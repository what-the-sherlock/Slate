from core.services.health import check_readiness
from tests.fakes.system import FakeHealthProbe


async def test_ready_when_db_ok_and_revision_matches() -> None:
    r = await check_readiness(FakeHealthProbe(db_ok=True, revision="0001"), "0001")
    assert r.ready
    assert r.migrations_current


async def test_not_ready_when_migrations_behind() -> None:
    r = await check_readiness(FakeHealthProbe(db_ok=True, revision="0001"), "0002")
    assert not r.ready
    assert r.database
    assert not r.migrations_current


async def test_not_ready_when_db_down_and_revision_not_queried() -> None:
    r = await check_readiness(FakeHealthProbe(db_ok=False, revision="0001"), "0001")
    assert not r.ready
    assert not r.database
    assert r.migration_revision is None
