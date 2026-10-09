import json

import pytest
from conftest import FakeS3, add_item

from reels_bot.state import mark_posted
from reels_bot.store import BucketStore, push_queue


def make_local_item(queue, slug, *, video=b"video", story=True):
    folder = queue / slug
    folder.mkdir(parents=True)
    (folder / "caption.txt").write_text("cap\n", encoding="utf-8")
    (folder / "meta.json").write_text(json.dumps({"title": slug}), encoding="utf-8")
    (folder / "cover.jpg").write_bytes(b"jpg")
    if video is not None:
        (folder / "video.mp4").write_bytes(video)
    if story:
        (folder / "story.mp4").write_bytes(b"story")
    return folder


def test_list_slugs_numeric_order_with_pagination(settings):
    store = BucketStore(FakeS3(page_size=2), "b", settings)
    for slug in ("100-z", "02-b", "11-c", "01-a"):
        add_item(store, slug, "cap")
    store.write_text("queue/not-an-item/x", "x")
    assert store.list_slugs() == ["01-a", "02-b", "11-c", "100-z"]


def test_state_roundtrip_and_defaults(store):
    state = store.load_state()
    assert state["items"] == {} and state["paused"] is False
    store.save_state(mark_posted(state, "01-a", media_id="1", permalink="p", posted_at="t", source="bot"))
    assert store.load_state()["items"]["01-a"]["posted"] is True


def test_load_items_single_listing_and_skip(store):
    add_item(store, "01-a", "  caption \n", "Título", cover=False)
    add_item(store, "02-b", "x")
    calls = []
    original = store.client.list_objects_v2
    store.client.list_objects_v2 = lambda **kw: calls.append(kw) or original(**kw)
    (item,) = store.load_items(skip=lambda slug: slug == "02-b")
    assert (item.title, item.caption, item.has_video, item.has_cover, item.has_story) == ("Título", "caption", True, False, True)
    assert len(calls) == 1


def test_push_refuses_empty_local_queue(store, tmp_path):
    add_item(store, "01-a", "cap")
    with pytest.raises(ValueError, match="vazia"):
        push_queue(store, tmp_path / "queue")
    assert store.list_slugs() == ["01-a"]


def test_push_uploads_skips_unchanged_and_mirrors(store, tmp_path):
    queue = tmp_path / "queue"
    make_local_item(queue, "01-a")
    make_local_item(queue, "02-b", video=None)
    report = push_queue(store, queue)
    assert sorted(report.uploaded) == ["01-a/cover.jpg", "01-a/story.mp4", "01-a/video.mp4", "02-b/cover.jpg", "02-b/story.mp4"]
    assert store.client.objects["queue/01-a/video.mp4"]["ContentType"] == "video/mp4"
    assert store.client.objects["queue/01-a/cover.jpg"]["ContentType"] == "image/jpeg"

    assert push_queue(store, queue).uploaded == []  # nada mudou

    (queue / "01-a" / "video.mp4").write_bytes(b"new longer video")
    (queue / "01-a" / "story.mp4").unlink()
    report = push_queue(store, queue)
    assert report.uploaded == ["01-a/video.mp4"]
    assert store.head("queue/01-a/story.mp4") is None


def test_push_removes_stale_unposted_but_keeps_posted(store, tmp_path):
    add_item(store, "05-old", "cap")
    add_item(store, "06-posted", "cap")
    store.save_state(mark_posted(store.load_state(), "06-posted", media_id="1", permalink=None,
                                 posted_at=None, source="bot"))
    queue = tmp_path / "queue"
    make_local_item(queue, "06-posted")
    report = push_queue(store, queue)
    assert report.removed == ["05-old"] and report.skipped_posted == ["06-posted"]
    assert store.list_slugs() == ["06-posted"]
    assert report.uploaded == []
