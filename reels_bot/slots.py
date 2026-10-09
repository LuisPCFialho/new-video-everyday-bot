"""Horas de publicação aleatórias, uma por janela e por dia.

As horas são pseudo-aleatórias mas determinísticas (hash de semente + data +
janela): o GitHub Actions e o PC calculam sempre as mesmas, sem guardar nada.
Só usa a biblioteca padrão para o "gate" do workflow correr antes de instalar
dependências:  python3 -m reels_bot.slots
"""
from __future__ import annotations

import hashlib
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Settings, load_settings

# Ligados à cadência do cron no workflow (de 30 em 30 min):
CATCHUP = timedelta(minutes=75)    # um slot ainda é publicado se o disparo vier até 75 min depois
LOOKAHEAD = timedelta(minutes=31)  # um disparo apanha o slot que cai até ao disparo seguinte


def daily_slots(day: date, settings: Settings) -> list[datetime]:
    if settings.start_date and day < settings.start_date:
        return []
    windows = settings.windows[-1:] if day == settings.start_date else settings.windows
    tz = ZoneInfo(settings.timezone)
    slots = []
    for index, (start_s, end_s) in enumerate(windows):
        start = datetime.combine(day, time.fromisoformat(start_s), tzinfo=tz)
        end = datetime.combine(day, time.fromisoformat(end_s), tzinfo=tz)
        span = int((end - start).total_seconds() // 60)
        digest = hashlib.sha256(f"{settings.slot_seed}|{day.isoformat()}|{index}".encode()).digest()
        slots.append(start + timedelta(minutes=int.from_bytes(digest[:4], "big") % span))
    return slots


def due_slot(now: datetime, settings: Settings) -> datetime | None:
    """Slot em [now - CATCHUP, now + LOOKAHEAD], se houver."""
    today = now.astimezone(ZoneInfo(settings.timezone)).date()
    for day in (today - timedelta(days=1), today):
        for slot in daily_slots(day, settings):
            if now - CATCHUP <= slot <= now + LOOKAHEAD:
                return slot
    return None


def main() -> int:
    """Gate do workflow: escreve go=true/false (e o slot) em $GITHUB_OUTPUT."""
    slot = due_slot(datetime.now(timezone.utc), load_settings())
    lines = [f"go={'true' if slot else 'false'}"] + ([f"slot={slot.isoformat()}"] if slot else [])
    print("Próximo slot dentro da janela:" if slot else "Nenhum slot nesta meia hora — nada a fazer.",
          slot.isoformat() if slot else "")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
