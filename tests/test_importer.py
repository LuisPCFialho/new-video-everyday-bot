import json
import os
import zipfile

import pytest

from reels_bot.importer import import_queue, slug_sort_key
from reels_bot.probe import ProbeError, VideoProbe

CAPTIONS = """01 · What if Lisbon?
On 1 November 1755…
#whatif
========================================
02 · What if Vesuvius?
Vesuvius buried Pompeii.
#whatif
========================================
14 · What if Titanic?
April 14, 1912.
#titanic
"""

GOOD = VideoProbe(65.0, 1080, 1920, 6, "h264", "aac")


def fake_probe(path):
    return GOOD


def fake_teaser(src, dst, seconds):
    fake_teaser.calls.append(src.parent.name)
    dst.write_bytes(b"teaser")


@pytest.fixture(autouse=True)
def reset_calls():
    fake_teaser.calls = []


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "src"
    (root / "Videos").mkdir(parents=True)
    (root / "Covers").mkdir()
    for slug in ("01-lisbon", "02-vesuvius"):
        (root / "Videos" / f"{slug}.mp4").write_bytes(b"video!")
    for slug in ("01-lisbon", "02-vesuvius", "14-titanic"):
        (root / "Covers" / f"{slug}-cover.jpg").write_bytes(b"jpg")
    (root / "Descriptions.txt").write_text(CAPTIONS, encoding="utf-8")
    return root


def run(source, queue, **kw):
    return import_queue(source / "Videos", source / "Covers", source / "Descriptions.txt", queue, 15,
                        probe_fn=kw.get("probe_fn", fake_probe), teaser_fn=fake_teaser)


def test_import_from_folders_builds_queue(source, tmp_path):
    queue = tmp_path / "queue"
    report = run(source, queue)
    assert [i.slug for i in report.items] == ["01-lisbon", "02-vesuvius", "14-titanic"]
    item = queue / "01-lisbon"
    assert sorted(p.name for p in item.iterdir()) == ["caption.txt", "cover.jpg", "meta.json", "story.mp4", "video.mp4"]
    assert (item / "caption.txt").read_text(encoding="utf-8") == "On 1 November 1755…\n#whatif\n"
    meta = json.loads((item / "meta.json").read_text(encoding="utf-8"))
    assert meta["title"] == "What if Lisbon?" and meta["scheduled_for"] is None
    assert meta["probe"]["video_codec"] == "h264"
    assert report.items[0].errors == []
    titanic = report.items[2]
    assert titanic.errors == ["video.mp4 em falta"]
    assert not (queue / "14-titanic" / "video.mp4").exists()


def test_reimport_is_idempotent_and_keeps_scheduled_for(source, tmp_path):
    queue = tmp_path / "queue"
    run(source, queue)
    meta_path = queue / "01-lisbon" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta_path.write_text(json.dumps({**meta, "scheduled_for": "2026-12-01"}), encoding="utf-8")
    fake_teaser.calls = []
    run(source, queue)
    assert fake_teaser.calls == []  # stories já actualizadas não são refeitas
    assert json.loads(meta_path.read_text(encoding="utf-8"))["scheduled_for"] == "2026-12-01"


def test_new_video_regenerates_story(source, tmp_path):
    queue = tmp_path / "queue"
    run(source, queue)
    video = source / "Videos" / "01-lisbon.mp4"
    video.write_bytes(b"new video!!")
    future = (queue / "01-lisbon" / "story.mp4").stat().st_mtime + 100
    os.utime(video, (future, future))
    fake_teaser.calls = []
    run(source, queue)
    assert fake_teaser.calls == ["01-lisbon"]
    assert (queue / "01-lisbon" / "video.mp4").read_bytes() == b"new video!!"


def test_renamed_slug_removes_stale_folder(source, tmp_path):
    queue = tmp_path / "queue"
    run(source, queue)
    (source / "Videos" / "02-vesuvius.mp4").rename(source / "Videos" / "02-vesuvio.mp4")
    (source / "Covers" / "02-vesuvius-cover.jpg").rename(source / "Covers" / "02-vesuvio-cover.jpg")
    report = run(source, queue)
    assert report.removed == ["02-vesuvius"]
    assert (queue / "02-vesuvio").is_dir() and not (queue / "02-vesuvius").exists()


