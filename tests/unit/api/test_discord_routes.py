from __future__ import annotations

from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient

from core.domain.discord import READ, VIEW_CHANNEL, Channel, ChannelType, Guild, Member, Overwrite
from core.domain.discord import Role as DRole
from tests.fakes.auth import FakeMailer
from tests.fakes.discord import BOT_ID, FakeDiscord
from tests.unit.api.accounts import csrf, sign_up

G = "200000000000000000"
ME = "200000000000000002"
GENERAL = "200000000000000010"
HIDDEN = "200000000000000011"


def setup_server(dc: FakeDiscord) -> None:
    s = dc.add_server(Guild(G, "Acme", "200000000000000001", (DRole(G, "@everyone", READ),)))
    s.channels[GENERAL] = Channel(GENERAL, "general", ChannelType.TEXT)
    s.channels[HIDDEN] = Channel(
        HIDDEN, "hidden", ChannelType.TEXT, overwrites=(Overwrite(G, 0, 0, VIEW_CHANNEL),)
    )
    s.members[ME] = Member(ME, ())
    dc.codes["goodcode1234"] = (G, ME)


def grant(client: TestClient, code: str = "goodcode1234") -> str:
    r = client.post("/api/v1/sources/discord/install", headers=csrf(client))
    assert r.status_code == 200, r.text
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    r = client.post(
        "/api/v1/sources/discord/installations",
        json={"guild_id": G, "code": code, "state": state},
        headers=csrf(client),
    )
    assert r.status_code == 200, r.text
    return r.json()["grant"]


def test_status_and_unconfigured(
    client: TestClient, mailer: FakeMailer, fake_discord: FakeDiscord
) -> None:
    sign_up(client, mailer)
    assert client.get("/api/v1/sources/discord/status").json() == {"available": True}
    fake_discord._configured = False
    assert client.get("/api/v1/sources/discord/status").json() == {"available": False}
    assert client.post("/api/v1/sources/discord/install", headers=csrf(client)).status_code == 503


def test_full_connect_sync_and_disconnect(
    client: TestClient, mailer: FakeMailer, fake_discord: FakeDiscord
) -> None:
    setup_server(fake_discord)
    sign_up(client, mailer)
    g = grant(client)
    server = client.post(
        "/api/v1/sources/discord/channels", json={"grant": g}, headers=csrf(client)
    ).json()
    rows = {c["id"]: c for c in server["channels"]}
    assert server["name"] == "Acme"
    assert rows[GENERAL]["selectable"]
    assert not rows[GENERAL]["private"]
    assert rows[HIDDEN]["private"]
    assert not rows[HIDDEN]["selectable"]
    assert rows[HIDDEN]["reason"] == "You can't read this channel"
    r = client.post(
        "/api/v1/sources/discord/connect",
        json={"grant": g, "channels": [GENERAL]},
        headers=csrf(client),
    )
    assert r.status_code == 201, r.text
    sid = r.json()["source_id"]
    r = client.post(f"/api/v1/sources/{sid}/sync", headers=csrf(client))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"  # no inline runner in this app
    assert client.delete(f"/api/v1/sources/{sid}", headers=csrf(client)).status_code == 204
    assert fake_discord.left == [G]
    assert client.delete(f"/api/v1/sources/{sid}", headers=csrf(client)).status_code == 404


def test_hidden_channel_cannot_be_forced_through_the_api(
    client: TestClient, mailer: FakeMailer, fake_discord: FakeDiscord
) -> None:
    setup_server(fake_discord)
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/sources/discord/connect",
        json={"grant": grant(client), "channels": [HIDDEN]},
        headers=csrf(client),
    )
    assert r.status_code == 403


def test_unproven_install_is_refused(
    client: TestClient, mailer: FakeMailer, fake_discord: FakeDiscord
) -> None:
    setup_server(fake_discord)
    sign_up(client, mailer)
    r = client.post("/api/v1/sources/discord/install", headers=csrf(client))
    state = parse_qs(urlsplit(r.json()["url"]).query)["state"][0]
    r = client.post(
        "/api/v1/sources/discord/installations",
        json={"guild_id": G, "code": "forgedcode99", "state": state},
        headers=csrf(client),
    )
    assert r.status_code == 403


def test_mutations_need_csrf(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    assert client.post("/api/v1/sources/discord/install").status_code == 403
    r = client.post("/api/v1/sources/discord/connect", json={"grant": "x", "channels": [GENERAL]})
    assert r.status_code == 403


def test_bad_input_is_422(client: TestClient, mailer: FakeMailer) -> None:
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/sources/discord/installations",
        json={"guild_id": "../x", "code": "c", "state": "s"},
        headers=csrf(client),
    )
    assert r.status_code == 422


def test_generic_routes_404_for_unknown_and_upload_sources(
    client: TestClient, mailer: FakeMailer
) -> None:
    sign_up(client, mailer)
    unknown = uuid4()
    assert client.post(f"/api/v1/sources/{unknown}/sync", headers=csrf(client)).status_code == 404
    assert client.delete(f"/api/v1/sources/{unknown}", headers=csrf(client)).status_code == 404


def test_other_account_cannot_sync_or_delete_my_server(
    client: TestClient, mailer: FakeMailer, fake_discord: FakeDiscord
) -> None:
    setup_server(fake_discord)
    sign_up(client, mailer)
    r = client.post(
        "/api/v1/sources/discord/connect",
        json={"grant": grant(client), "channels": [GENERAL]},
        headers=csrf(client),
    )
    sid = r.json()["source_id"]
    client.cookies.clear()
    sign_up(client, mailer, email="mallory@example.com")
    assert client.post(f"/api/v1/sources/{sid}/sync", headers=csrf(client)).status_code == 404
    assert client.delete(f"/api/v1/sources/{sid}", headers=csrf(client)).status_code == 404
    assert BOT_ID  # the bot never left
    assert fake_discord.left == []
