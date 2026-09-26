"""src/archive_policy.py: which files may leave the server for the local archive."""

from datetime import datetime, timedelta, timezone

import pytest

from src.archive_policy import TERMINAL_STATUSES, is_superseded, releasable

NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


@pytest.mark.parametrize("age_days, expected", [(89, False), (90, True), (91, True)])
def test_finished_records_wait_the_configured_days(age_days, expected):
    assert releasable("国家局登记证书.pdf", 7, NOW - timedelta(days=age_days), days=90, now=NOW) is expected


@pytest.mark.parametrize("status", [1, 2, 3, 4, 12, 14, 15, 16, 17, None])
def test_records_still_in_progress_keep_everything(status):
    assert releasable("x.pdf", status, NOW - timedelta(days=999), days=90, now=NOW) is False


def test_every_finished_status_counts():
    assert TERMINAL_STATUSES == {5, 6, 7, 8, 9, 10, 11, 13}
    for status in TERMINAL_STATUSES:
        assert releasable("x.pdf", status, NOW - timedelta(days=100), days=90, now=NOW)


def test_no_platform_update_time_means_no_release():
    assert releasable("x.pdf", 7, None, days=90, now=NOW) is False


def test_older_rounds_go_regardless_of_status_or_age():
    name = "《浙江省数据知识产权登记形式审查补正通知书》_2026-09-01.pdf"
    assert is_superseded(name)
    assert is_superseded("国家局登记证书_2026-07-19_2.pdf")
    assert not is_superseded("国家局登记证书.pdf")
    assert releasable(name, 2, NOW, days=90, now=NOW)  # the record is still 待补正


def test_zero_days_releases_as_soon_as_finished():
    assert releasable("x.pdf", 6, NOW, days=0, now=NOW)
