"""Lightweight static-token auth.

A single shared token (env `TRANSCRIPTOR_API_TOKEN`) gates the protected endpoints,
accepted as either `Authorization: Bearer <token>` or `X-API-Key: <token>`. This is
deliberately not a user system — it demonstrates access control without scope creep;
the README notes the path to real auth (OIDC/mTLS). If no token is configured, auth
is disabled for local dev convenience.
"""

from __future__ import annotations

import secrets

from fastapi import Header, HTTPException, status

from app.config import get_settings


async def require_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    settings = get_settings()
    if not settings.auth_enabled:
        return

    presented = x_api_key
    if not presented and authorization and authorization.lower().startswith("bearer "):
        presented = authorization[7:].strip()

    if not presented or not secrets.compare_digest(presented, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
