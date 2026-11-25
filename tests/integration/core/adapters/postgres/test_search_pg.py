"""Retrieval SQL against real Neon Postgres: both lanes, filters, and isolation.

The guest-isolation test is the M2 "done when" gate: a guest must never get a private
chunk from either lane, and no user may see another workspace's chunks. Vectors come
from the deterministic FakeEmbedder (zero cost). The test uses its own 'past'
generation, so it never touches the branch's real 'present' one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import text

from core.adapters.postgres.documents import PostgresDocumentStore
from core.adapters.postgres.search import PostgresSearchIndex
from core.adapters.postgres.sources import PostgresSourceStore
from core.adapters.postgres.uow import UnitOfWork
from core.domain.data_class import DataClass
from core.domain.documents import DocumentUpsert, NewChunk, ScoredChunk, SearchFilters
from core.domain.errors import NotFound
from core.domain.ids import GenerationId, SourceId, UserId, WorkspaceId
from core.domain.principals import PUBLIC, Principals, user_principal, workspace_principal
from tests.fakes.models import FakeEmbedder

MODEL = "search-it-fake"
embedder = FakeEmbedder(model=MODEL)

PUBLIC_TEXT = "Everyone gets 25 days of paid leave per year."
PRIVATE_TEXT = "Salary bands are confidential. Paid leave cover is ERR_BUDGET_42."
TEAM_TEXT = "Standup is on Mondays; paid leave requests go to the team lead."


@dataclass
class World:
    ws: WorkspaceId
    other_ws: WorkspaceId
    owner: UserId
    guest: UserId
    source: SourceId
    gen: GenerationId

    def owner_p(self) -> Principals:
        return Principals.for_user(self.owner, self.ws)

    def guest_p(self) -> Principals:
        return Principals.for_outsider(self.guest)


async def _scalar(uow: UnitOfWork, sql: str, **params: object) -> object:
    async with uow() as s:
        return (await s.execute(text(sql), params)).scalar_one()


async def _write(
    uow: UnitOfWork,
    w: World,
    ws: WorkspaceId,
    source: SourceId,
    ext: str,
    body: str,
    acl: tuple[str, ...],
    updated: datetime | None = None,
) -> None:
    vectors = (await embedder.embed_documents([body])).vectors
    await PostgresDocumentStore(uow).write_document(
        ws,
        DocumentUpsert(
            source,
            ext,
            f"upload://{ext}",
            ext.title(),
            "text/plain",
            ext,
            acl,
            source_updated_at=updated,
        ),
        [NewChunk(0, body, len(body.split()), heading_path="Policies")],
        {w.gen: vectors},
        data_class="private",
    )


@pytest.fixture
async def world(uow: UnitOfWork) -> AsyncIterator[World]:
    new_ws = (
        "INSERT INTO workspaces (name, slug) VALUES ('s', 's-' || gen_random_uuid()) RETURNING id"
    )
    new_user = (
        "INSERT INTO users (email, email_canonical, name, password_hash)"
        " SELECT e, e, 'S', 'h' FROM (SELECT 's-' || gen_random_uuid() || '@t.dev' AS e) x"
        " RETURNING id"
    )
    ws = WorkspaceId(UUID(str(await _scalar(uow, new_ws))))
    other = WorkspaceId(UUID(str(await _scalar(uow, new_ws))))
    owner = UserId(UUID(str(await _scalar(uow, new_user))))
    guest = UserId(UUID(str(await _scalar(uow, new_user))))
    gen = GenerationId(
        int(
            str(
                await _scalar(
                    uow,
                    "INSERT INTO embedding_generations (model, dims, status)"
                    " VALUES (:m, 1024, 'past') RETURNING id",
                    m=MODEL,
                )
            )
        )
    )
    sources = PostgresSourceStore(uow)
    source = await sources.upload_source(ws, DataClass.PRIVATE)
    other_source = await sources.upload_source(other, DataClass.PRIVATE)
    w = World(ws, other, owner, guest, source, gen)

    await _write(
        uow, w, ws, source, "handbook", PUBLIC_TEXT, (PUBLIC,), datetime(2026, 1, 1, tzinfo=UTC)
    )
    await _write(
        uow,
        w,
        ws,
        source,
        "salaries",
        PRIVATE_TEXT,
        (user_principal(owner),),
        datetime(2026, 6, 1, tzinfo=UTC),
    )
    await _write(uow, w, ws, source, "team", TEAM_TEXT, (workspace_principal(ws),))
    # Another workspace's document is readable by 'public', so ONLY workspace_id stops it.
    await _write(uow, w, other, other_source, "leak", PUBLIC_TEXT + " paid leave", (PUBLIC,))
    yield w
    async with uow() as s:
        await s.execute(text("DELETE FROM workspaces WHERE id = ANY(:w)"), {"w": [ws, other]})
        await s.execute(text("DELETE FROM embedding_generations WHERE id = :g"), {"g": gen})
        await s.execute(text("DELETE FROM users WHERE id = ANY(:u)"), {"u": [owner, guest]})


def titles(hits: Sequence[ScoredChunk]) -> set[str | None]:
    return {h.title for h in hits}


async def test_keyword_lane_ranks_and_filters_acl(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    owner = await index.keyword_search(world.ws, world.owner_p(), "paid leave", 10, SearchFilters())
    assert titles(owner) == {"Handbook", "Salaries", "Team"}
    assert all(h.score > 0 for h in owner)
    guest = await index.keyword_search(world.ws, world.guest_p(), "paid leave", 10, SearchFilters())
    assert titles(guest) == {"Handbook"}


async def test_keyword_lane_matches_identifiers_and_stems(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    # tsv_simple keeps identifiers whole; tsv_en stems "requested" ~ "requests".
    exact = await index.keyword_search(
        world.ws, world.owner_p(), "ERR_BUDGET_42", 10, SearchFilters()
    )
    assert titles(exact) == {"Salaries"}
    stemmed = await index.keyword_search(
        world.ws, world.owner_p(), "requested", 10, SearchFilters()
    )
    assert titles(stemmed) == {"Team"}
    # websearch syntax never raises on odd input.
    odd = await index.keyword_search(
        world.ws, world.owner_p(), '"unclosed -( OR', 10, SearchFilters()
    )
    assert isinstance(odd, list)


async def test_vector_lane_ranks_nearest_first_and_filters_acl(
    uow: UnitOfWork, world: World
) -> None:
    index = PostgresSearchIndex(uow)
    q = await embedder.embed_query(PRIVATE_TEXT)
    owner = await index.vector_search(world.ws, world.owner_p(), q, world.gen, 10, SearchFilters())
    assert owner[0].title == "Salaries"
    assert owner[0].score == pytest.approx(1.0, abs=1e-2)  # halfvec rounding
    assert [h.score for h in owner] == sorted((h.score for h in owner), reverse=True)
    guest = await index.vector_search(world.ws, world.guest_p(), q, world.gen, 10, SearchFilters())
    assert titles(guest) == {"Handbook"}


async def test_guest_isolation_across_both_lanes(uow: UnitOfWork, world: World) -> None:
    """M2 gate: no private chunk reaches a guest, whatever they search for."""
    index = PostgresSearchIndex(uow)
    for query in (PRIVATE_TEXT, TEAM_TEXT, "salary confidential Mondays ERR_BUDGET_42"):
        kw = await index.keyword_search(world.ws, world.guest_p(), query, 50, SearchFilters())
        vec = await index.vector_search(
            world.ws,
            world.guest_p(),
            await embedder.embed_query(query),
            world.gen,
            50,
            SearchFilters(),
        )
        assert titles(kw) <= {"Handbook"}
        assert titles(vec) <= {"Handbook"}


async def test_workspaces_never_mix(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    q = await embedder.embed_query(PUBLIC_TEXT + " paid leave")
    for p in (world.owner_p(), world.guest_p()):
        kw = await index.keyword_search(world.ws, p, "paid leave", 50, SearchFilters())
        vec = await index.vector_search(world.ws, p, q, world.gen, 50, SearchFilters())
        assert "Leak" not in titles(kw) | titles(vec)


async def test_filters_and_deleted_documents(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    after = SearchFilters(updated_after=datetime(2026, 3, 1, tzinfo=UTC))
    assert titles(
        await index.keyword_search(world.ws, world.owner_p(), "paid leave", 10, after)
    ) == {"Salaries"}
    other_source = SearchFilters(source_ids=(SourceId(UUID(int=7)),))
    assert (
        await index.keyword_search(world.ws, world.owner_p(), "paid leave", 10, other_source) == []
    )

    async with uow() as s:
        await s.execute(
            text(
                "UPDATE documents SET deleted_at = now() WHERE workspace_id = :w AND external_id = 'handbook'"
            ),
            {"w": world.ws},
        )
    assert (
        titles(
            await index.keyword_search(world.ws, world.guest_p(), "paid leave", 10, SearchFilters())
        )
        == set()
    )


async def test_k_is_bounded(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    with pytest.raises(ValueError, match="k must be"):
        await index.keyword_search(world.ws, world.owner_p(), "leave", 0, SearchFilters())
    with pytest.raises(ValueError, match="k must be"):
        await index.keyword_search(world.ws, world.owner_p(), "leave", 10_000, SearchFilters())


async def test_deleting_source_vanishes_and_long_queries_are_refused(
    uow: UnitOfWork, world: World
) -> None:
    index = PostgresSearchIndex(uow)
    with pytest.raises(ValueError, match="longer than"):
        await index.keyword_search(world.ws, world.owner_p(), "x" * 501, 10, SearchFilters())
    async with uow() as s:
        await s.execute(
            text("UPDATE sources SET status = 'deleting' WHERE id = :s"), {"s": world.source}
        )
    q = await embedder.embed_query(PUBLIC_TEXT)
    kw = await index.keyword_search(world.ws, world.owner_p(), "paid leave", 10, SearchFilters())
    vec = await index.vector_search(world.ws, world.owner_p(), q, world.gen, 10, SearchFilters())
    assert kw == []
    assert vec == []


async def test_preview_uses_the_same_access_rule(uow: UnitOfWork, world: World) -> None:
    index = PostgresSearchIndex(uow)
    hits = await index.keyword_search(
        world.ws, world.owner_p(), "ERR_BUDGET_42", 10, SearchFilters()
    )
    doc_id = hits[0].document_id
    preview = await index.document_preview(world.ws, world.owner_p(), doc_id, 50)
    assert preview.title == "Salaries"
    assert [c.content for c in preview.chunks] == [PRIVATE_TEXT]
    for ws, p in ((world.ws, world.guest_p()), (world.other_ws, world.owner_p())):
        with pytest.raises(NotFound):
            await index.document_preview(ws, p, doc_id, 50)


@pytest.mark.parametrize("query", ["a\b", "\\\\", "it's", "'; --", "kf:5002", "the and of"])
async def test_odd_keyword_queries_never_error(uow: UnitOfWork, world: World, query: str) -> None:
    hits = await PostgresSearchIndex(uow).keyword_search(
        world.ws, world.owner_p(), query, 10, SearchFilters()
    )
    assert isinstance(hits, list)


async def test_neighbours_use_the_same_access_rule(uow: UnitOfWork, world: World) -> None:
    """Answer assembly widens a passage with ordinal +-1 (ARCHITECTURE §8 step 6). The
    neighbour lookup must hide what search hides: other ACLs and other workspaces."""
    vecs = (await embedder.embed_documents(["a", "b", "c"])).vectors
    doc_id, _ = await PostgresDocumentStore(uow).write_document(
        world.ws,
        DocumentUpsert(world.source, "nbr", "upload://nbr", "Nbr", "text/plain", "nbr", (PUBLIC,)),
        [NewChunk(i, t, 1) for i, t in enumerate(["first", "middle", "last"])],
        {world.gen: vecs},
        data_class="private",
    )
    index = PostgresSearchIndex(uow)
    got = await index.neighbours(world.ws, world.owner_p(), [(doc_id, 1)])
    assert sorted(c.content for c in got) == ["first", "last"]
    assert all(c.data_class == "private" for c in got)
    assert await index.neighbours(world.other_ws, world.owner_p(), [(doc_id, 1)]) == []
    secret = (await embedder.embed_documents(["s0", "s1"])).vectors
    secret_id, _ = await PostgresDocumentStore(uow).write_document(
        world.ws,
        DocumentUpsert(
            world.source,
            "nbr-secret",
            "upload://s",
            "S",
            "text/plain",
            "s",
            (f"user:{world.owner}",),
        ),
        [NewChunk(i, t, 1) for i, t in enumerate(["s0", "s1"])],
        {world.gen: secret},
        data_class="private",
    )
    assert await index.neighbours(world.ws, world.guest_p(), [(secret_id, 0)]) == []
