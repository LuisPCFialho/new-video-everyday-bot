from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from reels_bot.config import Settings
from reels_bot.graph import GraphClient
from reels_bot.publisher import Publisher
from reels_bot.store import BucketStore

NOW = datetime(2026, 10, 9, 20, 0, tzinfo=UTC)  # 21:00 em Lisboa (verão)
IG = "IG1"
VALID_PROBE = {"duration_s": 65.0, "width": 1080, "height": 1920, "size_bytes": 50_000_000,
               "video_codec": "h264", "audio_codec": "aac"}


def make_settings(**overrides) -> Settings:
    base = dict(
        timezone="Europe/Lisbon", publish_time="21:00", min_hours_between_posts=20,
        source_dir=Path("."), videos_subdir="Videos", covers_subdir="Covers", captions_file="Descriptions.txt",
        ig_username="new.video.everyday", graph_api_version="v23.0", poll_interval_s=15, poll_timeout_s=60,
        story_teaser_seconds=15, story_poll_timeout_s=60, queue_prefix="queue/", state_key="state/state.json",
        token_key="state/token.json", presign_ttl_s=7200, max_consecutive_failures=2,
        token_refresh_days=10, log_keep_days=30,
    )
    return Settings(**{**base, **overrides})


@pytest.fixture
def settings() -> Settings:
    return make_settings()


class FakeS3:
    """Cliente S3 mínimo em memória (o suficiente para BucketStore)."""

    def __init__(self, page_size: int = 1000):
        self.objects: dict[str, dict] = {}
        self.page_size = page_size

    @staticmethod
    def _missing(op: str) -> ClientError:
        return ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, op)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]["body"])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[Key] = {"body": Body, "ContentType": ContentType, "Metadata": {}}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("HeadObject")
        obj = self.objects[Key]
        return {"ContentLength": len(obj["body"]), "Metadata": obj["Metadata"]}

    def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
        extra = ExtraArgs or {}
        self.objects[Key] = {"body": Path(Filename).read_bytes(), "ContentType": extra.get("ContentType"),
                             "Metadata": extra.get("Metadata", {})}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket, Prefix, Delimiter=None, ContinuationToken=None):
        entries: list[tuple[str, str]] = []
        for key in sorted(k for k in self.objects if k.startswith(Prefix)):
            rest = key[len(Prefix):]
            if Delimiter and Delimiter in rest:
                entry = ("prefix", Prefix + rest.split(Delimiter)[0] + Delimiter)
                if entry not in entries:
                    entries.append(entry)
            else:
                entries.append(("key", key))
        start = int(ContinuationToken or 0)
        page = entries[start:start + self.page_size]
        resp = {
            "CommonPrefixes": [{"Prefix": v} for t, v in page if t == "prefix"],
            "Contents": [{"Key": v} for t, v in page if t == "key"],
            "IsTruncated": start + self.page_size < len(entries),
        }
        if resp["IsTruncated"]:
            resp["NextContinuationToken"] = str(start + self.page_size)
        return resp

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://acct.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}?X-Amz-Expires={ExpiresIn}"


@pytest.fixture
def store(settings) -> BucketStore:
    return BucketStore(FakeS3(), "bucket", settings)


def add_item(store: BucketStore, slug: str, caption: str, title: str = "Title", *, probe=VALID_PROBE,
             video=True, cover=True, story=True, meta_extra: dict | None = None) -> None:
    meta = {"title": title, "scheduled_for": None, "probe": probe, "probe_error": None, "story_error": None,
            **(meta_extra or {})}
    store.write_text(store.key(slug, "caption.txt"), caption)
    store.write_text(store.key(slug, "meta.json"), json.dumps(meta))
    for name, present in (("video.mp4", video), ("cover.jpg", cover), ("story.mp4", story)):
        if present:
            store.write_text(store.key(slug, name), "binary")


def ig_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


