from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from conftest import make_settings

from reels_bot.slots import CATCHUP, LOOKAHEAD, daily_slots, due_slot

LX = ZoneInfo("Europe/Lisbon")
UTC = ZoneInfo("UTC")
SETTINGS = make_settings(start_date=date(2026, 10, 9))


def test_first_day_only_last_window_and_nothing_before():
    assert daily_slots(date(2026, 10, 8), SETTINGS) == []
    (only,) = daily_slots(date(2026, 10, 9), SETTINGS)
    assert 19 <= only.hour < 23


def test_two_slots_inside_windows_and_varying_daily():
    seen = set()
    for offset in range(1, 60):
        day = date(2026, 10, 9) + timedelta(days=offset)
        noon, evening = daily_slots(day, SETTINGS)
        assert datetime.combine(day, datetime.min.time(), LX).replace(hour=12) <= noon < noon.replace(hour=15, minute=0)
        assert 19 <= evening.hour < 23
        seen.add(noon.strftime("%H:%M"))
    assert len(seen) > 40  # horas diferentes quase todos os dias


def test_slots_are_deterministic_and_seed_dependent():
    day = date(2026, 11, 3)
    assert daily_slots(day, SETTINGS) == daily_slots(day, SETTINGS)
    assert daily_slots(day, SETTINGS) != daily_slots(day, make_settings(start_date=date(2026, 10, 9), slot_seed="x"))


def test_due_slot_window():
    slot = daily_slots(date(2026, 10, 20), SETTINGS)[0]
    assert due_slot(slot - LOOKAHEAD, SETTINGS) == slot
    assert due_slot(slot + CATCHUP, SETTINGS) == slot
    assert due_slot(slot - LOOKAHEAD - timedelta(minutes=1), SETTINGS) is None
    assert due_slot(slot + CATCHUP + timedelta(minutes=1), SETTINGS) is None


def test_cron_covers_every_slot_in_both_dst_seasons():
    """Cada slot do próximo ano tem pelo menos 2 disparos do cron (13,43 11-15,18-23 UTC) que o apanham."""
    runs_per_day = [(h, m) for h in [*range(11, 16), *range(18, 24)] for m in (13, 43)]
    for offset in range(365):
        day = date(2026, 10, 10) + timedelta(days=offset)
        for slot in daily_slots(day, SETTINGS):
            utc_day = slot.astimezone(UTC).date()
            runs = [datetime(utc_day.year, utc_day.month, utc_day.day, h, m, tzinfo=UTC) for h, m in runs_per_day]
            catching = [r for r in runs if slot - LOOKAHEAD <= r <= slot + CATCHUP]
            assert len(catching) >= 2, (slot, catching)