def test_warnings_for_orphans_and_slug_mismatch(source, tmp_path):
    (source / "Videos" / "03-orphan.mp4").write_bytes(b"x")
    (source / "Covers" / "01-lisboa-cover.jpg").write_bytes(b"x")
    (source / "Covers" / "01-lisbon-cover.jpg").unlink()
    report = run(source, tmp_path / "queue")
    assert any("03" in w and "legenda" in w for w in report.warnings)
    assert any("slug" in w for w in report.warnings)


def test_import_from_zips_ignores_paths(source, tmp_path):
    vz, cz = tmp_path / "videos.zip", tmp_path / "covers.zip"
    with zipfile.ZipFile(vz, "w") as z:
        z.writestr("../../evil/01-lisbon.mp4", b"video!")
        z.writestr("__MACOSX/._02-vesuvius.mp4", b"junk")
        z.writestr("folder/02-vesuvius.mp4", b"video!")
    with zipfile.ZipFile(cz, "w") as z:
        z.writestr("01-lisbon-cover.jpg", b"jpg")
    queue = tmp_path / "queue"
    report = import_queue(vz, cz, source / "Descriptions.txt", queue, 15, probe_fn=fake_probe, teaser_fn=fake_teaser)
    assert (queue / "01-lisbon" / "video.mp4").read_bytes() == b"video!"
    assert not (tmp_path / "evil").exists()
    assert report.items[1].errors == ["cover.jpg em falta"]


def test_probe_and_story_errors_are_reported(source, tmp_path):
    def bad_probe(path):
        raise ProbeError("ffprobe falhou: moov atom not found")

    def bad_teaser(src, dst, seconds):
        raise ProbeError("ffmpeg não gerou a story")

    report = import_queue(source / "Videos", source / "Covers", source / "Descriptions.txt", tmp_path / "q", 15,
                          probe_fn=bad_probe, teaser_fn=bad_teaser)
    assert report.items[0].errors == ["ffprobe falhou: moov atom not found", "ffmpeg não gerou a story"]


def test_duplicate_numbers_in_source(source, tmp_path):
    (source / "Videos" / "01-other.mp4").write_bytes(b"x")
    with pytest.raises(ValueError, match="01"):
        run(source, tmp_path / "queue")


def test_slug_sort_key_is_numeric():
    assert sorted(["100-a", "11-b", "02-c", "junk"], key=slug_sort_key) == ["02-c", "11-b", "100-a", "junk"]


def test_empty_captions_file_does_not_wipe_queue(source, tmp_path):
    queue = tmp_path / "queue"
    run(source, queue)
    (source / "Descriptions.txt").write_text("\n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Nenhuma legenda"):
        run(source, queue)
    assert (queue / "01-lisbon").is_dir()


def test_non_ascii_and_spaces_give_valid_slugs(source, tmp_path):
    (source / "Videos" / "02-vesuvius.mp4").rename(source / "Videos" / "02-Vesúvio Erupção.mp4")
    (source / "Covers" / "02-vesuvius-cover.jpg").rename(source / "Covers" / "02-Vesúvio Erupção-cover.jpg")
    (source / "Covers" / "14-titanic-cover.jpg").unlink()
    (source / "Descriptions.txt").write_text(CAPTIONS.replace("14 · What if Titanic?", "14 · Açores à noite"),
                                             encoding="utf-8")
    report = run(source, tmp_path / "queue")
    assert [i.slug for i in report.items] == ["01-lisbon", "02-vesuvio-erupcao", "14-acores-a-noite"]


def test_invalid_scheduled_for_reported(source, tmp_path):
    queue = tmp_path / "queue"
    run(source, queue)
    meta_path = queue / "01-lisbon" / "meta.json"
    meta_path.write_text(json.dumps({"scheduled_for": "dia 5"}), encoding="utf-8")
    report = run(source, queue)
    assert any("scheduled_for inválido" in e for e in report.items[0].errors)
