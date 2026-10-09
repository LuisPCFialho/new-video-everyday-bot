import json
from datetime import timedelta

import pytest
from conftest import NOW, VALID_PROBE, add_item, ig_time

from reels_bot.publisher import UsageError
from reels_bot.state import with_fields

CAP1 = "On 1 November 1755, Lisbon was hit 🌊\n\n#whatif #lisbon"
CAP2 = "Vesuvius buried Pompeii in AD 79.\n\n#whatif #vesuvius"


def armed(store):
    """Simula que o primeiro dry-run já foi feito."""
    store.save_state(with_fields(store.load_state(), dry_run_ok_at=NOW.isoformat()))


def test_happy_path_publishes_reel_then_story(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1, "What if Lisbon?")
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    graph_mock.status_plans = [["IN_PROGRESS", "IN_PROGRESS", "FINISHED"], ["IN_PROGRESS", "FINISHED"]]

    result = publisher.run()

    assert result.outcome == "published" and result.exit_code == 0
    assert result.slug == "01-lisbon"
    reel, story = graph_mock.posts("/media")
    assert reel["media_type"] == "REELS" and reel["share_to_feed"] == "true"
    assert reel["caption"] == CAP1.strip()
    assert "queue/01-lisbon/video.mp4" in reel["video_url"] and "cover.jpg" in reel["cover_url"]
    assert story == {"media_type": "STORIES", "video_url": story["video_url"]}
    assert "story.mp4" in story["video_url"]
    entry = store.load_state()["items"]["01-lisbon"]
    assert entry["posted"] and entry["source"] == "bot" and entry["ig_media_id"] == "MC1"
    assert entry["permalink"].endswith("/MC1/") and entry["story_media_id"] == "MC2"
    assert store.load_state()["in_flight"] is None
    for name in ("video.mp4", "cover.jpg", "story.mp4"):
        assert store.head(store.key("01-lisbon", name)) is None
    assert store.head(store.key("02-vesuvius", "video.mp4")) is not None