class GraphMock:
    """Simula os endpoints da Graph API usados pelo bot."""

    def __init__(self):
        self.reels: list[dict] = []
        self.quota = (0, 100)
        self.calls: list[tuple[str, str, dict]] = []
        self.status_plans: list[list[str]] = []   # um plano por contentor criado
        self.publish_faults: list[str] = []        # por chamada a media_publish
        self.containers: dict[str, dict] = {}
        self.statuses: dict[str, list[str]] = {}
        self.published: set[str] = set()
        self.publish_time = NOW
        self.late: dict[str, int] = {}  # cid -> nº de consultas de estado até aparecer publicado
        self.fail_container_status = False

    def posts(self, suffix: str) -> list[dict]:
        return [d for m, p, d in self.calls if m == "POST" and p.endswith(suffix)]

    def _publish(self, cid: str) -> str:
        self.published.add(cid)
        media_id = f"M{cid}"
        data = self.containers[cid]
        if data.get("media_type") == "REELS":
            self.reels.append({"id": media_id, "caption": data["caption"], "media_type": "VIDEO",
                               "media_product_type": "REELS", "permalink": f"https://www.instagram.com/reel/{media_id}/",
                               "timestamp": ig_time(self.publish_time)})
        return media_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split("/", 2)[2]
        data = dict(parse_qsl(request.content.decode())) if request.method == "POST" else dict(request.url.params)
        self.calls.append((request.method, path, data))
        ok = lambda payload: httpx.Response(200, json=payload)

        if request.method == "GET" and path == f"{IG}/media":
            return ok({"data": list(self.reels)})
        if request.method == "GET" and path == f"{IG}/content_publishing_limit":
            return ok({"data": [{"quota_usage": self.quota[0], "config": {"quota_total": self.quota[1]}}]})
        if request.method == "POST" and path == f"{IG}/media":
            cid = f"C{len(self.containers) + 1}"
            self.containers[cid] = data
            self.statuses[cid] = self.status_plans.pop(0) if self.status_plans else ["FINISHED"]
            return ok({"id": cid})
        if request.method == "POST" and path == f"{IG}/media_publish":
            cid = data["creation_id"]
            fault = self.publish_faults.pop(0) if self.publish_faults else None
            if fault == "network":
                raise httpx.ConnectError("boom", request=request)
            if fault == "5xx_after_publish":
                self._publish(cid)
                return httpx.Response(500, json={"error": {"message": "oops", "code": 2}})
            if fault == "5xx":
                return httpx.Response(500, json={"error": {"message": "oops", "code": 2}})
            if fault == "5xx_late":  # erro, mas o reel aparece publicado uns segundos depois
                self.late[cid] = 3
                return httpx.Response(500, json={"error": {"message": "oops", "code": 2}})
            if fault == "400":
                return httpx.Response(400, json={"error": {"message": "bad", "code": 100}})
            return ok({"id": self._publish(cid)})
        if request.method == "GET" and path in self.containers:
            if self.fail_container_status:
                return httpx.Response(400, json={"error": {"message": "unsupported get", "code": 100}})
            if path in self.late:
                self.late[path] -= 1
                if self.late[path] == 0:
                    del self.late[path]
                    self._publish(path)
            if path in self.published:
                return ok({"status_code": "PUBLISHED"})
            seq = self.statuses[path]
            code = seq.pop(0) if len(seq) > 1 else seq[0]
            return ok({"status_code": code, "status": f"{code}: detalhe"})
        if request.method == "GET" and path.startswith("MC"):
            return ok({"permalink": f"https://www.instagram.com/reel/{path}/", "timestamp": ig_time(self.publish_time)})
        return httpx.Response(404, json={"error": {"message": f"unknown {path}", "code": 803}})


@pytest.fixture
def graph_mock() -> GraphMock:
    return GraphMock()


@pytest.fixture
def graph(graph_mock) -> GraphClient:
    http = httpx.Client(transport=httpx.MockTransport(graph_mock.handler))
    return GraphClient("tok", "v23.0", http=http, sleep=lambda s: None)


@pytest.fixture
def publisher(graph, store, settings) -> Publisher:
    return Publisher(graph, store, settings, IG, now=lambda: NOW, sleep=lambda s: None, url_status=lambda u: 206)
