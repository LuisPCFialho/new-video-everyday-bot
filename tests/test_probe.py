import json
import subprocess

import pytest

from reels_bot.probe import ProbeError, VideoProbe, make_teaser, parse_probe, probe_video, video_errors


def ffprobe_json(duration=65.0, w=1080, h=1920, vcodec="h264", acodec="aac"):
    streams = [{"codec_type": "video", "codec_name": vcodec, "width": w, "height": h}]
    if acodec:
        streams.append({"codec_type": "audio", "codec_name": acodec})
    return {"format": {"duration": str(duration)}, "streams": streams}


def fake_runner(payload=None, returncode=0, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=json.dumps(payload or {}), stderr=stderr)

    run.calls = calls
    return run


def probe(**kw) -> VideoProbe:
    return parse_probe(ffprobe_json(**kw), size_bytes=50_000_000)


def test_valid_reel_has_no_errors(tmp_path):
    video = tmp_path / "video.mp4"
    video.write_bytes(b"x" * 10)
    runner = fake_runner(ffprobe_json(duration=88.83))
    p = probe_video(video, runner=runner)
    assert p == VideoProbe(88.83, 1080, 1920, 10, "h264", "aac")
    assert video_errors(p) == []
    assert runner.calls[0][0] == "ffprobe"


@pytest.mark.parametrize("kwargs, fragment", [
    ({"duration": 2.0}, "duração"),
    ({"duration": 90.5}, "duração"),
    ({"w": 1920, "h": 1080}, "9:16"),
    ({"vcodec": "hevc"}, "h264"),
    ({"acodec": "mp3"}, "aac"),
    ({"acodec": None}, "aac"),
])
def test_invalid_videos(kwargs, fragment):
    errors = video_errors(probe(**kwargs))
    assert len(errors) == 1 and fragment in errors[0]


def test_too_big():
    p = parse_probe(ffprobe_json(), size_bytes=1024**3 + 1)
    assert "1 GB" in video_errors(p)[0]


def test_no_video_stream():
    with pytest.raises(ProbeError, match="vídeo"):
        parse_probe({"format": {"duration": "5"}, "streams": [{"codec_type": "audio"}]}, 1)


def test_ffprobe_failure(tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    with pytest.raises(ProbeError, match="ffprobe falhou"):
        probe_video(video, runner=fake_runner(returncode=1, stderr="moov atom not found"))


def test_ffprobe_missing(tmp_path):
    def missing(*a, **k):
        raise FileNotFoundError

    with pytest.raises(ProbeError, match="não encontrado"):
        probe_video(tmp_path / "v.mp4", runner=missing)


def test_make_teaser_success_and_failure(tmp_path):
    src, dst = tmp_path / "video.mp4", tmp_path / "story.mp4"

    def ok(cmd, **kwargs):
        assert cmd[cmd.index("-t") + 1] == "15" and "afade=t=out:st=14:d=1" in cmd
        (tmp_path / "story.tmp.mp4").write_bytes(b"teaser")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    make_teaser(src, dst, 15, runner=ok)
    assert dst.read_bytes() == b"teaser"

    with pytest.raises(ProbeError, match="story"):
        make_teaser(src, tmp_path / "s2.mp4", 15, runner=fake_runner(returncode=1, stderr="bad"))
