"""Leitura de config.toml (parâmetros) e do ambiente/.env (segredos)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"
QUEUE_DIR = ROOT / "queue"
LOG_DIR = ROOT / "logs"

REQUIRED_SECRETS = (
    "META_ACCESS_TOKEN",
    "IG_USER_ID",
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
)


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Settings:
    timezone: str
    windows: tuple[tuple[str, str], ...]
    start_date: date | None
    slot_seed: str
    min_hours_between_posts: float
    publish_first: tuple[int, ...]
    source_dir: Path
    videos_subdir: str
    covers_subdir: str
    captions_file: str
    ig_username: str
    graph_api_version: str
    poll_interval_s: int
    poll_timeout_s: int
    story_teaser_seconds: float
    story_poll_timeout_s: int
    queue_prefix: str
    state_key: str
    token_key: str
    presign_ttl_s: int
    max_consecutive_failures: int
    token_refresh_days: int
    log_keep_days: int


@dataclass(frozen=True)
class Secrets:
    access_token: str
    ig_user_id: str
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket: str
    app_id: str | None = None
    app_secret: str | None = None


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        sch, q, src, ig, st, bk, sf = (
            raw[k] for k in ("schedule", "queue", "source", "instagram", "story", "bucket", "safety")
        )
        windows = tuple((str(a), str(b)) for a, b in sch["windows"])
        for a, b in windows:
            if time.fromisoformat(a) >= time.fromisoformat(b):
                raise ValueError(f"janela {a}–{b}: o início tem de ser antes do fim")
        return Settings(
            timezone=sch["timezone"],
            windows=windows,
            start_date=date.fromisoformat(sch["start_date"]) if sch.get("start_date") else None,
            slot_seed=str(sch["slot_seed"]),
            min_hours_between_posts=float(sch["min_hours_between_posts"]),
            publish_first=tuple(int(n) for n in q.get("publish_first", [])),
            source_dir=Path(src["dir"]),
            videos_subdir=src["videos_subdir"],
            covers_subdir=src["covers_subdir"],
            captions_file=src["captions_file"],
            ig_username=ig["username"],
            graph_api_version=ig["graph_api_version"],
            poll_interval_s=int(ig["poll_interval_s"]),
            poll_timeout_s=int(ig["poll_timeout_s"]),
            story_teaser_seconds=float(st["teaser_seconds"]),
            story_poll_timeout_s=int(st["poll_timeout_s"]),
            queue_prefix=bk["queue_prefix"],
            state_key=bk["state_key"],
            token_key=bk["token_key"],
            presign_ttl_s=int(bk["presign_ttl_s"]),
            max_consecutive_failures=int(sf["max_consecutive_failures"]),
            token_refresh_days=int(sf["token_refresh_days"]),
            log_keep_days=int(sf["log_keep_days"]),
        )
    except (OSError, KeyError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"config.toml inválido ou em falta ({path}): {exc}") from exc


def load_env_file(path: Path = ENV_PATH) -> None:
    """Carrega o .env para os.environ sem sobrepor variáveis já definidas
    (no GitHub Actions os segredos vêm do ambiente)."""
    if path.exists():
        from dotenv import load_dotenv

        load_dotenv(path, override=False)


def load_secrets(env: dict[str, str] | None = None) -> Secrets:
    env = dict(os.environ) if env is None else env
    missing = [k for k in REQUIRED_SECRETS if not env.get(k, "").strip()]
    if missing:
        raise ConfigError(
            "Faltam segredos: " + ", ".join(missing) + ". Corre `python -m reels_bot setup` "
            "(local) ou define-os nos GitHub repo secrets (cloud)."
        )
    return Secrets(
        access_token=env["META_ACCESS_TOKEN"].strip(),
        ig_user_id=env["IG_USER_ID"].strip(),
        r2_account_id=env["R2_ACCOUNT_ID"].strip(),
        r2_access_key_id=env["R2_ACCESS_KEY_ID"].strip(),
        r2_secret_access_key=env["R2_SECRET_ACCESS_KEY"].strip(),
        r2_bucket=env["R2_BUCKET"].strip(),
        app_id=env.get("META_APP_ID", "").strip() or None,
        app_secret=env.get("META_APP_SECRET", "").strip() or None,
    )
