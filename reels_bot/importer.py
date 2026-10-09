"""Constrói/espelha a pasta queue/ a partir da pasta-fonte (ou de ZIPs).

Fonte: vídeos `NN-slug.mp4`, capas `NN-slug-cover.jpg` e um .txt de legendas
separadas por linhas de '=' (cada uma começa por `NN · Título`).
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import unicodedata
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from .captions import caption_errors, parse_captions
from .probe import ProbeError, VideoProbe, make_teaser, probe_video, video_errors

VIDEO_RE = re.compile(r"^(\d{1,3})-(.+)\.mp4$", re.IGNORECASE)
COVER_RE = re.compile(r"^(\d{1,3})-(.+)-cover\.jpe?g$", re.IGNORECASE)
ITEM_DIR_RE = re.compile(r"^(\d{1,3})-[\w.-]+$")

ProbeFn = Callable[[Path], VideoProbe]
TeaserFn = Callable[[Path, Path, float], None]


@dataclass
class ItemReport:
    slug: str
    title: str
    errors: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    items: list[ItemReport] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def slug_sort_key(slug: str) -> tuple[int, str]:
    """Ordena pelo número (01, 02, … 100) e não alfabeticamente."""
    match = ITEM_DIR_RE.match(slug)
    return (int(match.group(1)) if match else 10**6, slug)


def item_errors(caption: str, meta: dict, has_video: bool, has_cover: bool, has_story: bool) -> list[str]:
    """Validação completa de um item (usada no import e antes de publicar)."""
    errors = caption_errors(caption)
    if not has_video:
        errors.append("video.mp4 em falta")
    elif meta.get("probe_error"):
        errors.append(meta["probe_error"])
    elif not meta.get("probe"):
        errors.append("vídeo ainda não analisado (corre `import`)")
    else:
        errors.extend(video_errors(VideoProbe.from_dict(meta["probe"])))
    if not has_cover:
        errors.append("cover.jpg em falta")
    if has_video and not has_story:
        errors.append(meta.get("story_error") or "story.mp4 em falta (corre `import`)")
    when = meta.get("scheduled_for")
    if when:
        try:
            datetime.fromisoformat(str(when))
        except ValueError:
            errors.append(f"scheduled_for inválido: {when!r} (usa AAAA-MM-DD)")
    return errors


def _ascii_slug(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "item"


def _collect(source: Path, pattern: re.Pattern, tmp: Path) -> dict[int, Path]:
    """Ficheiros que casam com `pattern`, de uma pasta ou de um ZIP, por número."""
    found: dict[int, Path] = {}
    if source.is_dir():
        candidates = [p for p in source.iterdir() if p.is_file()]
    elif zipfile.is_zipfile(source):
        candidates = []
        out_dir = Path(tempfile.mkdtemp(dir=tmp))
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                name = PurePosixPath(info.filename).name  # ignora caminhos (evita path traversal)
                if info.is_dir() or name.startswith(".") or not pattern.match(name):
                    continue
                target = out_dir / name
                with zf.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                candidates.append(target)
    else:
        raise FileNotFoundError(f"Não é pasta nem ZIP: {source}")
    for path in candidates:
        match = pattern.match(path.name)
        if not match:
            continue
        number = int(match.group(1))
        if number in found:
            raise ValueError(f"Dois ficheiros com o número {number:02d}: {found[number].name}, {path.name}")
        found[number] = path
    return found


def _copy_if_changed(src: Path, dst: Path) -> None:
    if dst.exists():
        s, d = src.stat(), dst.stat()
        if s.st_size == d.st_size and int(s.st_mtime) == int(d.st_mtime):
            return
    shutil.copy2(src, dst)


def _sync_file(src: Path | None, dst: Path) -> None:
    if src is None:
        dst.unlink(missing_ok=True)
    else:
        _copy_if_changed(src, dst)


def _sync_story(folder: Path, seconds: float, teaser_fn: TeaserFn) -> str | None:
    """Gera story.mp4 se não existir ou se o vídeo for mais recente. Devolve o erro, se houver."""
    video, story = folder / "video.mp4", folder / "story.mp4"
    if not video.exists():
        story.unlink(missing_ok=True)
        return None
    if story.exists() and story.stat().st_mtime >= video.stat().st_mtime:
        return None
    try:
        teaser_fn(video, story, seconds)
    except ProbeError as exc:
        story.unlink(missing_ok=True)
        return str(exc)
    return None


def _build_meta(folder: Path, title: str, video: Path | None, probe_fn: ProbeFn,
                story_error: str | None) -> dict:
    meta_path = folder / "meta.json"
    old = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    meta = {"title": title, "scheduled_for": old.get("scheduled_for"), "probe": None,
            "probe_error": None, "story_error": story_error}
    if video is not None:
        try:
            meta["probe"] = probe_fn(folder / "video.mp4").to_dict()
        except ProbeError as exc:
            meta["probe_error"] = str(exc)
    return meta


def import_queue(
    videos: Path,
    covers: Path,
    captions_txt: Path,
    queue_dir: Path,
    teaser_seconds: float,
    probe_fn: ProbeFn = probe_video,
    teaser_fn: TeaserFn = make_teaser,
) -> ImportReport:
    report = ImportReport()
    entries = parse_captions(captions_txt.read_text(encoding="utf-8-sig"))
    if not entries:
        # Protege contra apagar a fila inteira por causa de um ficheiro vazio/errado.
        raise ValueError(f"Nenhuma legenda encontrada em {captions_txt}: nada foi alterado.")
    queue_dir.mkdir(parents=True, exist_ok=True)
    produced: set[str] = set()

    with tempfile.TemporaryDirectory() as tmp:
        video_files = _collect(videos, VIDEO_RE, Path(tmp))
        cover_files = _collect(covers, COVER_RE, Path(tmp))
        caption_numbers = {e.number for e in entries}
        for number in sorted((set(video_files) | set(cover_files)) - caption_numbers):
            report.warnings.append(f"{number:02d}: tem vídeo/capa mas não tem legenda — ignorado")

        for entry in entries:
            video, cover = video_files.get(entry.number), cover_files.get(entry.number)
            v_slug = _ascii_slug(VIDEO_RE.match(video.name).group(2)) if video else None
            c_slug = _ascii_slug(COVER_RE.match(cover.name).group(2)) if cover else None
            if v_slug and c_slug and v_slug != c_slug:
                report.warnings.append(f"{entry.number:02d}: slug do vídeo ({v_slug}) ≠ slug da capa ({c_slug})")
            slug = f"{entry.number:02d}-{v_slug or c_slug or _ascii_slug(entry.title)}"
            folder = queue_dir / slug
            folder.mkdir(exist_ok=True)

            _sync_file(video, folder / "video.mp4")
            _sync_file(cover, folder / "cover.jpg")
            story_error = _sync_story(folder, teaser_seconds, teaser_fn)
            (folder / "caption.txt").write_text(entry.body + "\n", encoding="utf-8", newline="\n")
            meta = _build_meta(folder, entry.title, video, probe_fn, story_error)
            (folder / "meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
            )
            produced.add(slug)
            errors = item_errors(entry.body, meta, video is not None, cover is not None,
                                 (folder / "story.mp4").exists())
            report.items.append(ItemReport(slug, entry.title, errors))

    for stale in queue_dir.iterdir():
        if stale.is_dir() and ITEM_DIR_RE.match(stale.name) and stale.name not in produced:
            shutil.rmtree(stale)
            report.removed.append(stale.name)
    report.items.sort(key=lambda i: slug_sort_key(i.slug))
    return report
