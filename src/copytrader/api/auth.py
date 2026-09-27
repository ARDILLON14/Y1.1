"""Authentication, sessions, CSRF and request rate limiting for the dashboard API.

* Single operator account; password hashed with scrypt; optional TOTP 2FA.
* Server-side sessions (random 256-bit id in an HttpOnly, SameSite=Strict
  cookie). Restarting the process logs everyone out — acceptable and safer.
* CSRF: every mutating request must carry ``X-CSRF-Token`` equal to the
  session's token (double submit bound to the session).
* Brute force: per-IP failure counter with temporary lockout + constant-time
  password verification.
* Sensitive actions (going live, arming, disabling the global kill switch)
  require the password again in the request body (re-authentication).
"""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from copytrader.config.models import AppConfig
from copytrader.core.errors import AuthError
from copytrader.db.base import Database
from copytrader.db.repositories import UserRepo
from copytrader.security.passwords import FieldCipher, verify_password, verify_totp

SESSION_COOKIE = "ct_session"
CSRF_HEADER = "x-csrf-token"


@dataclass
class Session:
    id: str
    username: str
    csrf: str
    created: float
    expires: float
    ip: str | None
    last_seen: float = field(default_factory=time.time)


class SessionStore:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._sessions: dict[str, Session] = {}

    def create(self, username: str, ip: str | None) -> Session:
        now = time.time()
        sess = Session(
            id=secrets.token_urlsafe(32),
            username=username,
            csrf=secrets.token_urlsafe(24),
            created=now,
            expires=now + self.ttl,
            ip=ip,
        )
        self._sessions[sess.id] = sess
        self._gc(now)
        return sess

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        sess = self._sessions.get(session_id)
        now = time.time()
        if sess is None or sess.expires < now:
            self._sessions.pop(session_id or "", None)
            return None
        sess.last_seen = now
        return sess

    def revoke(self, session_id: str | None) -> None:
        if session_id:
            self._sessions.pop(session_id, None)

    def revoke_all(self) -> None:
        self._sessions.clear()

    def _gc(self, now: float) -> None:
        for sid in [s for s, v in self._sessions.items() if v.expires < now]:
            self._sessions.pop(sid, None)


class LoginGuard:
    """Per-IP failed-login tracking with lockout."""

    def __init__(self, config: Callable[[], AppConfig]) -> None:
        self._config = config
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def check(self, ip: str) -> None:
        until = self._locked_until.get(ip, 0.0)
        if until > time.time():
            raise AuthError(f"demasiados intentos; espera {int(until - time.time())}s")

    def failure(self, ip: str) -> None:
        cfg = self._config().api
        now = time.time()
        window = cfg.login_lockout_minutes * 60
        attempts = [t for t in self._failures.get(ip, []) if now - t < window] + [now]
        self._failures[ip] = attempts
        if len(attempts) >= cfg.login_max_attempts:
            self._locked_until[ip] = now + window
            self._failures[ip] = []

    def success(self, ip: str) -> None:
        self._failures.pop(ip, None)
        self._locked_until.pop(ip, None)


class Authenticator:
    def __init__(self, db: Database, config: Callable[[], AppConfig], cipher: FieldCipher) -> None:
        self.db = db
        self._config = config
        self.cipher = cipher
        self.sessions = SessionStore(config().api.session_ttl_minutes * 60)
        self.guard = LoginGuard(config)
        self._dummy_hash = "scrypt$32768$8$1$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

    async def verify(self, username: str, password: str, totp: str | None = None) -> None:
        async with self.db.session() as s:
            user = await UserRepo(s).get(username)
            stored = user.password_hash if user else self._dummy_hash
            totp_enc = user.totp_secret_enc if user else None
        ok = verify_password(password, stored) and user is not None  # always hash: no user enumeration timing
        if not ok:
            raise AuthError("credenciales inválidas")
        if totp_enc or self._config().api.require_totp:
            if not totp_enc or not totp:
                raise AuthError("código TOTP requerido")
            if not verify_totp(self.cipher.decrypt(totp_enc), totp):
                raise AuthError("código TOTP inválido")

    async def any_user(self) -> bool:
        async with self.db.session() as s:
            return await UserRepo(s).any_user()

    @staticmethod
    def csrf_ok(session: Session, header_value: str | None) -> bool:
        return bool(header_value) and hmac.compare_digest(session.csrf, header_value or "")
