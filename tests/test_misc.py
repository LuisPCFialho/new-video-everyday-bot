"""Estado, logs, config e utilitários do setup."""
import logging
from datetime import date

import pytest

from reels_bot.config import ConfigError, load_secrets, load_settings
from reels_bot.logs import purge_old_logs, setup_logging
from reels_bot.setup_wizard import pick_instagram, read_env, write_env
from reels_bot.state import empty_state, record_failure, record_success, resume, with_fields


def test_failures_pause_after_max_and_resume():
    s1 = record_failure(empty_state(), "a", 2)
    assert s1["consecutive_failures"] == 1 and not s1["paused"]
    s2 = record_failure(s1, "b", 2)
    assert s2["paused"] and "b" in s2["pause_reason"]
    assert record_success(s1)["consecutive_failures"] == 0
    r = resume(s2)
    assert not r["paused"] and r["consecutive_failures"] == 0
    assert s2["paused"]  # imutável


def test_record_failure_keeps_in_flight():
    s = with_fields(empty_state(), in_flight={"slug": "x", "container_id": "C"})
    assert record_failure(s, "e", 2)["in_flight"]["container_id"] == "C"
    assert record_success(s)["in_flight"] is None


def test_purge_old_logs(tmp_path):
    for name in ("2026-08-01.log", "2026-09-10.log", "2026-10-09.log", "notes.log", "2026-99-99.log"):
        (tmp_path / name).write_text("x")
    removed = purge_old_logs(tmp_path, date(2026, 10, 9), 30)
    assert [p.name for p in removed] == ["2026-08-01.log"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-09-10.log", "2026-10-09.log", "2026-99-99.log", "notes.log"]


def test_setup_logging_creates_daily_file_and_silences_httpx(tmp_path):
    setup_logging(tmp_path, 30, today=date(2026, 10, 9))
    logging.getLogger("reels_bot").info("olá")
    assert (tmp_path / "2026-10-09.log").exists()
    assert logging.getLogger("httpx").level == logging.WARNING
    logging.shutdown()


def test_load_settings_from_repo_config():
    s = load_settings()
    assert s.timezone == "Europe/Lisbon" and len(s.windows) == 2 and s.publish_first == (1, 16)
    assert s.story_teaser_seconds == 15 and s.max_consecutive_failures == 2


def test_load_settings_invalid(tmp_path):
    bad = tmp_path / "c.toml"
    bad.write_text("[schedule]\n")
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_load_secrets_lists_missing():
    with pytest.raises(ConfigError, match="IG_USER_ID"):
        load_secrets({"META_ACCESS_TOKEN": "t"})
    env = {k: "v" for k in ("META_ACCESS_TOKEN", "IG_USER_ID", "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
                            "R2_SECRET_ACCESS_KEY", "R2_BUCKET")}
    assert load_secrets(env).app_id is None


def test_pick_instagram():
    pages = [{"name": "Outra", "instagram_business_account": {"id": "1", "username": "other"}},
             {"name": "Sem IG"},
             {"name": "NVE", "instagram_business_account": {"id": "178", "username": "New.Video.Everyday"}}]
    page, ig = pick_instagram(pages, "new.video.everyday")
    assert page["name"] == "NVE" and ig["id"] == "178"
    with pytest.raises(RuntimeError, match="@other"):
        pick_instagram(pages[:2], "new.video.everyday")


def test_write_env_merges(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nKEEP=1\nR2_BUCKET=old\n")
    write_env(env, {"R2_BUCKET": "new", "EMPTY": ""})
    assert read_env(env) == {"KEEP": "1", "R2_BUCKET": "new"}
