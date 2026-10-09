"""`setup`: guia passo a passo para obter tokens/IDs do Meta e chaves do R2,
testa-os e grava o .env."""
from __future__ import annotations

import getpass
from collections.abc import Callable
from pathlib import Path

import httpx

from .config import ENV_PATH, Secrets, Settings

PERMISSIONS = (
    "instagram_basic",
    "instagram_content_publish",
    "pages_show_list",
    "pages_read_engagement",
    "business_management",
)

Ask = Callable[[str], str]

META_APP_STEPS = """
━━ 1/4 · App do Meta ━━
 1. Abre https://developers.facebook.com/apps → «Criar app».
 2. Caso de utilização: «Outro» → tipo de app: «Empresa» (Business).
    Associa-a ao teu portfólio empresarial (o mesmo de business.facebook.com).
 3. No painel da app, adiciona o produto «Instagram» e escolhe
    «API setup with Facebook login» (Instagram Graph API).
 4. Em Definições da app → Básicas, copia o «ID da app» e a «Chave secreta da app».
 A app pode ficar em modo de desenvolvimento: como és administrador dela,
 as permissões funcionam na tua própria conta sem App Review.
"""

TOKEN_STEPS = """
━━ 2/4 · Token de acesso ━━
 Opção A (recomendada, NÃO expira) — System User:
   business.facebook.com/settings → Utilizadores → Utilizadores do sistema →
   Adicionar (função Admin) → «Atribuir ativos»: a app (controlo total),
   a Página do Facebook e a conta de Instagram @{username} →
   «Gerar novo token» → escolhe a app → expiração «Nunca» → marca:
   {perms}
 Opção B (expira em ~60 dias, o bot renova sozinho):
   https://developers.facebook.com/tools/explorer → escolhe a app →
   «User or Page: Get User Access Token» → marca as mesmas permissões →
   «Generate Access Token» → copia o token (dura ~1 h; eu troco-o já por um de 60 dias).
"""

R2_STEPS = """
━━ 3/4 · Bucket Cloudflare R2 ━━
 1. https://dash.cloudflare.com → R2 Object Storage → «Create bucket»
    nome sugerido: new-video-everyday (localização: Automatic). NÃO o tornes público.
 2. R2 → «Manage API tokens» → «Create API token» → permissão
    «Object Read & Write», limitado a esse bucket → copia
    «Access Key ID», «Secret Access Key» e o «Account ID» (aparece no URL do endpoint
    https://<ACCOUNT_ID>.r2.cloudflarestorage.com).
"""


def _ask_secret(prompt: str) -> str:
    return getpass.getpass(prompt + " (o texto não aparece ao colar): ").strip()


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"')
    return values


