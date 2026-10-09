"""Bucket Cloudflare R2 (API S3): fila, state.json e URLs pré-assinados.

Layout no bucket:
    queue/<slug>/video.mp4 | cover.jpg | caption.txt | meta.json
    state/state.json
    state/token.json   (só se o token de utilizador for renovado automaticamente)
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from .config import Secrets, Settings
from .importer import ITEM_DIR_RE, slug_sort_key
from .state import State, is_done, normalize_state

log = logging.getLogger(__name__)

CONTENT_TYPES = {
    "video.mp4": "video/mp4",
    "cover.jpg": "image/jpeg",
    "story.mp4": "video/mp4",
    "caption.txt": "text/plain; charset=utf-8",
    "meta.json": "application/json",
}
MEDIA_FILES = ("video.mp4", "cover.jpg", "story.mp4")
TEXT_FILES = ("caption.txt", "meta.json")


@dataclass(frozen=True)
class QueueItem:
    slug: str
    title: str
    caption: str
    meta: dict
    has_video: bool
    has_cover: bool
    has_story: bool


@dataclass
class PushReport:
    uploaded: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    skipped_posted: list[str] = field(default_factory=list)


def _not_found(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound")


class BucketStore:
    def __init__(self, client: Any, bucket: str, settings: Settings):
        self.client = client
        self.bucket = bucket
        self.prefix = settings.queue_prefix
        self.state_key = settings.state_key
        self.token_key = settings.token_key
        self.lock_key = settings.state_key.rsplit("/", 1)[0] + "/lock.json"

    @classmethod
    def from_secrets(cls, secrets: Secrets, settings: Settings) -> BucketStore:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "s3",
            endpoint_url=f"https://{secrets.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=secrets.r2_access_key_id,
            aws_secret_access_key=secrets.r2_secret_access_key,
            region_name="auto",
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )
        return cls(client, secrets.r2_bucket, settings)

    # --- objectos -----------------------------------------------------------
    def key(self, slug: str, name: str) -> str:
        return f"{self.prefix}{slug}/{name}"

    def read_text(self, key: str) -> str | None:
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            return body.read().decode("utf-8")
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise

    def write_text(self, key: str, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=text.encode("utf-8"), ContentType=content_type)

    def head(self, key: str) -> dict | None:
        try:
            resp = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            if _not_found(exc):
                return None
            raise
        return {"size": resp["ContentLength"], "mtime": resp.get("Metadata", {}).get("src-mtime")}

    def upload_file(self, path: Path, key: str, content_type: str) -> None:
        self.client.upload_file(
            str(path), self.bucket, key,
            ExtraArgs={"ContentType": content_type, "Metadata": {"src-mtime": str(int(path.stat().st_mtime))}},
        )

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def presign(self, key: str, ttl_s: int) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=ttl_s
        )

    def _list(self, prefix: str, delimiter: str | None = None) -> list[dict]:
        pages, token = [], None
        while True:
            kwargs = {"Bucket": self.bucket, "Prefix": prefix}
            if delimiter:
                kwargs["Delimiter"] = delimiter
            if token:
                kwargs["ContinuationToken"] = token
            resp = self.client.list_objects_v2(**kwargs)
            pages.append(resp)
            if not resp.get("IsTruncated"):
                return pages
            token = resp["NextContinuationToken"]

    # --- fila ---------------------------------------------------------------
    def _queue_keys(self) -> set[str]:
        return {obj["Key"] for page in self._list(self.prefix) for obj in page.get("Contents", [])}

    def list_slugs(self, keys: set[str] | None = None) -> list[str]:
        keys = self._queue_keys() if keys is None else keys
        slugs = {k[len(self.prefix):].split("/", 1)[0] for k in keys if "/" in k[len(self.prefix):]}
        return sorted((s for s in slugs if ITEM_DIR_RE.match(s)), key=slug_sort_key)

    def delete_item(self, slug: str) -> None:
        for page in self._list(f"{self.prefix}{slug}/"):
            for obj in page.get("Contents", []):
                self.delete(obj["Key"])

    def load_items(self, skip: Callable[[str], bool] = lambda slug: False) -> list[QueueItem]:
        """Uma só listagem do bucket + 2 leituras por item não ignorado (`skip`)."""
        keys = self._queue_keys()
        items = []
        for slug in self.list_slugs(keys):
            if skip(slug):
                continue
            meta = json.loads(self.read_text(self.key(slug, "meta.json")) or "{}")
            items.append(QueueItem(
                slug=slug,
                title=meta.get("title", slug),
                caption=(self.read_text(self.key(slug, "caption.txt")) or "").strip(),
                meta=meta,
                has_video=self.key(slug, "video.mp4") in keys,
                has_cover=self.key(slug, "cover.jpg") in keys,
                has_story=self.key(slug, "story.mp4") in keys,
            ))
        return items

    # --- estado -------------------------------------------------------------
    def load_state(self) -> State:
        raw = self.read_text(self.state_key)
        return normalize_state(json.loads(raw) if raw else None)

    def save_state(self, state: State) -> None:
        self.write_text(self.state_key, json.dumps(state, ensure_ascii=False, indent=2), "application/json")


def push_queue(store: BucketStore, queue_dir: Path) -> PushReport:
    """Espelha queue/ local → bucket. Itens já publicados não são tocados;
    itens por publicar que já não existem localmente são removidos do bucket."""
    report = PushReport()
    state = store.load_state()
    local = sorted(
        (d.name for d in queue_dir.iterdir() if d.is_dir() and ITEM_DIR_RE.match(d.name)),
        key=slug_sort_key,
    ) if queue_dir.exists() else []
    if not local:
        raise ValueError(f"{queue_dir} está vazia: corre `import` primeiro (nada foi alterado no bucket).")

    for slug in store.list_slugs():
        if slug not in local and not is_done(state, slug):
            store.delete_item(slug)
            report.removed.append(slug)

    for slug in local:
        if is_done(state, slug):
            report.skipped_posted.append(slug)
            continue
        folder = queue_dir / slug
        for name in TEXT_FILES:
            store.upload_file(folder / name, store.key(slug, name), CONTENT_TYPES[name])
        for name in MEDIA_FILES:
            path, key = folder / name, store.key(slug, name)
            remote = store.head(key)
            if not path.exists():
                if remote:
                    store.delete(key)
                continue
            st = path.stat()
            if remote and remote["size"] == st.st_size and remote["mtime"] == str(int(st.st_mtime)):
                continue
            log.info("A enviar %s/%s (%.0f MB)", slug, name, st.st_size / 1024**2)
            store.upload_file(path, key, CONTENT_TYPES[name])
            report.uploaded.append(f"{slug}/{name}")
    return report
