"""Login / logout / current session."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from copytrader.api.auth import SESSION_COOKIE, Session
from copytrader.api.deps import client_ip, ctx, session, write_session
from copytrader.core.errors import AuthError
from copytrader.db.repositories import AuditRepo

router = APIRouter(tags=["auth"])


class LoginBody(BaseModel):
    username: str = Field("admin", max_length=64)
    password: str = Field(max_length=256)
    totp: str | None = Field(None, max_length=12)


@router.post("/auth/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, object]:
    c = ctx(request)
    ip = client_ip(request)
    c.auth.guard.check(ip)
    if not await c.auth.any_user():
        raise HTTPException(status_code=409,
                            detail="no hay usuario: ejecuta 'copytrader set-password' en el servidor")
    try:
        await c.auth.verify(body.username, body.password, body.totp)
    except AuthError:
        c.auth.guard.failure(ip)
        async with c.container.db.session() as s:
            await AuditRepo(s).add(body.username[:64], "login_failed", None, {}, ip)
        raise
    c.auth.guard.success(ip)
    sess = c.auth.sessions.create(body.username, ip)
    cfg = c.container.cfg.api
    response.set_cookie(SESSION_COOKIE, sess.id, httponly=True, secure=cfg.secure_cookies, samesite="strict",
                        max_age=cfg.session_ttl_minutes * 60, path="/")
    async with c.container.db.session() as s:
        await AuditRepo(s).add(body.username, "login", None, {}, ip)
    return {"username": sess.username, "csrf": sess.csrf}


@router.post("/auth/logout")
async def logout(request: Request, response: Response, sess: Session = Depends(write_session)) -> dict[str, bool]:
    ctx(request).auth.sessions.revoke(sess.id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(sess: Session = Depends(session)) -> dict[str, object]:
    return {"username": sess.username, "csrf": sess.csrf, "expires": sess.expires}
