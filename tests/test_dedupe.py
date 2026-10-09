from dataclasses import dataclass

import httpx
from conftest import IG

from reels_bot.dedupe import apply_sync, fetch_media, is_match, latest_reel_time, parse_ig_time, reels_only
from reels_bot.state import empty_state, mark_posted


@dataclass
class Item:
    slug: str
    title: str
    caption: str


ITEM = Item("01-lisbon", "What if the 1755 earthquake hit Lisbon today?",
            "On 1 November 1755, Lisbon was hit by an earthquake 🌊\n\n#whatif")


def test_first_line_match_ignores_case_emoji_punctuation():
    assert is_match(ITEM.caption, ITEM.title, "🔥 on 1 November 1755 Lisbon was hit by an earthquake!!!\n#other")


def test_title_contained_in_caption_matches():
    assert is_match(ITEM.caption, ITEM.title, "Hoje: What if the 1755 earthquake hit Lisbon today? 🇵🇹 #lisboa")


def test_unrelated_caption_does_not_match():
    assert not is_match(ITEM.caption, ITEM.title, "What if Vesuvius erupted today?\nVesuvius buried Pompeii")
    assert not is_match(ITEM.caption, ITEM.title, None)
    assert not is_match(ITEM.caption, ITEM.title, "")


def test_title_must_match_whole_words():
    assert not is_match("x", "what if lisbon", "what if lisbonense é outra coisa")
    assert is_match("x", "what if lisbon", "Hoje: what if Lisbon!")


def test_short_title_never_matches_by_substring():
    assert not is_match("April 14, 1912.", "Titanic", "A minha visita ao museu do Titanic")


def test_one_post_marks_only_one_item():
    a = Item("01-a", "Title A long one", "Same first line\nA")
    b = Item("02-b", "Title B long one", "Same first line\nB")
    new_state, marked = apply_sync(empty_state(), [a, b], [{"id": "M1", "caption": "Same first line\nA"}])
    assert marked == ["01-a"]
    # e um post já associado a um item no estado não marca outro
    _, marked_again = apply_sync(new_state, [a, b], [{"id": "M1", "caption": "Same first line\nA"}])
    assert marked_again == []


def test_apply_sync_marks_manual_and_keeps_bot_entries():
    other = Item("02-vesuvius", "What if Vesuvius erupted today?", "Vesuvius buried Pompeii.")
    state = mark_posted(empty_state(), "02-vesuvius", media_id="B1", permalink="p", posted_at="t", source="bot")
    reels = [
        {"id": "M1", "caption": ITEM.caption, "permalink": "https://ig/M1", "timestamp": "2026-10-01T20:00:00+0000"},
        {"id": "B1", "caption": other.caption},
    ]
    new_state, marked = apply_sync(state, [ITEM, other], reels)
    assert marked == ["01-lisbon"]
    assert new_state["items"]["01-lisbon"] == {
        "posted": True, "ig_media_id": "M1", "permalink": "https://ig/M1",
        "posted_at": "2026-10-01T20:00:00+0000", "source": "manual",
    }
    assert new_state["items"]["02-vesuvius"]["source"] == "bot"
    assert "01-lisbon" not in state["items"]  # estado original intacto


def test_fetch_reels_paginates_and_filters(graph):
    def handler(request):
        if request.url.params.get("after") == "c2":
            return httpx.Response(200, json={"data": [{"id": "3", "media_product_type": "REELS"}]})
        assert request.url.params["fields"] == "id,caption,media_type,media_product_type,permalink,timestamp"
        return httpx.Response(200, json={
            "data": [{"id": "1", "media_product_type": "REELS"}, {"id": "2", "media_product_type": "FEED"}],
            "paging": {"next": f"https://graph.facebook.com/v23.0/{IG}/media?after=c2"},
        })

    graph.http = httpx.Client(transport=httpx.MockTransport(handler))
    media = fetch_media(graph, IG)
    assert [m["id"] for m in media] == ["1", "2", "3"]
    assert [m["id"] for m in reels_only(media)] == ["1", "3"]


def test_feed_post_with_same_caption_counts_as_duplicate():
    state, marked = apply_sync(empty_state(), [ITEM], [{"id": "F", "caption": ITEM.caption, "media_product_type": "FEED"}])
    assert marked == ["01-lisbon"]


def test_latest_reel_time_and_bad_timestamps():
    reels = [{"timestamp": "2026-10-01T20:00:00+0000"}, {"timestamp": "2026-10-08T21:05:00+0000"}, {},
             {"timestamp": "ontem"}]
    assert latest_reel_time(reels).isoformat() == "2026-10-08T21:05:00+00:00"
    assert latest_reel_time([]) is None
    assert parse_ig_time("2026-10-08T21:05:00+00:00").hour == 21
    assert parse_ig_time("2026-10-08T21:05:00") is None
