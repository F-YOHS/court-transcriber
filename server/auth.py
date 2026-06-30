"""
Auth: bcrypt пароль + JWT в HttpOnly cookie.

Если AUTH_PASSWORD_HASH пустой — middleware пропускает всех (dev-режим).
Иначе все эндпоинты кроме /health, /login и /api/login требуют валидный JWT.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from jose import JWTError, jwt
import bcrypt

from server.config import settings

ALGO = "HS256"
COOKIE_NAME = "ct_session"

_PUBLIC_PREFIXES = ("/health", "/login", "/api/login", "/api/logout", "/favicon")
_PUBLIC_STATIC_FILES = {"/static/login.html", "/static/style.css"}

# /text и /speakers — обычные защищённые страницы, не добавляем в whitelist


def auth_enabled() -> bool:
    return bool(settings.auth_password_hash and settings.jwt_secret)


def verify_credentials(username: str, password: str) -> bool:
    if username != settings.auth_username:
        return False
    if not settings.auth_password_hash:
        return False
    try:
        # bcrypt учитывает только первые 72 байта; режем сами, иначе bcrypt 4.x
        # бросает ValueError на более длинном пароле (а не молча усекает).
        return bcrypt.checkpw(
            password.encode("utf-8")[:72],
            settings.auth_password_hash.encode("utf-8"),
        )
    except (ValueError, TypeError):
        return False


def issue_token(username: str) -> str:
    payload = {
        "sub": username,
        "iat": datetime.now(timezone.utc),
        "exp": datetime.now(timezone.utc) + timedelta(hours=settings.jwt_ttl_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGO)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[ALGO])
    except JWTError:
        return None


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=False,
        max_age=settings.jwt_ttl_hours * 3600,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/")


def _is_public_path(path: str) -> bool:
    if path in _PUBLIC_STATIC_FILES:
        return True
    return any(path == p or path.startswith(p + "/") or path == p for p in _PUBLIC_PREFIXES)


async def auth_middleware(request: Request, call_next):
    if not auth_enabled():
        return await call_next(request)

    if _is_public_path(request.url.path):
        return await call_next(request)

    token = request.cookies.get(COOKIE_NAME)
    if token and decode_token(token):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"detail": "Не авторизован"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )
    return RedirectResponse(url="/login", status_code=302)
