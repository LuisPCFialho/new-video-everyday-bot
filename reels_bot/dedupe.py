"""Detecção de publicações já existentes (manuais ou do bot)."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Protocol

from .captions import first_line, normalize
from .graph import GraphClient
from .state import State, item_state, mark_posted

log = logging.getLogger(__name__)

MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp"
MIN_TITLE_WORDS = 3  # títulos curtos ("Titanic") casavam com legendas alheias


class HasCaption(Protocol):
    slug: str
    title: str
    caption: str


def fetch_media(graph: GraphClient, ig_user_id: str) -> list[dict]:
    """Todas as publicações da conta (paginado). A deduplicação usa todas
    (um vídeo publicado à mão como post normal também conta)."""
    return list(graph.paginate(f"{ig_user_id}/media", fields=MEDIA_FIELDS, limit="100"))


def reels_only(media: list[dict]) -> list[dict]:
    return [m for m in media if m.get("media_product_type") == "REELS"]


def is_match(caption: str, title: str, ig_caption: str | None) -> bool:
    ig_norm = normalize(ig_caption)
    if not ig_norm:
        return False
    item_first = normalize(first_line(caption))
    if item_first and normalize(first_line(ig_caption)) == item_first:
        return True
    norm_title = normalize(title)
    return len(norm_title.split()) >= MIN_TITLE_WORDS and f" {norm_title} " in f" {ig_norm} "


def apply_sync(state: State, items: Iterable[HasCaption], media: list[dict]) -> tuple[State, list[str]]:
    """Marca como publicados os itens que já existem no Instagram.
    Cada publicação só pode corresponder a um item."""
    used = {e.get("ig_media_id") for e in state["items"].values() if e.get("ig_media_id")}
    bot_ids = {e.get("ig_media_id") for e in state["items"].values() if e.get("source") == "bot"}
    new_state, marked = state, []
    for item in items:
        if item_state(state, item.slug).get("posted"):
            continue
        match = next((m for m in media
                      if m["id"] not in used and is_match(item.caption, item.title, m.get("caption"))), None)
        if match:
            used.add(match["id"])
            new_state = mark_posted(
                new_state, item.slug,
                media_id=match["id"], permalink=match.get("permalink"), posted_at=match.get("timestamp"),
                source="bot" if match["id"] in bot_ids else "manual",
            )
            marked.append(item.slug)
    return new_state, marked


def parse_ig_time(value: str) -> datetime | None:
    """'2026-10-09T20:00:12+0000' → datetime com fuso (None se não reconhecer)."""
    for parse in (lambda v: datetime.strptime(v, "%Y-%m-%dT%H:%M:%S%z"), datetime.fromisoformat):
        try:
            dt = parse(value)
            if dt.tzinfo is not None:
                return dt
        except ValueError:
            continue
    log.warning("Timestamp do Instagram não reconhecido: %r", value)
    return None


def latest_reel_time(reels: list[dict]) -> datetime | None:
    times = [t for t in (parse_ig_time(m["timestamp"]) for m in reels if m.get("timestamp")) if t]
    return max(times, default=None)
