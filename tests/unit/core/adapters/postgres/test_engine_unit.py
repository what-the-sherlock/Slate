import pytest
from sqlalchemy.pool import NullPool

from core.adapters.postgres.engine import EngineKind, create_engine
from core.adapters.postgres.uow import make_uow

URL = "postgresql+psycopg://u:p@localhost:5432/db"


async def test_pooled_engine_uses_nullpool() -> None:
    engine = create_engine(URL, EngineKind.POOLED)
    assert isinstance(engine.pool, NullPool)
    await engine.dispose()


async def test_direct_engine_uses_a_real_pool() -> None:
    engine = create_engine(URL, EngineKind.DIRECT)
    assert not isinstance(engine.pool, NullPool)
    await engine.dispose()


def test_make_uow_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="positive"):
        make_uow(None, statement_timeout_ms=0)  # type: ignore[arg-type]
