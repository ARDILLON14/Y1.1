"""Signer service: the only process that ever decrypts the bot's private key.

Run it in its own container on an internal network without Internet access::

    python -m copytrader.signer_service

Environment (secrets preferably via ``*_FILE`` / Docker secrets):

* ``SIGNER_KEYSTORE_PATH``      encrypted keystore (default /run/secrets/bot_keystore)
* ``KEYSTORE_PASSPHRASE[_FILE]``
* ``SIGNER_HMAC_KEY[_FILE]`` and optional ``SIGNER_HMAC_KEY_PREVIOUS[_FILE]`` (rotation)
* ``SIGNER_MAX_NOTIONAL_USD_PER_TX`` / ``..._PER_DAY`` / ``SIGNER_MAX_TX_PER_MINUTE``
* ``SIGNER_MAX_PRIORITY_FEE_LAMPORTS`` / ``SIGNER_MAX_TIP_LAMPORTS``
* ``SIGNER_STATE_FILE``         persisted daily counters
* ``SIGNER_HOST`` / ``SIGNER_PORT``
"""

from __future__ import annotations

import base64
import binascii
import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from copytrader.core.errors import AuthError, SecurityError, SignerPolicyViolation
from copytrader.observability.logging import configure_logging
from copytrader.security.hmac_auth import HmacVerifier
from copytrader.security.keystore import load_keypair
from copytrader.security.redaction import REDACTOR
from copytrader.security.signer import sign_with_keypair
from copytrader.security.signer_policy import SignerLimits, SignerPolicy, SignIntent

log = structlog.get_logger("signer")
MAX_BODY = 64 * 1024


def _secret(name: str, required: bool = True) -> str | None:
    path = os.environ.get(f"{name}_FILE")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    value = os.environ.get(name)
    if required and not value:
        raise SystemExit(f"missing required secret {name}")
    return value


def create_signer_app(keypair: Any, policy: SignerPolicy, verifier: HmacVerifier) -> FastAPI:
    app = FastAPI(title="copytrader-signer", docs_url=None, redoc_url=None, openapi_url=None)
    pubkey = str(keypair.pubkey())

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        if request.url.path == "/healthz":
            return await call_next(request)
        body = await request.body()
        if len(body) > MAX_BODY:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        try:
            verifier.verify(request.method, request.url.path, body, dict(request.headers))
        except AuthError as exc:
            log.warning("signer_auth_failed", reason=str(exc), client=request.client.host if request.client else None)
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/pubkey")
    async def get_pubkey() -> dict[str, str]:
        return {"public_key": pubkey}

    @app.post("/v1/sign")
    async def sign(request: Request) -> dict[str, str]:
        payload = await request.json()
        try:
            intent = SignIntent.from_dict(payload["intent"])
            tx_bytes = base64.b64decode(payload["tx"], validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error) as exc:
            raise HTTPException(status_code=400, detail="bad request") from exc
        try:
            signed = sign_with_keypair(keypair, tx_bytes, policy, intent)
        except SignerPolicyViolation as exc:
            log.error("signer_policy_violation", order=intent.client_order_id, violations=str(exc))
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (SecurityError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="cannot parse transaction") from exc
        log.info(
            "signed",
            order=intent.client_order_id,
            purpose=intent.purpose,
            notional_usd=round(intent.notional_usd, 2),
            signature=signed.signature,
        )
        return {"tx": base64.b64encode(signed.tx_bytes).decode(), "signature": signed.signature}

    return app


def main() -> None:
    configure_logging(os.environ.get("SIGNER_LOG_LEVEL", "INFO"), json_logs=True)
    passphrase = _secret("KEYSTORE_PASSPHRASE")
    hmac_key = _secret("SIGNER_HMAC_KEY")
    hmac_prev = _secret("SIGNER_HMAC_KEY_PREVIOUS", required=False)
    assert passphrase and hmac_key
    keypair = load_keypair(os.environ.get("SIGNER_KEYSTORE_PATH", "/run/secrets/bot_keystore"), passphrase)
    REDACTOR.register([passphrase, hmac_key, hmac_prev, str(keypair)])
    limits = SignerLimits(
        max_notional_usd_per_tx=float(os.environ.get("SIGNER_MAX_NOTIONAL_USD_PER_TX", "100")),
        max_notional_usd_per_day=float(os.environ.get("SIGNER_MAX_NOTIONAL_USD_PER_DAY", "1000")),
        max_tx_per_minute=int(os.environ.get("SIGNER_MAX_TX_PER_MINUTE", "20")),
        max_priority_fee_lamports=int(os.environ.get("SIGNER_MAX_PRIORITY_FEE_LAMPORTS", "5000000")),
        max_tip_lamports=int(os.environ.get("SIGNER_MAX_TIP_LAMPORTS", "5000000")),
        state_file=os.environ.get("SIGNER_STATE_FILE", "/data/signer_state.json"),
    )
    policy = SignerPolicy(owner=str(keypair.pubkey()), limits=limits)
    keys = [hmac_key.encode()] + ([hmac_prev.encode()] if hmac_prev else [])
    verifier = HmacVerifier(keys=keys, max_skew_seconds=float(os.environ.get("SIGNER_MAX_SKEW", "30")))
    app = create_signer_app(keypair, policy, verifier)
    log.info(
        "signer_started",
        public_key=str(keypair.pubkey()),
        max_per_tx=limits.max_notional_usd_per_tx,
        max_per_day=limits.max_notional_usd_per_day,
    )

    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("SIGNER_HOST", "0.0.0.0"),  # noqa: S104 (internal network)
        port=int(os.environ.get("SIGNER_PORT", "8700")),
        log_level="warning",
        access_log=False,
    )
