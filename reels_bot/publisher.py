"""Máquina de estados da publicação de um Reel (+ story teaser).

lock → sync → recuperação de execução interrompida → intervalo mínimo → quota →
validação → contentor REELS → polling → media_publish → permalink →
story → limpeza → unlock.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from .config import Settings
from .dedupe import apply_sync, fetch_media, is_match, latest_reel_time, reels_only
from .graph import GraphClient, GraphError
from .importer import item_errors
from .state import State, is_done, item_state, mark_posted, mark_story, record_failure, record_success, with_fields
from .store import MEDIA_FILES, BucketStore, QueueItem

log = logging.getLogger(__name__)

EXIT_CODES = {"published": 0, "dry_run": 0, "waiting": 0, "failed": 1, "story_failed": 1,
              "paused": 10, "queue_empty": 11}
PUBLISH_TRIES = 3
VERIFY_CHECKS = 8  # depois de um erro em media_publish, confirma durante ~2 min antes de repetir
LOCK_TTL = timedelta(minutes=30)


class PublishError(RuntimeError):
    pass


class UsageError(RuntimeError):
    pass


class LockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class RunResult:
    outcome: str
    slug: str | None = None
    permalink: str | None = None
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.outcome]


def _range_get_status(url: str) -> int:
    """Confirma que o URL pré-assinado é acessível (GET do 1.º byte)."""
    return httpx.get(url, headers={"Range": "bytes=0-0"}, timeout=30).status_code


def active_lock(store: BucketStore, now: datetime) -> dict | None:
    """Lock de outra execução ainda válido (ou None)."""
    raw = store.read_text(store.lock_key)
    if not raw:
        return None
    held = json.loads(raw)
    return held if now - datetime.fromisoformat(held["at"]) < LOCK_TTL else None


@dataclass
class Publisher:
    graph: GraphClient
    store: BucketStore
    settings: Settings
    ig_user_id: str
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    sleep: Callable[[float], None] = time.sleep
    url_status: Callable[[str], int] = _range_get_status
    owner: str = field(default_factory=lambda: "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local")

    # --- entrada ------------------------------------------------------------
    def run(self, *, dry_run: bool = False, slug: str | None = None) -> RunResult:
        state = self.store.load_state()
        if state["paused"]:
            return RunResult("paused", reason=state["pause_reason"] or "pausado")
        if not dry_run and not state["dry_run_ok_at"]:
            log.warning("Ainda não houve nenhum dry-run com sucesso: esta execução é forçada a dry-run.")
            dry_run = True
        lock = None
        try:
            if not dry_run:
                lock = self._acquire_lock()
            return self._run(state, dry_run, slug)
        except LockedError as exc:
            log.warning("%s — nada feito.", exc)
            return RunResult("waiting", slug, reason=str(exc))
        except UsageError:
            raise
        except Exception as exc:  # qualquer erro conta para a pausa (o in_flight mantém-se)
            log.error("Falhou: %s: %s", type(exc).__name__, exc)
            if dry_run:
                return RunResult("failed", slug, reason=str(exc))
            return self._record_failure(slug, f"{type(exc).__name__}: {exc}")
        finally:
            if lock:
                self._release_lock(lock)

    def sync(self) -> tuple[State, list[str]]:
        state = self.store.load_state()
        items = self.store.load_items(skip=lambda s: is_done(state, s))
        media = fetch_media(self.graph, self.ig_user_id)
        state, marked = apply_sync(self._recover_in_flight(state, items, media), items, media)
        self.store.save_state(state)
        return state, marked

    # --- lock e falhas ------------------------------------------------------
    def _acquire_lock(self) -> str:
        held = active_lock(self.store, self.now())
        if held:
            raise LockedError(f"outra execução ({held.get('owner')}) está a publicar desde {held['at']}")
        token = uuid.uuid4().hex
        self.store.write_text(self.store.lock_key, json.dumps(
            {"token": token, "owner": self.owner, "at": self.now().isoformat()}), "application/json")
        if json.loads(self.store.read_text(self.store.lock_key) or "{}").get("token") != token:
            raise LockedError("outra execução começou ao mesmo tempo")
        return token

    def _release_lock(self, token: str) -> None:
        try:
            raw = self.store.read_text(self.store.lock_key)
            if raw and json.loads(raw).get("token") == token:
                self.store.delete(self.store.lock_key)
        except Exception as exc:  # o lock expira sozinho ao fim de 30 min
            log.warning("Não consegui libertar o lock: %s", exc)

    def _record_failure(self, slug: str | None, reason: str) -> RunResult:
        try:
            state = record_failure(self.store.load_state(), reason, self.settings.max_consecutive_failures)
            self.store.save_state(state)
        except Exception as exc:
            log.error("Nem consegui registar a falha no bucket: %s", exc)
            return RunResult("failed", slug, reason=reason)
        if state["paused"]:
            log.error("PAUSADO após %d falhas seguidas.", state["consecutive_failures"])
            return RunResult("paused", slug, reason=state["pause_reason"])
        return RunResult("failed", slug, reason=reason)

    # --- fluxo --------------------------------------------------------------
    def _run(self, state: State, dry_run: bool, slug: str | None) -> RunResult:
        items = self.store.load_items(skip=lambda s: is_done(state, s))
        media = fetch_media(self.graph, self.ig_user_id)
        state, marked = apply_sync(self._recover_in_flight(state, items, media), items, media)
        for s in marked:
            log.info("Já está no Instagram (não será publicado): %s", s)
        self.store.save_state(state)

        pending = self._pending(state, items, slug)
        if not pending:
            return RunResult("queue_empty", reason="não há itens por publicar")
        due = [i for i in pending if self._is_due(i)]
        if not due:
            return RunResult("waiting", reason="itens restantes têm scheduled_for no futuro")

        wait_reason = self._wait_reason(reels_only(media))
        if wait_reason:
            if not dry_run:
                return RunResult("waiting", reason=wait_reason)
            log.info("(dry-run) Numa execução real esperaria: %s", wait_reason)

        item = self._first_valid(due)
        if dry_run:
            return self._dry_run(state, item)

        log.info("A publicar %s — %s", item.slug, item.title)
        posted = self._publish(state, item)
        state = mark_posted(state, item.slug, media_id=posted["id"], permalink=posted.get("permalink"),
                            posted_at=posted.get("timestamp") or self.now().isoformat(), source="bot")
        state = record_success(state)
        self.store.save_state(state)  # o reel fica registado antes de se tentar a story
        log.info("Reel publicado: %s %s", item.slug, posted.get("permalink") or "")

        story_id, story_error = self._publish_story(item)
        self.store.save_state(mark_story(state, item.slug, media_id=story_id, error=story_error))
        self._cleanup(item.slug)
        if story_error:
            return RunResult("story_failed", item.slug, posted.get("permalink"), reason=f"story falhou: {story_error}")
        log.info("Story publicada: %s", story_id)
        return RunResult("published", item.slug, posted.get("permalink"))

    def _pending(self, state: State, items: list[QueueItem], slug: str | None) -> list[QueueItem]:
        if slug is None:
            return [i for i in items if not is_done(state, i.slug)]
        if is_done(state, slug):
            raise UsageError(f"'{slug}' já foi publicado ou saltado")
        chosen = [i for i in items if i.slug == slug]
        if not chosen:
            raise UsageError(f"'{slug}' não existe na fila do bucket")
        return chosen

    def _is_due(self, item: QueueItem) -> bool:
        when = item.meta.get("scheduled_for")
        if not when:
            return True
        try:
            dt = datetime.fromisoformat(str(when))
        except ValueError:
            return True  # a validação marca-o como inválido e salta-o
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(self.settings.timezone))
        return self.now() >= dt

    def _wait_reason(self, reels: list[dict]) -> str | None:
        last = latest_reel_time(reels)
        min_gap = timedelta(hours=self.settings.min_hours_between_posts)
        if last and self.now() - last < min_gap:
            hours = (self.now() - last).total_seconds() / 3600
            return f"último reel há {hours:.1f} h (mínimo {self.settings.min_hours_between_posts:g} h)"
        data = (self.graph.get(f"{self.ig_user_id}/content_publishing_limit",
                               fields="config,quota_usage").get("data") or [{}])[0]
        usage, total = int(data.get("quota_usage", 0)), int(data.get("config", {}).get("quota_total", 25))
        if usage >= total:
            return f"quota de publicação esgotada ({usage}/{total} em 24 h)"
        return None

    def _first_valid(self, due: list[QueueItem]) -> QueueItem:
        reasons = []
        for item in due:
            errors = item_errors(item.caption, item.meta, item.has_video, item.has_cover, item.has_story)
            if not errors:
                return item
            log.warning("A saltar %s: %s", item.slug, "; ".join(errors))
            reasons.append(f"{item.slug}: {'; '.join(errors)}")
        raise PublishError("nenhum item válido na fila — " + " | ".join(reasons))

    def _dry_run(self, state: State, item: QueueItem) -> RunResult:
        for name in MEDIA_FILES:
            status = self.url_status(self._url(item, name))
            if status not in (200, 206):
                raise PublishError(f"URL pré-assinado de {name} devolveu HTTP {status}")
        log.info("(dry-run) Seria publicado: %s — %s", item.slug, item.title)
        log.info("(dry-run) Legenda (%d caracteres):\n%s", len(item.caption), item.caption)
        self.store.save_state(with_fields(state, dry_run_ok_at=self.now().isoformat()))
        return RunResult("dry_run", item.slug, reason="nada foi publicado")

    # --- Graph API ----------------------------------------------------------
    def _url(self, item: QueueItem, name: str) -> str:
        return self.store.presign(self.store.key(item.slug, name), self.settings.presign_ttl_s)

    def _publish(self, state: State, item: QueueItem) -> dict:
        cid = self.graph.post(f"{self.ig_user_id}/media", {
            "media_type": "REELS",
            "video_url": self._url(item, "video.mp4"),
            "cover_url": self._url(item, "cover.jpg"),
            "caption": item.caption,
            "share_to_feed": "true",
        })["id"]
        self.store.save_state(with_fields(state, in_flight={
            "slug": item.slug, "container_id": cid, "started_at": self.now().isoformat()}))
        self._wait_finished(cid, self.settings.poll_timeout_s)
        media_id = self._publish_container(cid, lambda: self._reel_already_published(cid, item))
        try:
            info = self.graph.get(media_id, fields="permalink,timestamp") if media_id else {}
        except GraphError as exc:
            log.warning("Publicado, mas não consegui obter o permalink: %s", exc)
            info = {}
        return {"id": media_id, **info}

    def _publish_story(self, item: QueueItem) -> tuple[str | None, str | None]:
        """Story com o teaser do reel. Devolve (media_id, erro); nunca levanta excepção."""
        try:
            cid = self.graph.post(f"{self.ig_user_id}/media", {
                "media_type": "STORIES",
                "video_url": self._url(item, "story.mp4"),
            })["id"]
            self._wait_finished(cid, self.settings.story_poll_timeout_s)
            return self._publish_container(cid, lambda: self._container_published(cid)), None
        except Exception as exc:  # a story nunca pode estragar um reel já publicado
            log.error("O reel foi publicado, mas a story falhou: %s", exc)
            return None, str(exc)

    def _wait_finished(self, cid: str, timeout_s: int) -> None:
        waited = 0
        while True:
            status = self.graph.get(cid, fields="status_code,status")
            code = status.get("status_code")
            if code in ("FINISHED", "PUBLISHED"):
                return
            if code in ("ERROR", "EXPIRED"):
                raise PublishError(f"contentor {code}: {status.get('status', '')}")
            if waited >= timeout_s:
                raise PublishError(f"o Instagram não processou o vídeo em {waited // 60} min (estado {code})")
            self.sleep(self.settings.poll_interval_s)
            waited += self.settings.poll_interval_s

    def _publish_container(self, cid: str, verify: Callable[[], tuple[bool, str | None]]) -> str | None:
        """Nunca repete media_publish sem antes confirmar (`verify`), durante ~2 min,
        que o contentor não foi publicado."""
        last_error: GraphError | None = None
        for attempt in range(1, PUBLISH_TRIES + 1):
            try:
                return self.graph.post(f"{self.ig_user_id}/media_publish", {"creation_id": cid}, retry=False)["id"]
            except GraphError as exc:
                if not exc.transient:
                    raise
                last_error = exc
            for check in range(VERIFY_CHECKS):
                published, media_id = verify()
                if published:
                    log.warning("media_publish deu erro mas o contentor foi publicado (media %s).", media_id)
                    return media_id
                if check < VERIFY_CHECKS - 1:
                    self.sleep(self.settings.poll_interval_s)
        raise PublishError(f"media_publish falhou {PUBLISH_TRIES} vezes: {last_error}")

    def _container_published(self, cid: str) -> tuple[bool, str | None]:
        return self.graph.get(cid, fields="status_code").get("status_code") == "PUBLISHED", None

    def _reel_already_published(self, cid: str, item: QueueItem) -> tuple[bool, str | None]:
        def find() -> dict | None:
            return next((m for m in fetch_media(self.graph, self.ig_user_id)
                         if is_match(item.caption, item.title, m.get("caption"))), None)

        match = find()
        if match:
            return True, match["id"]
        if self._container_published(cid)[0]:
            match = find()  # publicado entretanto: obter o id do media
            return True, match["id"] if match else None
        return False, None

    def _recover_in_flight(self, state: State, items: list[QueueItem], media: list[dict]) -> State:
        """Se uma execução anterior morreu a meio, confirma se o contentor foi publicado.
        Se não for possível confirmar, falha (o in_flight mantém-se para a próxima vez)."""
        flight = state.get("in_flight")
        if not flight:
            return state
        slug, cid = flight.get("slug"), flight.get("container_id")
        if cid and not item_state(state, slug).get("posted"):
            try:
                code = self.graph.get(cid, fields="status_code").get("status_code")
            except GraphError as exc:
                raise PublishError(f"não consegui verificar o contentor pendente {cid} ({slug}): {exc}") from exc
            if code == "PUBLISHED":
                item = next((i for i in items if i.slug == slug), None)
                match = next((m for m in media if item and is_match(item.caption, item.title, m.get("caption"))), {})
                log.warning("A execução anterior publicou %s sem o registar — corrigido.", slug)
                state = mark_posted(state, slug, media_id=match.get("id"), permalink=match.get("permalink"),
                                    posted_at=match.get("timestamp") or flight.get("started_at"), source="bot")
        return with_fields(state, in_flight=None)

    def _cleanup(self, slug: str) -> None:
        for name in MEDIA_FILES:
            try:
                self.store.delete(self.store.key(slug, name))
            except Exception as exc:  # limpeza não deve estragar uma publicação bem-sucedida
                log.warning("Não consegui apagar %s/%s do bucket: %s", slug, name, exc)
