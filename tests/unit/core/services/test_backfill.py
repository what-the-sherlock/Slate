from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from core.domain.documents import DocumentUpsert, NewChunk
from core.domain.errors import BudgetExceeded
from core.domain.ids import GenerationId, SourceId, WorkspaceId
from core.domain.principals import PUBLIC
from core.services.backfill import Provider, backfill_generation
from tests.fakes.models import FakeEmbedder
from tests.fakes.storage import InMemoryIndex
from tests.fakes.usage import InMemoryGenerations, InMemoryLedger

FREE = Provider("workersai", free_tier=True)
PAID = Provider("openai", free_tier=False, usd_per_m_tokens=Decimal("0.02"))
WS_A, WS_B = WorkspaceId(uuid4()), WorkspaceId(uuid4())
PRESENT = GenerationId(1)


async def seed(index: InMemoryIndex) -> None:
    """3 public chunks across two workspaces, 2 private ones."""
    docs = [
        (WS_A, "pub-a", "public", 2),
        (WS_B, "pub-b", "public", 1),
        (WS_A, "priv", "private", 2),
    ]
    for ws, ext, dc, n in docs:
        acl = (PUBLIC,) if dc == "public" else (f"workspace:{ws}",)
        await index.write_document(
            ws,
            DocumentUpsert(SourceId(uuid4()), ext, f"x://{ext}", ext, "text/plain", ext, acl),
            [NewChunk(i, f"{ext} chunk {i}", 3) for i in range(n)],
            {PRESENT: [[1.0] * 4] * n},
            data_class=dc,
        )


def setup() -> tuple[InMemoryIndex, InMemoryGenerations, InMemoryLedger]:
    return InMemoryIndex(), InMemoryGenerations(), InMemoryLedger()


async def test_public_generation_embeds_only_public_chunks_and_is_idempotent() -> None:
    index, gens, ledger = setup()
    await seed(index)
    embedder = FakeEmbedder(model="bge-test")
    first = await backfill_generation(
        index,
        gens,
        ledger,
        embedder,
        FREE,
        data_scope="public",
        monthly_budget_usd=Decimal(5),
        batch=2,
    )
    assert first.embedded == 3
    assert first.complete
    assert gens.future_gen is not None
    assert gens.future_gen.data_scope == "public"
    missing = await index.chunks_missing_embedding(
        first.generation, public_only=False, after=None, limit=10
    )
    assert {c.title for c in missing} == {"priv"}  # private chunks never sent
    again = await backfill_generation(
        index, gens, ledger, embedder, FREE, data_scope="public", monthly_budget_usd=Decimal(5)
    )
    assert again.embedded == 0
    assert again.complete


async def test_ledger_rows_per_workspace_and_marked_public() -> None:
    index, gens, ledger = setup()
    await seed(index)
    await backfill_generation(
        index,
        gens,
        ledger,
        FakeEmbedder(model="bge-test"),
        FREE,
        data_scope="public",
        monthly_budget_usd=Decimal(5),
    )
    assert {e.workspace_id for e in ledger.entries} == {WS_A, WS_B}
    assert {e.data_class for e in ledger.entries} == {"public"}
    assert all(e.cost_usd == 0 and e.provider == "workersai" for e in ledger.entries)


async def test_max_chunks_pauses_and_a_rerun_resumes() -> None:
    index, gens, ledger = setup()
    await seed(index)
    embedder = FakeEmbedder(model="bge-test")
    run = lambda: backfill_generation(  # noqa: E731
        index,
        gens,
        ledger,
        embedder,
        FREE,
        data_scope="public",
        monthly_budget_usd=Decimal(5),
        batch=1,
        max_chunks=2,
    )
    first = await run()
    assert (first.embedded, first.complete) == (2, False)
    second = await run()
    assert (second.embedded, second.complete) == (1, True)


async def test_free_tier_may_never_build_an_all_data_generation() -> None:
    index, gens, ledger = setup()
    with pytest.raises(ValueError, match="free-tier"):
        await backfill_generation(
            index,
            gens,
            ledger,
            FakeEmbedder(),
            FREE,
            data_scope="all",
            monthly_budget_usd=Decimal(5),
        )
    assert gens.future_gen is None  # refused before anything was created


async def test_paid_backfill_stops_at_the_budget() -> None:
    index, gens, _ = setup()
    await seed(index)
    with pytest.raises(BudgetExceeded):
        await backfill_generation(
            index,
            gens,
            InMemoryLedger(Decimal(5)),
            FakeEmbedder(model="paid"),
            PAID,
            data_scope="all",
            monthly_budget_usd=Decimal(5),
        )


@pytest.mark.parametrize(("batch", "max_chunks"), [(0, 10), (101, 10), (10, 0)])
async def test_rejects_bad_limits(batch: int, max_chunks: int) -> None:
    index, gens, ledger = setup()
    with pytest.raises(ValueError, match="batch"):
        await backfill_generation(
            index,
            gens,
            ledger,
            FakeEmbedder(),
            FREE,
            data_scope="public",
            monthly_budget_usd=Decimal(5),
            batch=batch,
            max_chunks=max_chunks,
        )
