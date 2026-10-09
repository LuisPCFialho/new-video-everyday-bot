import json
from datetime import UTC, datetime

import pytest
from conftest import NOW, add_item

from reels_bot import cli
from reels_bot.importer import import_queue
from reels_bot.probe import VideoProbe
from reels_bot.state import mark_posted, with_fields
from reels_bot.tokens import TokenError


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path, store, publisher):
    monkeypatch.setattr(cli, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(cli, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(cli, "load_env_file", lambda: None)
    monkeypatch.setattr(cli, "_store", lambda settings: store)
    monkeypatch.setattr(cli, "_publisher", lambda settings, st: publisher)


def test_status_table(store, capsys):
    add_item(store, "01-lisbon", "Lisbon caption", "What if Lisbon?")
    add_item(store, "02-vesuvius", "Vesuvius caption", "What if Vesuvius?")
    add_item(store, "14-titanic", "Titanic caption", "What if Titanic?", video=False)
    store.save_state(mark_posted(store.load_state(), "01-lisbon", media_id="1", permalink="https://ig/1",
                                 posted_at="2026-10-01T20:00:00+0000", source="manual"))
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "publicado (manual)" in out and "https://ig/1" in out
    assert "na fila" in out and "INVÁLIDO: video.mp4 em falta" in out
    assert "primeiro dry-run" in out


def test_publish_next_dry_run_then_real(store):
    add_item(store, "01-lisbon", "Lisbon caption")
    assert cli.main(["publish-next", "--dry-run"]) == 0
    assert cli.main(["publish-next"]) == 0
    assert store.load_state()["items"]["01-lisbon"]["posted"]
    assert cli.main(["publish-next"]) == 11  # fila vazia


def test_paused_exits_10_without_calling_api(store, monkeypatch):
    store.save_state(with_fields(store.load_state(), paused=True, pause_reason="x"))
    monkeypatch.setattr(cli, "_publisher", lambda s, st: pytest.fail("não devia criar o publisher"))
    assert cli.main(["publish-next"]) == 10
    assert cli.main(["resume"]) == 0
    assert store.load_state()["paused"] is False


def test_token_error_counts_as_failure(store, monkeypatch):
    def boom(settings, st):
        raise TokenError("Token inválido")

    monkeypatch.setattr(cli, "_publisher", boom)
    assert cli.main(["publish-next"]) == 1  # ensaio forçado: não conta
    assert store.load_state()["consecutive_failures"] == 0
    store.save_state(with_fields(store.load_state(), dry_run_ok_at=NOW.isoformat()))
    assert cli.main(["publish-next"]) == 1
    assert cli.main(["publish-next"]) == 10
    assert store.load_state()["paused"]


def test_skip_refused_while_publishing(store):
    add_item(store, "01-lisbon", "Lisbon caption")
    store.write_text(store.lock_key, json.dumps({"token": "t", "owner": "github-actions",
                                                 "at": datetime.now(UTC).isoformat()}))
    assert cli.main(["skip", "01-lisbon"]) == 2
    assert cli.main(["resume"]) == 2
    assert not store.load_state()["items"]


def test_skip_and_publish_specific(store):
    add_item(store, "01-lisbon", "Lisbon caption")
    add_item(store, "02-vesuvius", "Vesuvius caption")
    store.save_state(with_fields(store.load_state(), dry_run_ok_at=NOW.isoformat()))
    assert cli.main(["skip", "01-lisbon", "--reason", "não gosto"]) == 0
    assert store.load_state()["items"]["01-lisbon"]["skip_reason"] == "não gosto"
    assert cli.main(["skip", "01-lisbon"]) == 2
    assert cli.main(["skip", "99-nope"]) == 2
    assert cli.main(["publish", "02-vesuvius"]) == 0
    assert cli.main(["publish", "02-vesuvius"]) == 2


def test_sync_command(store, graph_mock):
    add_item(store, "01-lisbon", "Lisbon caption", "What if Lisbon?")
    graph_mock.reels = [{"id": "A", "caption": "What if Lisbon? 🇵🇹", "media_product_type": "REELS"}]
    assert cli.main(["sync"]) == 0
    assert store.load_state()["items"]["01-lisbon"]["source"] == "manual"


def test_import_and_push(tmp_path, store, monkeypatch, capsys):
    src = tmp_path / "src"
    (src / "V").mkdir(parents=True)
    (src / "C").mkdir()
    (src / "V" / "01-lisbon.mp4").write_bytes(b"v")
    (src / "C" / "01-lisbon-cover.jpg").write_bytes(b"c")
    (src / "d.txt").write_text("01 · What if Lisbon?\nCaption #a\n", encoding="utf-8")

    def fake_import(*args):
        return import_queue(*args, probe_fn=lambda p: VideoProbe(65, 1080, 1920, 1, "h264", "aac"),
                            teaser_fn=lambda s, d, sec: d.write_bytes(b"t"))

    monkeypatch.setattr(cli, "import_queue", fake_import)
    assert cli.main(["import", str(src / "V"), str(src / "C"), str(src / "d.txt")]) == 0
    assert "01-lisbon" in capsys.readouterr().out
    assert cli.main(["push-queue"]) == 0
    assert store.list_slugs() == ["01-lisbon"]


def test_missing_captions_file_returns_2(tmp_path):
    assert cli.main(["import", str(tmp_path), str(tmp_path), str(tmp_path / "nope.txt")]) == 2
