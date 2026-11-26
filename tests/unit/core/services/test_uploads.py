from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import uuid4

import pytest

from core.domain.auth import Identity, Role
from core.domain.errors import Conflict, PermissionDenied, RateLimited, ValidationFailed
from core.domain.ids import SessionId, UserId, WorkspaceId
from core.domain.jobs import JobStatus
from core.domain.principals import PUBLIC, Principals
from core.domain.signed_tokens import sign
from core.domain.uploads import MAX_UPLOAD_BYTES, Visibility, access_for, external_id_for
from core.services.uploads import (
    PROCESS_UPLOAD,
    TOKEN_PURPOSE,
    BeginResult,
    CompleteResult,
    UploadService,
    UploadStatus,
    derive_key,
)
from tests.fakes.jobs import FakeJobQueue
from tests.fakes.rate_limit import InMemoryRateLimiter
from tests.fakes.sources import InMemorySources
from tests.fakes.storage import InMemoryBlobStore
from tests.fakes.system import FakeClock

PDF = b"%PDF-1.7\n" + b"x" * 500
ROOT = b"r" * 32


def identity(
    role: Role = Role.MEMBER,
    *,
    guest: bool = False,
    ws: WorkspaceId | None = None,
    user: UserId | None = None,
) -> Identity:
    u, w = user or UserId(uuid4()), ws or WorkspaceId(uuid4())
    return Identity(
        user_id=u,
        workspace_id=w,
        session_id=SessionId(uuid4()),
        email="a@b.c",
        name="A",
        role=role,
        email_verified=True,
        principals=(Principals.for_outsider(u) if guest else Principals.for_user(u, w)),
        csrf_secret="c",
    )


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Env:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.blobs = InMemoryBlobStore()
        self.sources = InMemorySources()
        self.jobs = FakeJobQueue(self.clock)
        self.limiter = InMemoryRateLimiter(self.clock)
        self.svc = UploadService(
            self.blobs, self.sources, self.jobs, self.limiter, self.clock, root_secret=ROOT
        )

    async def begin(
        self,
        who: Identity,
        data: bytes = PDF,
        name: str = "a.pdf",
        visibility: Visibility = Visibility.ONLY_ME,
    ) -> BeginResult:
        return await self.svc.begin(
            who, filename=name, size=len(data), sha256=sha(data), visibility=visibility
        )

    async def upload(
        self, who: Identity, data: bytes = PDF, visibility: Visibility = Visibility.ONLY_ME
    ) -> CompleteResult:
        begun = await self.begin(who, data, visibility=visibility)
        assert begun.status is UploadStatus.UPLOAD
        assert begun.upload is not None
        assert begun.upload_token is not None
        await self.blobs.put(begun.upload.fields["key"], data, "application/pdf")  # the browser
        return await self.svc.complete(who, begun.upload_token)


@pytest.fixture
def env() -> Env:
    return Env()


# ---------------------------------------------------------------- happy path


async def test_happy_path_queues_processing_with_the_access_plan(env: Env) -> None:
    who = identity()
    done = await env.upload(who)
    assert done.status is UploadStatus.UPLOAD
    assert done.job_id is not None
    job = await env.jobs.get(done.job_id)
    assert job.kind == PROCESS_UPLOAD
    assert job.payload["acl"] == [f"user:{who.user_id}"]
    assert job.payload["data_class"] == "private"
    assert job.payload["sha256"] == sha(PDF)  # the worker re-verifies against this
    assert next(iter(env.blobs.upload_policies.values()))[1] == MAX_UPLOAD_BYTES


@pytest.mark.parametrize("role", [Role.MEMBER, Role.ADMIN, Role.OWNER])
@pytest.mark.parametrize("visibility", list(Visibility))
async def test_no_upload_is_ever_public(env: Env, role: Role, visibility: Visibility) -> None:
    """ADR-0026: whatever the role or choice, user uploads are private data with no
    'public' principal, so they never reach free-tier AI or other accounts."""
    done = await env.upload(identity(role), visibility=visibility)
    assert done.job_id is not None
    job = await env.jobs.get(done.job_id)
    assert job.payload["data_class"] == "private"
    acl = job.payload["acl"]
    assert isinstance(acl, list)
    assert PUBLIC not in acl


async def test_a_token_minted_for_the_removed_public_choice_is_refused(env: Env) -> None:
    """A link issued before ADR-0026 shipped (15-minute tokens) fails cleanly, not with 500."""
    who = identity(Role.OWNER)
    token = sign(
        {"u": str(who.user_id), "w": str(who.workspace_id), "v": "public"},
        secret=derive_key(ROOT, "upload-token"),
        purpose=TOKEN_PURPOSE,
        expires_at=env.clock.now() + timedelta(minutes=5),
    )
    with pytest.raises(ValidationFailed, match="expired"):
        await env.svc.complete(who, token)


