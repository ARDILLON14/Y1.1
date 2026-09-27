"""FastAPI dependencies: context, session and CSRF enforcement."""

from __future__ import annotations

from fastapi import HTTPException, Request

from copytrader.api.auth import CSRF_HEADER, SESSION_COOKIE, Authenticator, Session
from copytrader.api.server import ApiContext


def ctx(request: Request) -> ApiContext:
    return request.app.state.ctx  # type: ignore[no-any-return]


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def session(request: Request) -> Session:
    auth: Authenticator = ctx(request).auth
    sess = auth.sessions.get(request.cookies.get(SESSION_COOKIE))
    if sess is None:
        raise HTTPException(status_code=401, detail="no autenticado")
    return sess


def write_session(request: Request) -> Session:
    """Session + CSRF check, for every state-changing endpoint."""
    sess = session(request)
    if not Authenticator.csrf_ok(sess, request.headers.get(CSRF_HEADER)):
        raise HTTPException(status_code=403, detail="token CSRF inválido")
    return sess


async def reauth(request: Request, sess: Session, password: str | None, totp: str | None = None) -> None:
    """Sensitive actions require the password again."""
    if not password:
        raise HTTPException(status_code=403, detail="esta acción requiere confirmar la contraseña")
    auth: Authenticator = ctx(request).auth
    ip = client_ip(request)
    auth.guard.check(ip)
    try:
        await auth.verify(sess.username, password, totp)
    except Exception:
        auth.guard.failure(ip)
        raise HTTPException(status_code=403, detail="contraseña incorrecta") from None
    auth.guard.success(ip)
