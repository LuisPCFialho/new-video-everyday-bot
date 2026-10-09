"""Validade e renovação do token de acesso do Meta.

- Token de System User (recomendado): não expira; só se confirma que é válido.
- Token de utilizador de longa duração (~60 dias): quando faltam menos de
  `token_refresh_days`, troca-se por um novo e guarda-se em state/token.json
  no bucket (partilhado entre o PC e o GitHub Actions).

Só se considera o token inválido com uma resposta clara do Meta; erros de
rede ou 5xx na verificação não bloqueiam a publicação.
"""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx

from .config import Secrets, Settings
from .store import BucketStore

log = logging.getLogger(__name__)

TRIES = 3


class TokenError(RuntimeError):
    pass


def current_token(secrets: Secrets, store: BucketStore) -> str:
    """O token renovado no bucket tem prioridade sobre o do ambiente."""
    raw = store.read_text(store.token_key)
    if raw:
        saved = json.loads(raw)
        if saved.get("source_token_tail") == secrets.access_token[-8:]:
            return saved["access_token"]
    return secrets.access_token


def _get(http: httpx.Client, url: str, params: dict, sleep: Callable[[float], None]) -> httpx.Response | None:
    """GET com retry em erros de rede/5xx. None se o Meta não respondeu."""
    for attempt in range(1, TRIES + 1):
        try:
            resp = http.get(url, params=params)
            if resp.status_code < 500:
                return resp
            reason = f"HTTP {resp.status_code}"
        except httpx.TransportError as exc:
            reason = type(exc).__name__
        if attempt < TRIES:
            log.warning("Verificação do token: %s; nova tentativa %d/%d", reason, attempt + 1, TRIES)
            sleep(2.0 * 2 ** (attempt - 1))
    return None


def ensure_token(secrets: Secrets, settings: Settings, store: BucketStore, http: httpx.Client | None = None,
                 now: datetime | None = None, sleep: Callable[[float], None] = time.sleep) -> str:
    token = current_token(secrets, store)
    if not (secrets.app_id and secrets.app_secret):
        log.warning("META_APP_ID/META_APP_SECRET em falta: não verifico a validade do token.")
        return token
    http = http or httpx.Client(timeout=30)
    now = now or datetime.now(UTC)
    base = f"https://graph.facebook.com/{settings.graph_api_version}"

    resp = _get(http, f"{base}/debug_token",
                {"input_token": token, "access_token": f"{secrets.app_id}|{secrets.app_secret}"}, sleep)
    if resp is None or resp.status_code != 200:
        log.warning("Não consegui verificar o token (%s); continuo com o actual.",
                    "sem resposta" if resp is None else f"HTTP {resp.status_code}")
        return token
    data = resp.json().get("data", {})
    if not data.get("is_valid"):
        raise TokenError("Token do Meta inválido ou revogado. Corre `setup` de novo e `gh secret set -f .env`.")
    expires_at = int(data.get("expires_at") or 0)
    if expires_at == 0:
        return token
    days_left = (expires_at - now.timestamp()) / 86400
    if days_left > settings.token_refresh_days:
        log.info("Token válido por mais %.0f dias.", days_left)
        return token
    if data.get("type") != "USER":
        raise TokenError(f"Token expira em {days_left:.0f} dias e não é renovável automaticamente.")

    log.info("Token expira em %.0f dias: a renovar.", days_left)
    resp = _get(http, f"{base}/oauth/access_token", {
        "grant_type": "fb_exchange_token",
        "client_id": secrets.app_id,
        "client_secret": secrets.app_secret,
        "fb_exchange_token": token,
    }, sleep)
    new_token = resp.json().get("access_token") if resp is not None and resp.status_code == 200 else None
    if not new_token:
        raise TokenError(f"Renovação do token falhou. Expira em {days_left:.0f} dias: "
                         "corre `setup` (de preferência com um token de System User).")
    store.write_text(store.token_key, json.dumps({
        "access_token": new_token,
        "source_token_tail": secrets.access_token[-8:],
        "refreshed_at": now.isoformat(),
    }), "application/json")
    log.info("Token renovado e guardado no bucket.")
    return new_token
