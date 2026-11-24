"""Usage ledger (budget guard + ADR-0012 audit) and embedding generations over Postgres."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.errors import Conflict
from core.domain.ids import GenerationId
from core.ports.usage import GenerationInfo, UsageEntry


class PostgresUsageLedger:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def record(self, entry: UsageEntry) -> None:
        async with self._uow() as s:
            await s.execute(
                text(
                    "INSERT INTO usage_ledger (workspace_id, provider, model, kind, data_class,"
                    " tokens_in, tokens_out, cost_usd) VALUES (:ws, :p, :m, :k, :dc, :ti, :to, :c)"
                ),
                {
                    "ws": entry.workspace_id,
                    "p": entry.provider,
                    "m": entry.model,
                    "k": entry.kind,
                    "dc": entry.data_class,
                    "ti": entry.tokens_in,
                    "to": entry.tokens_out,
                    "c": entry.cost_usd,
                },
            )

    async def month_to_date_usd(self) -> Decimal:
        async with self._uow() as s:
            value = (
                await s.execute(
                    text(
                        "SELECT coalesce(sum(cost_usd), 0) FROM usage_ledger"
                        " WHERE day >= date_trunc('month', now())::date"
                    )
                )
            ).scalar_one()
        return Decimal(value)


class PostgresGenerationStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def present(self) -> GenerationInfo | None:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT id, model, dims FROM embedding_generations WHERE status = 'present'"
                    )
                )
            ).first()
        if row is None:
            return None
        return GenerationInfo(GenerationId(int(row[0])), str(row[1]), int(row[2]))

    async def ensure_present(self, model: str, dims: int) -> GenerationId:
        async with self._uow() as s:
            # Serialise first-time registration across concurrent workers.
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('slate-generations'))"))
            row = (
                await s.execute(
                    text("SELECT id, model FROM embedding_generations WHERE status = 'present'")
                )
            ).first()
            if row is not None:
                if row[1] != model:
                    raise Conflict(
                        f"generation for {row[1]!r} is present; switch models with a planned flip"
                    )
                return GenerationId(int(row[0]))
            gen = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO embedding_generations (model, dims, status)"
                            " VALUES (:m, :d, 'present') RETURNING id"
                        ),
                        {"m": model, "d": dims},
                    )
                ).scalar_one()
            )
            # One partial HNSW index per generation (ARCHITECTURE §4). Created inside this
            # transaction while the generation is still empty, so it's instant; later
            # generations on a full table should use CREATE INDEX CONCURRENTLY.
            ddl = (  # gen is an int from the DB; DDL can't take bind parameters
                f"CREATE INDEX IF NOT EXISTS chunk_embeddings_hnsw_g{gen}"
                " ON chunk_embeddings USING hnsw (embedding halfvec_cosine_ops)"
                f" WHERE generation_id = {gen}"
            )
            await s.execute(text(ddl))
            return GenerationId(gen)
