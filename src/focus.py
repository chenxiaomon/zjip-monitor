"""关注范围：本年申请的全部，加上一年申请、还没有结果的。

Once most of a company's records are settled history, the counts and lists
that matter day to day are this year's applications and whatever from last
year is still in review. The console's year picker offers this next to
全部年份 and the single years, and settings.yaml → display.focus_default
makes it what the counting and listing pages show when no year is chosen:
the dashboard, the records page, the /view board and the mini program.

It narrows what is shown, never what is known. The crawl, the diff, notices,
待办, the deadline calendar, the change history, the ops banner and the
ledger export keep seeing every record, so an old certificate's renewal or a
correction deadline on an old application is never hidden.

"Still in review" is "not finished": anything outside archive_policy's
TERMINAL_STATUSES (待审核, 待补正, 公示中, 异议, 待发证, and codes the
platform may add later). A record without a usable application date is
always in focus — it cannot be ruled out.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .archive_policy import TERMINAL_STATUSES
from .scraper import int_or_none
from .settings_cache import get_settings

_SHANGHAI_TZ = timezone(timedelta(hours=8))


def this_year() -> int:
    """The current application year, by the Beijing calendar."""
    return datetime.now(_SHANGHAI_TZ).year


def apply_year(ms: Any) -> int | None:
    """The Beijing-calendar year of a record's dataRegApplyTime, else None."""
    ms = int_or_none(ms)
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, _SHANGHAI_TZ).year
    except (ValueError, OverflowError, OSError):
        return None


def in_focus(year: int | None, status_code: int | None, current: int) -> bool:
    """One record, by its application year and status, against the focus of
    `current` (the year it is now)."""
    if year is None or year >= current:
        return True
    return year == current - 1 and status_code not in TERMINAL_STATUSES


def record_in_focus(record: dict, current: int) -> bool:
    return in_focus(
        apply_year(record.get("dataRegApplyTime")),
        int_or_none(record.get("dataRegAuditStatus")),
        current,
    )


def focus_default() -> bool:
    """settings.yaml → display.focus_default: whether pages open on 关注范围
    rather than 全部年份. Only a real YAML `true` turns it on."""
    data = get_settings()
    display = data.get("display") if isinstance(data, dict) else None
    return isinstance(display, dict) and display.get("focus_default") is True


def describe(current: int) -> str:
    """What 关注范围 means this year, in words for a tooltip or a note."""
    return f"{current} 年申请的全部，加 {current - 1} 年申请、还在审的"
