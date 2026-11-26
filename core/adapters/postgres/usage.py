"""Usage ledger (budget guard + ADR-0012 audit) and embedding generations over Postgres."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text

from core.adapters.postgres.uow import UnitOfWork
from core.domain.auth import canonical_email
from core.domain.budget import Spend
from core.domain.errors import Conflict
from core.domain.ids import GenerationId, WorkspaceId
from core.ports.usage import GenerationInfo, UsageEntry


class PostgresUsageLedger:
    def __init__(self, uow: UnitOfWork, *, owner_email: str | None = None) -> None:
        self._uow = uow
        # Canonical form, like users.email_canonical (ADR-0021).
        self._owner_email = canonical_email(owner_email) if owner_email else None

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

    async def spend(self, ws: WorkspaceId) -> Spend:
        # One round trip: the four sums plus the workspace's age and owner flag (budget.py).
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        "SELECT coalesce(sum(cost_usd), 0),"
                        " coalesce(sum(cost_usd) FILTER (WHERE day >= :today), 0),"
                        " coalesce(sum(cost_usd) FILTER (WHERE workspace_id = :ws), 0),"
                        " coalesce(sum(cost_usd) FILTER"
                        "   (WHERE workspace_id = :ws AND day >= :today), 0),"
                        " (SELECT extract(day FROM now() - w.created_at)::int"
                        "    FROM workspaces w WHERE w.id = :ws),"
                        " EXISTS (SELECT 1 FROM memberships m JOIN users u ON u.id = m.user_id"
                        "   WHERE m.workspace_id = :ws AND m.role = 'owner'"
                        "   AND u.email_verified_at IS NOT NULL"
                        "   AND u.email_canonical = CAST(:owner AS citext))"
                        # only while the owner is its sole member: invitees never inherit it
                        " AND (SELECT count(*) FROM memberships o WHERE o.workspace_id = :ws) = 1"
                        " FROM usage_ledger"
                        " WHERE day >= date_trunc('month', CAST(:today AS date))::date"
                    ),
                    # UTC days (budget.py)
                    {"ws": ws, "today": datetime.now(UTC).date(), "owner": self._owner_email or ""},
                )
            ).one()
        return Spend(
            month_all=Decimal(row[0]),
            day_all=Decimal(row[1]),
            month_workspace=Decimal(row[2]),
            day_workspace=Decimal(row[3]),
            privileged=bool(row[5]) and self._owner_email is not None,
            workspace_age_days=int(row[4]) if row[4] is not None else 10**6,
        )


class PostgresGenerationStore:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def present(self) -> GenerationInfo | None:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(f"SELECT {_GEN_COLS} FROM embedding_generations WHERE status = 'present'")  # noqa: S608 (constant column list)
                )
            ).first()
        return _gen(row)

    async def find(self, model: str) -> GenerationInfo | None:
        async with self._uow() as s:
            row = (
                await s.execute(
                    text(
                        f"SELECT {_GEN_COLS} FROM embedding_generations"  # noqa: S608 (constant column list)
                        " WHERE model = :m AND status IN ('present', 'future')"
                    ),
                    {"m": model},
                )
            ).first()
        return _gen(row)

    async def ensure_future(self, model: str, dims: int, *, data_scope: str) -> GenerationInfo:
        if data_scope not in {"all", "public"}:
            raise ValueError("data_scope must be 'all' or 'public'")
        async with self._uow() as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(hashtext('slate-generations'))"))
            rows = (
                await s.execute(
                    text(
                        f"SELECT {_GEN_COLS} FROM embedding_generations"  # noqa: S608 (constant column list)
                        " WHERE status IN ('present', 'future')"
                    )
                )
            ).all()
            for row in rows:
                info = _gen(row)
                if info is None:
                    continue
                if info.model == model:
                    if info.data_scope != data_scope or info.dims != dims:
                        raise Conflict(f"generation for {model!r} exists with other settings")
                    return info
                if info.status == "future":
                    raise Conflict(f"{info.model!r} is already being built as 'future'")
            gen = int(
                (
                    await s.execute(
                        text(
                            "INSERT INTO embedding_generations (model, dims, status, data_scope)"
                            " VALUES (:m, :d, 'future', :sc) RETURNING id"
                        ),
                        {"m": model, "d": dims, "sc": data_scope},
                    )
                ).scalar_one()
            )
            # The new generation has no rows yet, so building its partial index is instant.
            await s.execute(text(_hnsw_ddl(gen)))
        return GenerationInfo(GenerationId(gen), model, dims, "future", data_scope)

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
            await s.execute(text(_hnsw_ddl(gen)))
            return GenerationId(gen)


_GEN_COLS = "id, model, dims, status, data_scope"


def _gen(row: Sequence[Any] | None) -> GenerationInfo | None:  # a Row is a tuple
    if row is None:
        return None
    return GenerationInfo(
        GenerationId(int(row[0])), str(row[1]), int(row[2]), str(row[3]), str(row[4])
    )


def _hnsw_ddl(gen: int) -> str:
    """One partial HNSW index per generation (ARCHITECTURE section 4). ``gen`` is an int
    from the DB, because DDL can't take bind parameters."""
    return (
        f"CREATE INDEX IF NOT EXISTS chunk_embeddings_hnsw_g{int(gen)}"
        " ON chunk_embeddings USING hnsw (embedding halfvec_cosine_ops)"
        f" WHERE generation_id = {int(gen)}"
    )