def write_env(path: Path, updates: dict[str, str]) -> None:
    merged = {**read_env(path), **{k: v for k, v in updates.items() if v}}
    lines = ["# Gerado por `python -m reels_bot setup`. NUNCA fazer commit deste ficheiro."]
    lines += [f"{k}={v}" for k, v in sorted(merged.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def pick_instagram(pages: list[dict], username: str) -> tuple[dict, dict]:
    """Escolhe a Página cuja conta IG ligada é @username."""
    linked = [(p, p["instagram_business_account"]) for p in pages if p.get("instagram_business_account")]
    for page, ig in linked:
        if ig.get("username", "").lower() == username.lower():
            return page, ig
    found = ", ".join(f"@{ig.get('username')}" for _, ig in linked) or "nenhuma"
    raise RuntimeError(f"Não encontrei @{username} ligada a uma Página. Contas encontradas: {found}. "
                       "Confirma que a conta é profissional e está ligada à Página, e que o token tem acesso a ela.")


def _graph(http: httpx.Client, base: str, path: str, **params: str) -> dict:
    resp = http.get(f"{base}/{path}", params=params)
    payload = resp.json()
    if resp.status_code != 200 or "error" in payload:
        raise RuntimeError(f"Graph API: {payload.get('error', {}).get('message', resp.text[:200])}")
    return payload


def setup_meta(settings: Settings, ask: Ask, out: Callable[[str], None], http: httpx.Client) -> dict[str, str]:
    base = f"https://graph.facebook.com/{settings.graph_api_version}"
    out(META_APP_STEPS)
    app_id = ask("ID da app: ").strip()
    app_secret = _ask_secret("Chave secreta da app")
    out(TOKEN_STEPS.format(username=settings.ig_username, perms=", ".join(PERMISSIONS)))
    option = ask("Opção A ou B? [A]: ").strip().upper() or "A"
    token = _ask_secret("Cola o token")
    if option == "B":
        token = _graph(http, base, "oauth/access_token", grant_type="fb_exchange_token",
                       client_id=app_id, client_secret=app_secret, fb_exchange_token=token)["access_token"]
        out("✔ Token trocado por um de longa duração (~60 dias).")

    try:
        granted = {p["permission"] for p in _graph(http, base, "me/permissions", access_token=token).get("data", [])
                   if p.get("status") == "granted"}
        missing = [p for p in PERMISSIONS if p not in granted]
        if missing:
            out(f"⚠ Permissões em falta no token: {', '.join(missing)} — gera-o de novo com elas marcadas.")
    except RuntimeError as exc:
        out(f"⚠ Não consegui listar as permissões do token ({exc}); continuo.")
    pages = _graph(http, base, "me/accounts", access_token=token,
                   fields="id,name,access_token,instagram_business_account{id,username}").get("data", [])
    page, ig = pick_instagram(pages, settings.ig_username)
    out(f"✔ Página «{page['name']}» → Instagram @{ig['username']} (IG_USER_ID {ig['id']})")
    return {
        "META_APP_ID": app_id,
        "META_APP_SECRET": app_secret,
        "META_ACCESS_TOKEN": token,
        "META_PAGE_ACCESS_TOKEN": page.get("access_token", ""),
        "IG_USER_ID": ig["id"],
    }


def setup_r2(settings: Settings, ask: Ask, out: Callable[[str], None], http: httpx.Client) -> dict[str, str]:
    from .store import BucketStore

    out(R2_STEPS)
    values = {
        "R2_ACCOUNT_ID": ask("Account ID: ").strip(),
        "R2_BUCKET": ask("Nome do bucket [new-video-everyday]: ").strip() or "new-video-everyday",
        "R2_ACCESS_KEY_ID": ask("Access Key ID: ").strip(),
        "R2_SECRET_ACCESS_KEY": _ask_secret("Secret Access Key"),
    }
    secrets = Secrets(access_token="-", ig_user_id="-", r2_account_id=values["R2_ACCOUNT_ID"],
                      r2_access_key_id=values["R2_ACCESS_KEY_ID"],
                      r2_secret_access_key=values["R2_SECRET_ACCESS_KEY"], r2_bucket=values["R2_BUCKET"])
    store = BucketStore.from_secrets(secrets, settings)
    key = "setup-test.txt"
    store.write_text(key, "ok")
    status = http.get(store.presign(key, 300)).status_code
    store.delete(key)
    if status != 200:
        raise RuntimeError(f"O URL pré-assinado do R2 devolveu HTTP {status}")
    out("✔ R2 testado: escrita, URL pré-assinado público (HTTPS) e remoção OK.")
    return values


def run_setup(settings: Settings, ask: Ask = input, out: Callable[[str], None] = print,
              env_path: Path = ENV_PATH) -> None:
    out("Configuração do publicador de Reels — podes repetir isto quando quiseres.")
    with httpx.Client(timeout=30) as http:
        if ask("Configurar Meta/Instagram agora? [S/n]: ").strip().lower() != "n":
            write_env(env_path, setup_meta(settings, ask, out, http))
            out(f"✔ Gravado em {env_path.name}")
        if ask("Configurar o bucket R2 agora? [S/n]: ").strip().lower() != "n":
            write_env(env_path, setup_r2(settings, ask, out, http))
            out(f"✔ Gravado em {env_path.name}")
    out("""
━━ 4/4 · GitHub Actions ━━
 Envia os segredos para o repositório (uma vez, e sempre que mudares o .env):
     gh secret set -f .env
 O .env está no .gitignore: nunca é enviado para o GitHub.""")