# ---------------------------------------------------------------- dedup and visibility


async def test_reupload_while_processing_is_not_queued_twice(env: Env) -> None:
    who = identity()
    await env.upload(who)
    assert (await env.begin(who)).status is UploadStatus.PROCESSING
    assert len(env.jobs.jobs) == 1


async def test_double_complete_enqueues_one_job(env: Env) -> None:
    who = identity()
    begun = await env.begin(who)
    assert begun.upload is not None
    await env.blobs.put(begun.upload.fields["key"], PDF, "application/pdf")
    first = await env.svc.complete(who, begun.upload_token or "")
    second = await env.svc.complete(who, begun.upload_token or "")  # double click / replay
    assert second.status is UploadStatus.PROCESSING
    assert second.job_id == first.job_id
    assert len(env.jobs.jobs) == 1


async def test_reupload_with_same_access_is_a_duplicate(env: Env) -> None:
    who = identity()
    plan = access_for(Visibility.ONLY_ME, who.user_id, who.workspace_id)
    doc = env.sources.index(
        who.workspace_id, external_id_for(sha(PDF), who.user_id), plan.acl, plan.data_class
    )
    again = await env.begin(who)
    assert again.status is UploadStatus.DUPLICATE
    assert again.document_id == doc
    assert again.upload is None


@pytest.mark.parametrize(
    ("indexed_as", "requested"),
    [
        (Visibility.WORKSPACE, Visibility.ONLY_ME),  # narrowing: must NOT say "duplicate"
        (Visibility.ONLY_ME, Visibility.WORKSPACE),  # widening
    ],
    ids=["narrow", "widen"],
)
async def test_reupload_with_different_access_is_flagged(
    env: Env, indexed_as: Visibility, requested: Visibility
) -> None:
    who = identity(Role.ADMIN)
    plan = access_for(indexed_as, who.user_id, who.workspace_id)
    doc = env.sources.index(
        who.workspace_id, external_id_for(sha(PDF), who.user_id), plan.acl, plan.data_class
    )
    again = await env.begin(who, visibility=requested)
    assert again.status is UploadStatus.VISIBILITY_DIFFERS
    assert again.document_id == doc
    assert again.upload is None  # never a second document over the same bytes
    assert not env.jobs.jobs


async def test_a_dead_job_is_retried_under_a_deterministic_key(env: Env) -> None:
    who = identity()
    done = await env.upload(who)
    job = await env.jobs.claim("w", [PROCESS_UPLOAD])
    assert job is not None
    await env.jobs.fail(job.id, "w", "bad pdf", retryable=False)
    assert (await env.jobs.get(job.id)).status is JobStatus.DEAD
    retry = await env.upload(who)
    assert retry.job_id is not None
    assert retry.job_id != done.job_id
    assert (await env.jobs.get(retry.job_id)).idempotency_key.endswith(f"retry-of:{job.id}")


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("data", "name", "message"),
    [
        (b"", "empty.pdf", "empty"),
        (PDF, "evil.exe", "isn't supported"),
        (b"", "x.exe", "isn't supported"),  # type problem reported, not "empty"
    ],
)
async def test_begin_rejects_bad_files(env: Env, data: bytes, name: str, message: str) -> None:
    with pytest.raises(ValidationFailed) as info:
        await env.begin(identity(), data, name)
    fields = info.value.extra["fields"]
    assert isinstance(fields, dict)
    assert message in str(fields.get("file"))


async def test_begin_rejects_oversize_before_any_upload(env: Env) -> None:
    with pytest.raises(ValidationFailed, match="can't be uploaded"):
        await env.svc.begin(
            identity(),
            filename="big.pdf",
            size=MAX_UPLOAD_BYTES + 1,
            sha256="a" * 64,
            visibility=Visibility.ONLY_ME,
        )


async def test_mislabelled_file_is_rejected_at_complete(env: Env) -> None:
    who = identity()
    fake_pdf = b"MZ\x90\x00 this is an executable"
    begun = await env.begin(who, fake_pdf, "invoice.pdf")
    assert begun.upload is not None
    await env.blobs.put(begun.upload.fields["key"], fake_pdf, "application/pdf")
    with pytest.raises(ValidationFailed, match=r"valid \.pdf"):
        await env.svc.complete(who, begun.upload_token or "")
    assert not env.jobs.jobs


async def test_complete_before_the_bytes_arrive_is_a_conflict(env: Env) -> None:
    who = identity()
    begun = await env.begin(who)
    with pytest.raises(Conflict, match="hasn't finished uploading"):
        await env.svc.complete(who, begun.upload_token or "")


