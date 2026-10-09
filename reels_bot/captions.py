"""Parsing do ficheiro de legendas, validação e normalização para comparação."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_CAPTION_CHARS = 2200
MAX_HASHTAGS = 30

_SEPARATOR_RE = re.compile(r"^[ \t]*={8,}[ \t]*$", re.MULTILINE)
# "01 · Title" (aceita também "01 - Title", "01. Title", "01: Title")
_HEADER_RE = re.compile(r"^\s*(\d{1,3})\s*[·•\-–—.:]\s*(\S.*?)\s*$")
_HASHTAG_RE = re.compile(r"(?<![\w&/])#\w+", re.UNICODE)


@dataclass(frozen=True)
class CaptionEntry:
    number: int
    title: str
    body: str


def parse_captions(text: str) -> list[CaptionEntry]:
    """Divide o texto em blocos separados por linhas de '=' (8 ou mais).
    Cada bloco começa por 'NN · Título'; o resto é a legenda a publicar."""
    entries: list[CaptionEntry] = []
    seen: set[int] = set()
    for block in _SEPARATOR_RE.split(text.lstrip("﻿")):
        lines = block.strip().splitlines()
        if not lines:
            continue
        match = _HEADER_RE.match(lines[0])
        if not match:
            raise ValueError(f"Bloco sem cabeçalho 'NN · Título': {lines[0][:60]!r}")
        number = int(match.group(1))
        if number in seen:
            raise ValueError(f"Número {number:02d} repetido no ficheiro de legendas")
        seen.add(number)
        body = "\n".join(lines[1:]).strip()
        if not body:
            raise ValueError(f"Legenda {number:02d} está vazia")
        entries.append(CaptionEntry(number=number, title=match.group(2), body=body))
    return entries


def count_hashtags(caption: str) -> int:
    return len(_HASHTAG_RE.findall(caption))


def caption_errors(caption: str) -> list[str]:
    errors = []
    if not caption.strip():
        errors.append("legenda vazia")
    if len(caption) > MAX_CAPTION_CHARS:
        errors.append(f"legenda com {len(caption)} caracteres (máx. {MAX_CAPTION_CHARS})")
    tags = count_hashtags(caption)
    if tags > MAX_HASHTAGS:
        errors.append(f"legenda com {tags} hashtags (máx. {MAX_HASHTAGS})")
    return errors


def first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def normalize(text: str | None) -> str:
    """Minúsculas, sem emoji nem pontuação, espaços colapsados."""
    kept = [
        ch if unicodedata.category(ch)[0] in "LN" else " "
        for ch in unicodedata.normalize("NFC", (text or "").casefold())
    ]
    return " ".join("".join(kept).split())
