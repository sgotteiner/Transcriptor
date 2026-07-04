"""Static-token auth dependency."""

import pytest
from fastapi import HTTPException

import app.auth as auth
from app.config import Settings


@pytest.fixture
def with_token(monkeypatch):
    settings = Settings(api_token="s3cret")
    monkeypatch.setattr(auth, "get_settings", lambda: settings)


async def test_missing_token_rejected(with_token):
    with pytest.raises(HTTPException) as exc:
        await auth.require_token(authorization=None, x_api_key=None)
    assert exc.value.status_code == 401


async def test_valid_token_accepted(with_token):
    assert await auth.require_token(authorization=None, x_api_key="s3cret") is None
    assert await auth.require_token(authorization="Bearer s3cret", x_api_key=None) is None


async def test_wrong_token_rejected(with_token):
    with pytest.raises(HTTPException):
        await auth.require_token(authorization=None, x_api_key="nope")


async def test_auth_disabled_when_no_token(monkeypatch):
    monkeypatch.setattr(auth, "get_settings", lambda: Settings(api_token=""))
    assert await auth.require_token(authorization=None, x_api_key=None) is None
