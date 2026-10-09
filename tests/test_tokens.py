import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from reels_bot.config import Secrets
from reels_bot.tokens import TokenError, current_token, ensure_token

NOW = datetime(2026, 10, 9, tzinfo=UTC)
SECRETS = Secrets(access_token="OLD-TOKEN-12345678", ig_user_id="IG", r2_account_id="a", r2_access_key_id="k",
                  r2_secret_access_key="s", r2_bucket="b", app_id="APP", app_secret="SECRET")


def http(debug: dict, exchange: dict | None = None, status=200):
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/debug_token"):
            return httpx.Response(status, json={"data": debug})
        return httpx.Response(200 if exchange else 400, json=exchange or {"error": {"message": "no"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.calls = calls
    return client


def test_never_expiring_token_is_kept(settings, store):
    c = http({"is_valid": True, "expires_at": 0, "type": "SYSTEM_USER"})
    assert ensure_token(SECRETS, settings, store, http=c, now=NOW, sleep=lambda s: None) == SECRETS.access_token
    assert len(c.calls) == 1


def test_token_far_from_expiry_is_kept(settings, store):
    exp = int((NOW + timedelta(days=40)).timestamp())
    assert ensure_token(SECRETS, settings, store, http=http({"is_valid": True, "expires_at": exp, "type": "USER"}),
                        now=NOW, sleep=lambda s: None) == SECRETS.access_token


def test_near_expiry_user_token_is_refreshed_and_persisted(settings, store):
    exp = int((NOW + timedelta(days=5)).timestamp())
    c = http({"is_valid": True, "expires_at": exp, "type": "USER"}, {"access_token": "NEW"})
    assert ensure_token(SECRETS, settings, store, http=c, now=NOW, sleep=lambda s: None) == "NEW"
    saved = json.loads(store.read_text(settings.token_key))
    assert saved["access_token"] == "NEW"
    assert current_token(SECRETS, store) == "NEW"
    other = Secrets(**{**SECRETS.__dict__, "access_token": "BRAND-NEW-FROM-SETUP"})
    assert current_token(other, store) == "BRAND-NEW-FROM-SETUP"  # setup novo ignora o token antigo guardado


def test_refresh_failure_raises(settings, store):
    exp = int((NOW + timedelta(days=2)).timestamp())
    with pytest.raises(TokenError, match="Renovação"):
        ensure_token(SECRETS, settings, store, http=http({"is_valid": True, "expires_at": exp, "type": "USER"}), now=NOW, sleep=lambda s: None)


def test_near_expiry_page_token_cannot_refresh(settings, store):
    exp = int((NOW + timedelta(days=2)).timestamp())
    with pytest.raises(TokenError, match="não é renovável"):
        ensure_token(SECRETS, settings, store, http=http({"is_valid": True, "expires_at": exp, "type": "PAGE"}), now=NOW, sleep=lambda s: None)


def test_invalid_token_raises(settings, store):
    with pytest.raises(TokenError, match="inválido"):
        ensure_token(SECRETS, settings, store, http=http({"is_valid": False}), now=NOW, sleep=lambda s: None)


def test_without_app_credentials_skips_check(settings, store):
    secrets = Secrets(**{**SECRETS.__dict__, "app_id": None, "app_secret": None})
    assert ensure_token(secrets, settings, store, http=http({}), now=NOW, sleep=lambda s: None) == SECRETS.access_token


def test_debug_token_5xx_or_network_does_not_block(settings, store):
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ConnectError("x")
        return httpx.Response(503, text="down")

    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert ensure_token(SECRETS, settings, store, http=c, now=NOW, sleep=lambda s: None) == SECRETS.access_token
    assert len(calls) == 3


def test_debug_token_400_does_not_mark_invalid(settings, store):
    c = http({"is_valid": False}, status=400)
    assert ensure_token(SECRETS, settings, store, http=c, now=NOW, sleep=lambda s: None) == SECRETS.access_token
