"""Leitura das características do vídeo com ffprobe e validação para Reels."""
from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

MIN_DURATION_S = 3.0
MAX_DURATION_S = 90.0
MAX_SIZE_BYTES = 1024**3  # 1 GB
TARGET_RATIO = 9 / 16
RATIO_TOLERANCE = 0.01

Runner = Callable[..., subprocess.CompletedProcess]


class ProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class VideoProbe:
    duration_s: float
    width: int
    height: int
    size_bytes: int
    video_codec: str
    audio_codec: str | None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> VideoProbe:
        return cls(**data)


def probe_video(path: Path, runner: Runner = subprocess.run) -> VideoProbe:
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries", "format=duration:stream=codec_type,codec_name,width,height",
        str(path),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=60, check=False)
    except FileNotFoundError as exc:
        raise ProbeError("ffprobe não encontrado (instala o ffmpeg)") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe falhou em {path.name}: {proc.stderr.strip()[:200]}")
    return parse_probe(json.loads(proc.stdout), size_bytes=path.stat().st_size)


def make_teaser(src: Path, dst: Path, seconds: float, runner: Runner = subprocess.run) -> None:
    """Primeiros `seconds` do vídeo, re-encodados (corte exacto) com fade-out no áudio."""
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    cmd = [
        "ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", f"{seconds:g}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", "-af", f"afade=t=out:st={max(seconds - 1, 0):g}:d=1",
        "-movflags", "+faststart", str(tmp),
    ]
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=300, check=False)
    except FileNotFoundError as exc:
        raise ProbeError("ffmpeg não encontrado (instala o ffmpeg)") from exc
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise ProbeError(f"ffmpeg não gerou a story de {src.parent.name}: {proc.stderr.strip()[:200]}")
    tmp.replace(dst)


def parse_probe(data: dict, size_bytes: int) -> VideoProbe:
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ProbeError("ficheiro sem faixa de vídeo")
    return VideoProbe(
        duration_s=round(float(data.get("format", {}).get("duration", 0)), 3),
        width=int(video.get("width", 0)),
        height=int(video.get("height", 0)),
        size_bytes=size_bytes,
        video_codec=video.get("codec_name", ""),
        audio_codec=audio.get("codec_name") if audio else None,
    )


def video_errors(p: VideoProbe) -> list[str]:
    errors = []
    if not MIN_DURATION_S <= p.duration_s <= MAX_DURATION_S:
        errors.append(f"duração {p.duration_s:.1f} s fora de {MIN_DURATION_S:.0f}–{MAX_DURATION_S:.0f} s")
    if p.height == 0 or abs(p.width / p.height - TARGET_RATIO) > RATIO_TOLERANCE:
        errors.append(f"resolução {p.width}×{p.height} não é 9:16")
    if p.size_bytes > MAX_SIZE_BYTES:
        errors.append(f"ficheiro com {p.size_bytes / 1024**2:.0f} MB (máx. 1 GB)")
    if p.video_codec != "h264":
        errors.append(f"codec de vídeo {p.video_codec!r} (tem de ser h264)")
    if p.audio_codec != "aac":
        errors.append(f"codec de áudio {p.audio_codec!r} (tem de ser aac)")
    return errors
