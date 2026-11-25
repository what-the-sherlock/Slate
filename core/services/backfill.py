"""Build a 'future' embedding generation from chunks already stored (ADR-0013 flow:
build future -> evaluate -> flip). Used for BGE-M3 on Workers AI (ADR-0004).

* **Idempotent and resumable:** each pass selects only chunks with no vector in the
  generation, and inserts with ON CONFLICT DO NOTHING. Stop it at any point, run it
  again, and it carries on; nothing is ever embedded twice.
* **Private data never reaches a free provider (ADR-0012):** a free-tier embedder may
  only build a 'public'-scoped generation, which only selects public chunks. The
  database trigger rejects a private chunk under a public generation as a last line.
* **Bounded:** ``max_chunks`` caps one run, so the daily free quota (or the monthly
  budget for a paid embedder) can't be blown by a single invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import structlog

from core.domain.chunking import embedding_text
from core.domain.documents import NewChunk
from core.domain.errors import BudgetExceeded
from core.domain.ids import ChunkId, GenerationId, WorkspaceId
from core.ports.models import Embedder
from core.ports.storage import DocumentStore
from core.ports.usage import GenerationStore, UsageEntry, UsageLedger

log = structlog.get_logger("backfill")

DEFAULT_BATCH = 50
DEFAULT_MAX_CHUNKS = 2_000


@dataclass(frozen=True, slots=True)
class BackfillReport:
    generation: GenerationId
    embedded: int
    complete: bool  # True when no eligible chunk is left without a vector


@dataclass(frozen=True, slots=True)
class Provider:
    name: str  # usage_ledger.provider, e.g. "workersai"
    free_tier: bool  # free tiers may retain inputs: public data only (ADR-0012)
    usd_per_m_tokens: Decimal = Decimal(0)


async def backfill_generation(
    documents: DocumentStore,
    generations: GenerationStore,
    ledger: UsageLedger,
    embedder: Embedder,
    provider: Provider,
    *,
    data_scope: str,
    monthly_budget_usd: Decimal,
    batch: int = DEFAULT_BATCH,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> BackfillReport:
    if provider.free_tier and data_scope != "public":
        raise ValueError("a free-tier embedder may only build a public-scoped generation")
    if not 1 <= batch <= 100 or max_chunks < 1:
        raise ValueError("batch must be 1-100 and max_chunks positive")
    gen = await generations.ensure_future(embedder.model, embedder.dims, data_scope=data_scope)
    public_only = gen.data_scope == "public"
    embedded = 0
    after: ChunkId | None = None
    while embedded < max_chunks:
        todo = await documents.chunks_missing_embedding(
            gen.id, public_only=public_only, after=after, limit=min(batch, max_chunks - embedded)
        )
        if not todo:
            log.info("backfill_complete", generation=int(gen.id), embedded=embedded)
            return BackfillReport(gen.id, embedded, complete=True)
        if provider.usd_per_m_tokens and await ledger.month_to_date_usd() >= monthly_budget_usd:
            raise BudgetExceeded("This month's AI budget is used up; backfill stopped.")
        # Exactly the text ingestion embeds, so generations are comparable.
        texts = [
            embedding_text(
                c.title, NewChunk(c.ordinal, c.content, token_count=0, heading_path=c.heading_path)
            )
            for c in todo
        ]
        result = await embedder.embed_documents(texts)
        embedded += await documents.add_embeddings(
            gen.id, [(c.chunk_id, v) for c, v in zip(todo, result.vectors, strict=True)]
        )
        # One ledger row per workspace in the batch, tokens split by text length.
        total_chars = sum(len(t) for t in texts) or 1
        by_ws: dict[WorkspaceId, int] = {}
        for c, t in zip(todo, texts, strict=True):
            by_ws[c.workspace_id] = by_ws.get(c.workspace_id, 0) + len(t)
        for ws, chars in by_ws.items():
            tokens = result.tokens * chars // total_chars
            await ledger.record(
                UsageEntry(
                    workspace_id=ws,
                    provider=provider.name,
                    model=embedder.model,
                    kind="embed",
                    data_class="public" if public_only else "private",
                    tokens_in=tokens,
                    tokens_out=0,
                    cost_usd=provider.usd_per_m_tokens * tokens / Decimal(1_000_000),
                )
            )
        after = todo[-1].chunk_id
        log.info("backfill_batch", generation=int(gen.id), embedded=embedded)
    log.info("backfill_paused", generation=int(gen.id), embedded=embedded, reason="max_chunks")
    return BackfillReport(gen.id, embedded, complete=False)
