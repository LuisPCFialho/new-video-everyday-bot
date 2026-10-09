"""Cliente mínimo da Graph API (Instagram), com retries limitados.

Por segurança só permite POST para criar contentores (`/media`) e publicá-los
(`/media_publish`): não há forma de apagar ou editar publicações existentes.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator

import httpx

log = logging.getLogger(__name__)

ALLOWED_POST_SUFFIXES = ("/media", "/media_publish")
TRANSIENT_CODES = {1, 2}  # "unknown error" / "service temporarily unavailable"


class GraphError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: int | None = None,
                 subcode: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status, self.code, self.subcode, self.transient = status, code, subcode, transient


def _decode(resp: httpx.Response) -> dict:
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    if resp.status_code < 400 and isinstance(payload, dict) and "error" not in payload:
        return payload
    err = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
    code = err.get("code")
    raise GraphError(
        f"Graph API {resp.status_code}: {err.get('message', resp.text[:200])} "
        f"(code={code}, subcode={err.get('error_subcode')})",
        status=resp.status_code,
        code=code,
        subcode=err.get("error_subcode"),
        transient=resp.status_code >= 500 or bool(err.get("is_transient")) or code in TRANSIENT_CODES,
    )


class GraphClient:
    def __init__(self, token: str, version: str, http: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_tries: int = 3, backoff_s: float = 2.0):
        self.base = f"https://graph.facebook.com/{version}"
        self.http = http or httpx.Client(timeout=60)
        self.headers = {"Authorization": f"Bearer {token}"}
        self.sleep, self.max_tries, self.backoff_s = sleep, max_tries, backoff_s

    def _request(self, method: str, url: str, *, params: dict | None = None,
                 data: dict | None = None, retry: bool = True) -> dict:
        tries = self.max_tries if retry else 1
        for attempt in range(1, tries + 1):
            try:
                return _decode(self.http.request(method, url, params=params, data=data, headers=self.headers))
            except httpx.TransportError as exc:
                error = GraphError(f"erro de rede: {exc.__class__.__name__}", transient=True)
            except GraphError as exc:
                error = exc
            if not error.transient or attempt == tries:
                raise error
            delay = self.backoff_s * 2 ** (attempt - 1)
            log.warning("Erro transitório (%s); nova tentativa %d/%d em %.0f s", error, attempt + 1, tries, delay)
            self.sleep(delay)
        raise AssertionError("inalcançável")

    def get(self, path: str, **params: str) -> dict:
        return self._request("GET", f"{self.base}/{path}", params=params)

    def post(self, path: str, data: dict, *, retry: bool = True) -> dict:
        if not path.endswith(ALLOWED_POST_SUFFIXES):
            raise PermissionError(f"POST não permitido: {path}")
        return self._request("POST", f"{self.base}/{path}", data=data, retry=retry)

    def paginate(self, path: str, **params: str) -> Iterator[dict]:
        url: str | None = f"{self.base}/{path}"
        query: dict | None = params
        while url:
            payload = self._request("GET", url, params=query)
            yield from payload.get("data", [])
            url = payload.get("paging", {}).get("next")
            query = None  # o URL "next" já traz os parâmetros
