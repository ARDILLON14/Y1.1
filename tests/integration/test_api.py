from __future__ import annotations

import httpx
import pytest

from copytrader.api.server import create_app
from copytrader.db.repositories import UserRepo
from copytrader.security.passwords import hash_password
from tests.integration.conftest import seeded_container

PASSWORD = "correct horse battery staple"


@pytest.fixture
async def client(tmp_path, template_db):
    c = await seeded_container(tmp_path, template_db, {"api": {"secure_cookies": False, "login_max_attempts": 3}})
    async with c.db.session() as s:
        await UserRepo(s).set_password("admin", hash_password(PASSWORD))
    app = create_app(c)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as cl:
        yield cl, c
    await c.aclose()


async def _login(cl) -> str:
    r = await cl.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    return r.json()["csrf"]


async def test_requires_authentication_and_sets_security_headers(client):
    cl, _ = client
    r = await cl.get("/api/overview")
    assert r.status_code == 401
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert (await cl.get("/healthz")).status_code == 200
    index = await cl.get("/")
    assert index.status_code == 200 and "<script" in index.text and "cdn" not in index.text.lower()


async def test_login_cookie_flags_and_overview(client):
    cl, _ = client
    r = await cl.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    ov = (await cl.get("/api/overview")).json()
    assert ov["mode"]["level"] == 3 and ov["wallets"]["total"] == 17
    assert ov["book"]["equity_usd"] == pytest.approx(1000.0)


async def test_bruteforce_lockout(client):
    cl, _ = client
    for _ in range(3):
        r = await cl.post("/api/auth/login", json={"username": "admin", "password": "wrong-password-x"})
        assert r.status_code == 401
    r = await cl.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 401 and "intentos" in r.json()["detail"]


async def test_csrf_required_for_mutations(client):
    cl, _ = client
    csrf = await _login(cl)
    body = {"patch": {"selection": {"top_n": 5}}}
    assert (await cl.patch("/api/config", json=body)).status_code == 403
    r = await cl.patch("/api/config", json=body, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200
    assert (await cl.get("/api/config")).json()["config"]["selection"]["top_n"] == 5


async def test_invalid_or_locked_config_is_rejected(client):
    cl, c = client
    csrf = await _login(cl)
    h = {"X-CSRF-Token": csrf}
    r = await cl.patch("/api/config", json={"patch": {"risk": {"max_trade_usd": 999_999}}}, headers=h)
    assert r.status_code == 400
    r = await cl.patch("/api/config", json={"patch": {"app": {"operating_level": 5}}}, headers=h)
    assert r.status_code == 400
    r = await cl.patch("/api/config", json={"patch": {"levels": {"live_trading_enabled": True}}}, headers=h)
    assert r.status_code == 400
    assert c.cfg.risk.max_trade_usd == 100


async def test_sensitive_actions_require_password(client):
    cl, _ = client
    csrf = await _login(cl)
    h = {"X-CSRF-Token": csrf}
    r = await cl.post("/api/system/level", json={"level": 2}, headers=h)
    assert r.status_code == 403
    r = await cl.post("/api/system/level", json={"level": 2, "password": "nope-nope-nope"}, headers=h)
    assert r.status_code == 403
    r = await cl.post("/api/system/level", json={"level": 2, "password": PASSWORD}, headers=h)
    assert r.status_code == 200 and r.json()["level"] == 2
    r = await cl.post("/api/system/level", json={"level": 5, "password": PASSWORD}, headers=h)
    assert r.status_code == 400  # above the configured ceiling
    # activating the kill switch is free, deactivating the global one needs the password
    r = await cl.post("/api/risk/kill-switch", json={"action": "activate", "scope": "global", "reason": "t"}, headers=h)
    assert r.json()["global"]["active"]
    r = await cl.post("/api/risk/kill-switch", json={"action": "deactivate", "scope": "global"}, headers=h)
    assert r.status_code == 403
    r = await cl.post(
        "/api/risk/kill-switch", json={"action": "deactivate", "scope": "global", "password": PASSWORD}, headers=h
    )
    assert r.status_code == 200 and not r.json()["global"]["active"]
    audit = (await cl.get("/api/audit")).json()
    assert {"set_level", "kill_switch_on:global", "kill_switch_off:global", "login"} <= {a["action"] for a in audit}


async def test_arm_requires_preflight(client):
    cl, c = client
    csrf = await _login(cl)
    r = await cl.post("/api/system/arm", json={"password": PASSWORD}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 409 and "preflight" in r.json()["detail"]
    assert not c.mode.armed and c.mode.trade_mode.value == "paper"


async def test_wallet_crud_and_validation(client):
    cl, _ = client
    csrf = await _login(cl)
    h = {"X-CSRF-Token": csrf}
    wallets = (await cl.get("/api/wallets")).json()
    assert len(wallets) == 17 and {"score", "status", "metrics"} <= set(wallets[0])
    addr = wallets[0]["address"]
    detail = (await cl.get(f"/api/wallets/{addr}")).json()
    assert detail["wallet"]["address"] == addr and "flags" in detail and "metrics" in detail
    r = await cl.patch(f"/api/wallets/{addr}", json={"list_type": "blacklist"}, headers=h)
    assert r.status_code == 200
    w = next(x for x in (await cl.get("/api/wallets")).json() if x["address"] == addr)
    assert w["list_type"] == "blacklist" and not w["selected"]
    r = await cl.post("/api/wallets", json={"address": "x" * 10}, headers=h)
    assert r.status_code == 422
    r = await cl.patch(f"/api/wallets/{addr}", json={"list_type": "friends"}, headers=h)
    assert r.status_code == 400
    assert (await cl.get("/api/wallets/unknown-wallet-address-00000000000")).status_code == 404


async def test_logout_invalidates_session(client):
    cl, _ = client
    csrf = await _login(cl)
    assert (await cl.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})).status_code == 200
    assert (await cl.get("/api/overview")).status_code == 401
