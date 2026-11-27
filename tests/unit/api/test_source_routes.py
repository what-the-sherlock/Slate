from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from core.domain.ids import JobId, SourceId, UserId, WorkspaceId
from core.ports.catalog import JobView, SourceSummary
from tests.fakes.auth import FakeMailer
from tests.fakes.catalog import InMemoryCatalog
from tests.unit.api.accounts import sign_up


def owner(client: TestClient, mailer: FakeMailer) -> dict[str, object]:
    sign_up(client, mailer)
    return client.get("/api/v1/me").json()


def summary(ws: WorkspaceId, **kw: object) -> SourceSummary:
    base: dict[str, object] = {
        "id": SourceId(uuid4()),
        "name": "Uploads",
        "connector": "upload",
        "data_class": "private",
        "visibility": (f"workspace:{ws}",),
        "status": "active",
        "documents": 3,
        "last_synced_at": datetime(2026, 1, 1, tzinfo=UTC),
        "last_run_status": "succeeded",
        "last_run_error": None,
        "pending_jobs": 0,
    }
    base.update(kw)
    return SourceSummary(**base)  # type: ignore[arg-type]


def test_sources_list_derives_status_and_visibility(
    client: TestClient, catalog: InMemoryCatalog, mailer: FakeMailer
) -> None:
    me = owner(client, mailer)
    ws = WorkspaceId(UUID(str(me["workspace_id"])))
    catalog.source_rows += [
        (ws, summary(ws)),
        (ws, summary(ws, name="Failing", last_run_status="failed", last_run_error="Bad PDF")),
        (
            ws,
            summary(
                ws,
                name="Busy",
                pending_jobs=2,
                jobs_running=2,
                last_run_status="running",
                running_since=datetime.now(UTC),
            ),
        ),
        (
            ws,
            summary(
                ws,
                name="Retrying",
                jobs_waiting=1,
                retry_error="unexpected KeyError",
                oldest_waiting_due=datetime.now(UTC),
            ),
        ),
        (
            ws,
            summary(ws, name="Empty", documents=0, run_skipped={"unsupported_type:pdf": 3}),
        ),
        (
            ws,
            summary(
                ws,
                name="Public uploads",
                data_class="public",
                visibility=("public", f"workspace:{ws}"),
            ),
        ),
    ]
    rows = {r["name"]: r for r in client.get("/api/v1/sources").json()}
    assert rows["Uploads"]["status"] == "synced"
    assert rows["Failing"]["status"] == "failed"
    assert rows["Failing"]["last_error"] == "Bad PDF"
    assert rows["Busy"]["status"] == "syncing"
    assert rows["Busy"]["busy"]
    assert rows["Retrying"]["status"] == "retrying"  # not "queued" with no end (P0-2)
    assert rows["Retrying"]["last_error"] == "Retrying: Slate hit an internal error (KeyError)."
    assert rows["Empty"]["status"] == "empty"
    assert not rows["Empty"]["healthy"]
    assert rows["Empty"]["skipped_summary"] == "3 PDFs (not supported yet)"
    assert rows["Public uploads"]["visibility"] == "public"


def test_sources_require_sign_in(client: TestClient) -> None:
    assert client.get("/api/v1/sources").status_code == 401


def test_jobs_are_private_to_their_uploader(
    client: TestClient, catalog: InMemoryCatalog, mailer: FakeMailer
) -> None:
    me = owner(client, mailer)
    ws = WorkspaceId(UUID(str(me["workspace_id"])))
    mine, theirs = JobId(uuid4()), JobId(uuid4())
    my_id = UserId(UUID(str(me["user"]["id"])))  # type: ignore[index]
    catalog.jobs[mine] = (ws, JobView(mine, "dead", 1, "No text found", "a.pdf", my_id))
    catalog.jobs[theirs] = (ws, JobView(theirs, "queued", 0, None, "b.pdf", UserId(uuid4())))
    r = client.get(f"/api/v1/jobs/{mine}")
    assert r.json()["error"] == "No text found"
    # The owner is an admin, so they may see others' jobs; a plain member wouldn't.
    assert client.get(f"/api/v1/jobs/{theirs}").status_code == 200
    assert client.get(f"/api/v1/jobs/{uuid4()}").status_code == 404