def test_first_run_is_forced_dry_run(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    result = publisher.run()
    assert result.outcome == "dry_run"
    assert graph_mock.posts("/media") == []
    assert store.load_state()["dry_run_ok_at"] == NOW.isoformat()
    assert publisher.run().outcome == "published"


def test_dry_run_fails_when_presigned_url_unreachable(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    publisher.url_status = lambda url: 403
    result = publisher.run(dry_run=True)
    assert result.outcome == "failed" and "HTTP 403" in result.reason
    assert store.load_state()["consecutive_failures"] == 0  # dry-run nunca conta como falha


def test_manual_post_is_detected_and_skipped(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1, "What if the 1755 earthquake hit Lisbon today?")
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    graph_mock.reels = [{"id": "OLD", "caption": "on 1 November 1755 Lisbon was hit!!", "media_product_type": "REELS",
                         "permalink": "https://ig/OLD", "timestamp": ig_time(NOW - timedelta(days=3))}]
    result = publisher.run()
    assert result.slug == "02-vesuvius"
    items = store.load_state()["items"]
    assert items["01-lisbon"]["source"] == "manual" and items["01-lisbon"]["ig_media_id"] == "OLD"


def test_waits_when_last_reel_too_recent(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.reels = [{"id": "X", "caption": "outro", "media_product_type": "REELS",
                         "timestamp": ig_time(NOW - timedelta(hours=5))}]
    result = publisher.run()
    assert result.outcome == "waiting" and "5.0 h" in result.reason
    assert graph_mock.posts("/media") == []


def test_waits_when_quota_used(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.quota = (100, 100)
    result = publisher.run()
    assert result.outcome == "waiting" and "quota" in result.reason
    assert graph_mock.posts("/media") == []


def test_invalid_item_skipped_next_one_published(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1, video=False)
    add_item(store, "02-vesuvius", CAP2, probe={**VALID_PROBE, "duration_s": 120})
    add_item(store, "03-ok", "Fine caption")
    armed(store)
    assert publisher.run().slug == "03-ok"


def test_no_valid_items_counts_as_failure_then_pauses(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1, story=False)
    armed(store)
    first = publisher.run()
    assert first.outcome == "failed" and "story.mp4 em falta" in first.reason
    second = publisher.run()
    assert second.outcome == "paused" and second.exit_code == 10
    assert store.load_state()["paused"]
    calls_before = len(graph_mock.calls)
    assert publisher.run().outcome == "paused"
    assert len(graph_mock.calls) == calls_before  # em pausa não chama a API


def test_container_error_records_failure(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.status_plans = [["IN_PROGRESS", "ERROR"]]
    result = publisher.run()
    assert result.outcome == "failed" and "ERROR" in result.reason
    state = store.load_state()
    assert state["consecutive_failures"] == 1 and not state["items"].get("01-lisbon")
    assert state["in_flight"]["container_id"] == "C1"


def test_container_timeout(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.status_plans = [["IN_PROGRESS"]]
    result = publisher.run()
    assert result.outcome == "failed" and "não processou" in result.reason


def test_media_publish_network_error_but_published_is_not_retried(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.publish_faults = ["5xx_after_publish"]
    result = publisher.run()
    assert result.outcome == "published"
    assert len(graph_mock.posts("/media_publish")) == 2  # 1 do reel (falhou mas publicou) + 1 da story
    assert len([r for r in graph_mock.reels if r["caption"] == CAP1.strip()]) == 1


def test_media_publish_retries_when_not_published(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.publish_faults = ["network", "5xx"]
    assert publisher.run().outcome == "published"
    assert len(graph_mock.posts("/media_publish")) == 4  # 3 do reel + 1 da story


def test_media_publish_non_transient_error_fails(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.publish_faults = ["400"]
    result = publisher.run()
    assert result.outcome == "failed" and "bad" in result.reason
    assert len(graph_mock.posts("/media_publish")) == 1


def test_story_failure_keeps_reel_and_does_not_count_as_failure(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.status_plans = [["FINISHED"], ["ERROR"]]
    result = publisher.run()
    assert result.outcome == "story_failed" and result.exit_code == 1
    state = store.load_state()
    assert state["items"]["01-lisbon"]["posted"] and state["consecutive_failures"] == 0
    assert "ERROR" in state["items"]["01-lisbon"]["story_error"]


def test_recovers_in_flight_container_published_by_crashed_run(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    graph_mock.containers["C9"] = {"media_type": "REELS", "caption": CAP1.strip()}
    graph_mock._publish("C9")
    graph_mock.reels[0]["timestamp"] = ig_time(NOW - timedelta(days=1))
    store.save_state(with_fields(store.load_state(), in_flight={"slug": "01-lisbon", "container_id": "C9"}))
    result = publisher.run()
    assert result.slug == "02-vesuvius"
    assert store.load_state()["items"]["01-lisbon"]["source"] == "bot"


def test_queue_empty(publisher, store):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    store.save_state({**store.load_state(), "items": {"01-lisbon": {"skipped": True}}})
    result = publisher.run()
    assert result.outcome == "queue_empty" and result.exit_code == 11


def test_scheduled_for_in_future_waits(publisher, store):
    add_item(store, "01-lisbon", CAP1, meta_extra={"scheduled_for": "2026-12-01"})
    armed(store)
    assert publisher.run().outcome == "waiting"


def test_publish_specific_slug(publisher, store):
    add_item(store, "01-lisbon", CAP1)
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    assert publisher.run(slug="02-vesuvius").slug == "02-vesuvius"
    with pytest.raises(UsageError):
        publisher.run(slug="02-vesuvius")
    with pytest.raises(UsageError):
        publisher.run(slug="99-nope")


def test_sync_marks_without_publishing(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1, "What if Lisbon")
    graph_mock.reels = [{"id": "A", "caption": "WHAT IF LISBON — teaser", "media_product_type": "REELS"}]
    state, marked = publisher.sync()
    assert marked == ["01-lisbon"] and state["items"]["01-lisbon"]["ig_media_id"] == "A"
    assert graph_mock.posts("/media") == []


# --- correcções da revisão --------------------------------------------------
def test_lock_held_by_other_run_blocks_publishing(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    store.write_text(store.lock_key, json.dumps({"token": "x", "owner": "github-actions",
                                                 "at": (NOW - timedelta(minutes=5)).isoformat()}))
    result = publisher.run()
    assert result.outcome == "waiting" and "github-actions" in result.reason
    assert graph_mock.posts("/media") == []
    assert store.read_text(store.lock_key)  # o lock alheio não é apagado


def test_expired_lock_is_ignored_and_own_lock_released(publisher, store):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    store.write_text(store.lock_key, json.dumps({"token": "x", "owner": "local",
                                                 "at": (NOW - timedelta(hours=2)).isoformat()}))
    assert publisher.run().outcome == "published"
    assert store.read_text(store.lock_key) is None


def test_unverifiable_in_flight_fails_safe_without_republishing(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.containers["C9"] = {"media_type": "REELS", "caption": CAP1.strip()}
    graph_mock.fail_container_status = True
    store.save_state(with_fields(store.load_state(), in_flight={"slug": "01-lisbon", "container_id": "C9"}))
    result = publisher.run()
    assert result.outcome == "failed" and "contentor pendente" in result.reason
    assert graph_mock.posts("/media") == []
    state = store.load_state()
    assert state["in_flight"]["container_id"] == "C9" and state["consecutive_failures"] == 1


def test_unexpected_exception_counts_towards_pause(publisher, store, monkeypatch):
    add_item(store, "01-lisbon", CAP1)
    armed(store)

    def broken(*a, **k):
        raise KeyError("id")

    monkeypatch.setattr(publisher.graph, "post", broken)
    assert publisher.run().outcome == "failed"
    assert publisher.run().outcome == "paused"


def test_invalid_scheduled_for_is_skipped_not_crash(publisher, store):
    add_item(store, "01-lisbon", CAP1, meta_extra={"scheduled_for": "amanhã"})
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    assert publisher.run().slug == "02-vesuvius"


def test_media_publish_error_then_late_publish_is_detected(publisher, store, graph_mock):
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    graph_mock.publish_faults = ["5xx_late"]
    assert publisher.run().outcome == "published"
    assert len(graph_mock.posts("/media_publish")) == 2  # 1 reel (sem repetir) + 1 story
    assert len([r for r in graph_mock.reels if r["caption"] == CAP1.strip()]) == 1
    assert store.load_state()["items"]["01-lisbon"]["ig_media_id"] == "MC1"


# --- agendamento aleatório (2 por dia) ----------------------------------------
def scheduled_publisher(publisher, settings_overrides, now):
    from conftest import make_settings
    publisher.settings = make_settings(**settings_overrides)
    publisher.now = lambda: now
    slept = []
    publisher.sleep = slept.append
    return slept


def test_scheduled_waits_until_slot_then_never_reuses_it(publisher, store, graph_mock):
    from datetime import date

    from reels_bot.slots import daily_slots
    from conftest import make_settings
    add_item(store, "01-lisbon", CAP1)
    add_item(store, "02-vesuvius", CAP2)
    armed(store)
    settings = {"start_date": date(2026, 10, 1), "min_hours_between_posts": 3}
    slot = daily_slots(date(2026, 10, 9), make_settings(**settings))[1]
    slept = scheduled_publisher(publisher, settings, slot - timedelta(minutes=20))
    assert publisher.run(scheduled=True).slug == "01-lisbon"
    assert slept[0] == 20 * 60
    assert slot.isoformat() in store.load_state()["slots_used"]
    assert publisher.run(scheduled=True).outcome == "waiting"  # mesmo slot: não repete
    assert len(graph_mock.posts("/media_publish")) == 2  # 1 reel + 1 story


def test_scheduled_outside_slot_does_nothing(publisher, store, graph_mock):
    from datetime import date
    add_item(store, "01-lisbon", CAP1)
    armed(store)
    scheduled_publisher(publisher, {"start_date": date(2026, 12, 1)}, NOW)
    assert publisher.run(scheduled=True).outcome == "waiting"
    assert graph_mock.calls == []


def test_publish_first_order(publisher, store):
    for slug in ("01-lisbon", "02-vesuvius", "03-meteor", "16-earth"):
        add_item(store, slug, f"Caption for {slug}")
    armed(store)
    scheduled_publisher(publisher, {"publish_first": (1, 16), "min_hours_between_posts": 0}, NOW)
    order = [publisher.run().slug for _ in range(4)]
    assert order == ["01-lisbon", "16-earth", "02-vesuvius", "03-meteor"]