async def test_size_mismatch_is_rejected(env: Env) -> None:
    who = identity()
    begun = await env.begin(who)
    assert begun.upload is not None
    await env.blobs.put(begun.upload.fields["key"], PDF + b"extra", "application/pdf")
    with pytest.raises(ValidationFailed, match="doesn't match"):
        await env.svc.complete(who, begun.upload_token or "")


# ---------------------------------------------------------------- tokens and roles


async def test_someone_elses_token_is_refused(env: Env) -> None:
    alice = identity()
    begun = await env.begin(alice)
    mallory = identity(ws=alice.workspace_id)
    with pytest.raises(PermissionDenied):
        await env.svc.complete(mallory, begun.upload_token or "")


async def test_same_user_in_another_workspace_is_refused(env: Env) -> None:
    alice = identity()
    begun = await env.begin(alice)
    with pytest.raises(PermissionDenied):
        await env.svc.complete(identity(user=alice.user_id), begun.upload_token or "")


async def test_tokens_signed_with_the_raw_root_secret_are_rejected(env: Env) -> None:
    # Key separation: a MAC made with SESSION_SECRET itself (or another purpose's
    # sub-key) never validates as an upload token.
    who = identity()
    forged = sign(
        {"u": str(who.user_id), "w": str(who.workspace_id)},
        secret=ROOT,
        purpose="upload",
        expires_at=env.clock.now() + timedelta(minutes=5),
    )
    with pytest.raises(ValidationFailed):
        await env.svc.complete(who, forged)


@pytest.mark.parametrize(
    ("who", "visibility"),
    [
        (identity(Role.VIEWER), Visibility.ONLY_ME),
        (identity(Role.VIEWER, guest=True), Visibility.ONLY_ME),
        (identity(Role.VIEWER), Visibility.WORKSPACE),
    ],
    ids=["viewer", "guest", "viewer-workspace"],
)
async def test_role_checks(env: Env, who: Identity, visibility: Visibility) -> None:
    with pytest.raises(PermissionDenied):
        await env.begin(who, visibility=visibility)


# ---------------------------------------------------------------- abuse limits


async def test_begin_is_rate_limited_per_user(env: Env) -> None:
    who = identity()
    for n in range(30):
        await env.begin(who, PDF + str(n).encode())
    with pytest.raises(RateLimited, match="a lot of uploads"):
        await env.begin(who, PDF + b"one more")
    assert (await env.begin(identity(), PDF)).status is UploadStatus.UPLOAD  # others unaffected


async def test_daily_byte_budget_is_enforced(env: Env) -> None:
    who = identity()
    for n in range(20):  # 20 x 25 MB = 500 MB announced
        await env.svc.begin(
            who,
            filename=f"big{n}.pdf",
            size=MAX_UPLOAD_BYTES,
            sha256=f"{n:064x}",
            visibility=Visibility.ONLY_ME,
        )
    with pytest.raises(RateLimited, match="500 MB"):
        await env.svc.begin(
            who,
            filename="one-more.pdf",
            size=MAX_UPLOAD_BYTES,
            sha256="f" * 64,
            visibility=Visibility.ONLY_ME,
        )


# ---------------------------------------------------------------- workspace quotas (ADR-0021)


async def test_upload_is_refused_at_the_workspace_document_limit(env: Env) -> None:
    from core.domain.uploads import MAX_DOCUMENTS_PER_WORKSPACE

    who = identity()
    env.sources.usage[who.workspace_id] = (MAX_DOCUMENTS_PER_WORKSPACE, 0)
    with pytest.raises(ValidationFailed, match="limit of 50 documents") as info:
        await env.begin(who)
    assert "file" in info.value.extra["fields"]  # type: ignore[operator]
    assert env.blobs.upload_policies == {}  # no presigned URL was issued


async def test_upload_is_refused_over_the_workspace_storage_limit(env: Env) -> None:
    from core.domain.uploads import MAX_STORED_BYTES_PER_WORKSPACE

    who = identity()
    env.sources.usage[who.workspace_id] = (1, MAX_STORED_BYTES_PER_WORKSPACE - len(PDF) + 1)
    with pytest.raises(ValidationFailed, match="100 MB"):
        await env.begin(who)
    env.sources.usage[who.workspace_id] = (1, MAX_STORED_BYTES_PER_WORKSPACE - len(PDF))
    assert (await env.begin(who)).status is UploadStatus.UPLOAD  # exactly at the limit is fine


async def test_another_workspaces_usage_does_not_count(env: Env) -> None:
    full, mine = identity(), identity()
    env.sources.usage[full.workspace_id] = (10_000, 10**12)
    assert (await env.begin(mine)).status is UploadStatus.UPLOAD
