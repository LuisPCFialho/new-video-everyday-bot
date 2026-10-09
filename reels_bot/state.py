"""Estado persistente (state.json no bucket). Funções puras: devolvem cópias."""
from __future__ import annotations

import copy
from typing import Any

State = dict[str, Any]


def empty_state() -> State:
    return {
        "version": 1,
        "items": {},
        "consecutive_failures": 0,
        "paused": False,
        "pause_reason": None,
        "dry_run_ok_at": None,
        "in_flight": None,
    }


def normalize_state(raw: dict | None) -> State:
    return {**empty_state(), **copy.deepcopy(raw or {})}


def item_state(state: State, slug: str) -> dict:
    return state["items"].get(slug, {})


def is_done(state: State, slug: str) -> bool:
    entry = item_state(state, slug)
    return bool(entry.get("posted") or entry.get("skipped"))


def with_fields(state: State, **fields: Any) -> State:
    new = copy.deepcopy(state)
    new.update(fields)
    return new


def _with_item(state: State, slug: str, **fields: Any) -> State:
    new = copy.deepcopy(state)
    new["items"][slug] = {**new["items"].get(slug, {}), **fields}
    return new


def mark_posted(state: State, slug: str, *, media_id: str | None, permalink: str | None,
                posted_at: str | None, source: str) -> State:
    return _with_item(state, slug, posted=True, ig_media_id=media_id, permalink=permalink,
                      posted_at=posted_at, source=source)


def mark_story(state: State, slug: str, *, media_id: str | None, error: str | None) -> State:
    return _with_item(state, slug, story_media_id=media_id, story_error=error)


def mark_skipped(state: State, slug: str, reason: str) -> State:
    return _with_item(state, slug, skipped=True, skip_reason=reason)


def record_success(state: State) -> State:
    return with_fields(state, consecutive_failures=0, in_flight=None)


def record_failure(state: State, reason: str, max_failures: int) -> State:
    """Mantém `in_flight` de propósito: a execução seguinte verifica se o
    contentor chegou a ser publicado antes de tentar outro item."""
    failures = state["consecutive_failures"] + 1
    paused = failures >= max_failures
    return with_fields(
        state,
        consecutive_failures=failures,
        paused=paused or state["paused"],
        pause_reason=f"{failures} falhas seguidas; última: {reason}" if paused else state["pause_reason"],
    )


def resume(state: State) -> State:
    return with_fields(state, paused=False, pause_reason=None, consecutive_failures=0)
