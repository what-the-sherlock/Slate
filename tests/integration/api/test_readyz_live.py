import httpx

from api.deps import get_container
from api.main import create_app
from core.config import Settings


async def test_readyz_against_real_neon_branch(settings: Settings) -> None:
    # httpx over ASGI runs the app on the test's own (selector) event loop.
    # fastapi's TestClient would use a separate loop thread, which psycopg can't use on Windows.
    get_container.cache_clear()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get("/api/v1/readyz")
    body = r.json()
    assert body["database"] is True
    # Ready exactly when the branch's schema matches the code's migration head.
    assert (r.status_code == 200) == body["migrations_current"]
