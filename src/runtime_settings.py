"""Typed, validated view of the runtime tunables in config/settings.yaml.

Every value here used to be a literal somewhere in src/ or web/: the serial
vs. concurrent threshold, crawl delays, retention limits, HTTP timeouts, the
"data may be stale" cut-off. The dataclass defaults ARE those literals, so a
settings.yaml without any of these keys behaves exactly as the code did
before. Keys whose consumers arrived later (disk.*, attachments.max_total_mb,
retention.*logs_keep_days) are defined here too, so the disk guard, the log
set-up and the console banner all read them through the same validation.

Reads go through settings_cache.get_settings() (mtime-cached), so an edit
takes effect at the next call without a restart. A value of the wrong type
or out of range falls back to its default and is logged ONCE per key — the
scraper asks for its page delay on every page, and one typo must not turn
the log into a firehose. The warning re-arms once the key reads valid again.

Type rules: an *integer* key rejects floats, bools and numeric strings; a
*number* key accepts int or float (finite) but still rejects bools/strings.
YAML `true` or "3" for a count is a typo, not something to coerce.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, fields, replace
from typing import Any, Callable, TypeVar

from loguru import logger

from .settings_cache import get_settings

_MIB = 1024 * 1024


def _mb_to_bytes(mb: float) -> int:
    return int(mb * _MIB)


# ---------------------------------------------------------------------------
# One frozen dataclass per settings.yaml section. Field defaults are the
# pre-configuration literals — tests assert this, do not "tidy" them.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RunSettings:
    """settings.yaml → run (main.run_all / _run_serial / _run_concurrent)."""

    # ≤ this many accounts run one after another; beyond it a thread pool.
    serial_max_accounts: int = 10
    # Pool size for the concurrent path. 3 is the historical value; on the
    # 1 vCPU production box 1–2 is the safer choice.
    max_workers: int = 3
    # Random pause between accounts on the serial path (politeness jitter).
    account_delay_min_seconds: float = 2.0
    account_delay_max_seconds: float = 5.0
    # Wall-clock ceiling per account on the concurrent path. Login (≤120s,
    # login.LOGIN_HARD_TIMEOUT_SECONDS) + paging + attachments normally finish
    # well under 2 minutes; 10 minutes is the "something is wedged" line and
    # the second line of defence that guarantees run_all() reaches
    # ctx.finish() and releases the global run lock.
    account_timeout_seconds: float = 600


@dataclass(frozen=True)
class RetentionSettings:
    """settings.yaml → retention (main._prune_data and the log pruning job)."""

    snapshots_keep_per_company: int = 30
    reports_keep_days: int = 60
    # events.jsonl is rotated once it grows past this many MiB…
    events_rotate_mb: float = 5
    # …and only this many rotated events-*.jsonl files are kept. They had no
    # cap at all once, on a root partition that has been 96% full.
    events_rotated_keep: int = 6
    logs_keep_days: int = 30        # logs/run_*.log
    web_logs_keep_days: int = 14    # logs/web_*.log

    @property
    def events_rotate_bytes(self) -> int:
        return _mb_to_bytes(self.events_rotate_mb)


@dataclass(frozen=True)
class ScraperSettings:
    """settings.yaml → scraper (the record-list crawl)."""

    page_delay_seconds: float = 0.5   # polite pause between list pages
    # Requested page length. The server ignores it and answers with a fixed
    # ~10 rows, so this is only ever a request parameter.
    page_size: int = 50
    request_timeout_seconds: float = 30
    # Safety cap: ~10 rows per page → 300 pages ≈ 3000 records. Paging stops
    # on an empty page or a page with no new records, never on this.
    max_pages: int = 300


@dataclass(frozen=True)
class AttachmentSettings:
    """settings.yaml → attachments (enabled / certificates_enabled keep their
    original read path)."""

    request_timeout_seconds: float = 30    # getNotification / confirmation
    download_timeout_seconds: float = 60   # the pre-signed OSS file fetch
    crawl_delay_seconds: float = 0.5       # pause after each record
    # Notices and certificates are small PDFs; anything this size is a bug or
    # a hostile URL, and the partition they land on has run at 96% full.
    max_file_mb: float = 50
    # Ops banner threshold for the whole data/attachments/ tree.
    max_total_mb: float = 1024

    @property
    def max_file_bytes(self) -> int:
        return _mb_to_bytes(self.max_file_mb)

    @property
    def max_total_bytes(self) -> int:
        return _mb_to_bytes(self.max_total_mb)


@dataclass(frozen=True)
class OpsAlertSettings:
    """settings.yaml → ops_alerts (dashboard staleness + ops banner)."""

    # No successful scan for this long → 数据可能过期 on the account card.
    stale_hours: float = 36
    # This many failed accounts in one round → banner.
    min_failed_accounts: int = 3
    # A 补正 whose deadline is this many days away (or fewer) → banner;
    # overdue or ≤ 2 days is critical either way.
    correction_warn_days: int = 5


@dataclass(frozen=True)
class LoginSettings:
    """settings.yaml → login, numeric keys only.

    `method`, `headless` and `max_consecutive_failures` keep their original
    read paths (they are strings/bools, or already coerced where they are
    used). This one is here because a quoted "12" reached
    `timedelta(hours=...)` and raised TypeError out of every single login.
    """

    token_max_age_hours: int = 12


@dataclass(frozen=True)
class DiskSettings:
    """settings.yaml → disk (disk guard)."""

    # Below this much free space: skip attachment downloads + banner.
    min_free_mb: float = 500


@dataclass(frozen=True)
class ArchiveSettings:
    """settings.yaml → archive (scripts/archive_release.py, src/archive_policy)."""

    # A finished record (登记成功/不予登记/撤回…) the platform has not touched
    # for this many days may have its files released from the server once
    # the laptop holds a verified copy. Older rounds are released regardless.
    release_after_terminal_days: int = 90


# ---------------------------------------------------------------------------
# Validation rules: field → (kind, lower bound, bound is exclusive)
# ---------------------------------------------------------------------------

_INT = "int"   # integers only
_NUM = "num"   # int or float, finite

_Rule = tuple[str, float, bool]

_RULES: dict[str, dict[str, _Rule]] = {
    "run": {
        # 0 is meaningful: "always use the pool".
        "serial_max_accounts": (_INT, 0, False),
        # ThreadPoolExecutor refuses max_workers <= 0.
        "max_workers": (_INT, 1, False),
        "account_delay_min_seconds": (_NUM, 0, False),
        "account_delay_max_seconds": (_NUM, 0, False),
        # asyncio.wait_for(timeout=0) would time every account out instantly.
        "account_timeout_seconds": (_NUM, 0, True),
    },
    "retention": {
        # Keep-counts must be ≥ 1: `files[:-0]` is `files[:0]`, so 0 would
        # silently prune nothing rather than everything — the opposite of
        # what an operator writing 0 would expect. Refuse it instead.
        "snapshots_keep_per_company": (_INT, 1, False),
        "reports_keep_days": (_INT, 1, False),
        "events_rotate_mb": (_NUM, 0, True),
        "events_rotated_keep": (_INT, 1, False),
        "logs_keep_days": (_INT, 1, False),
        "web_logs_keep_days": (_INT, 1, False),
    },
    "scraper": {
        "page_delay_seconds": (_NUM, 0, False),
        "page_size": (_INT, 1, False),
        "request_timeout_seconds": (_NUM, 0, True),
        "max_pages": (_INT, 1, False),
    },
    "attachments": {
        "request_timeout_seconds": (_NUM, 0, True),
        "download_timeout_seconds": (_NUM, 0, True),
        "crawl_delay_seconds": (_NUM, 0, False),
        "max_file_mb": (_NUM, 0, True),
        "max_total_mb": (_NUM, 0, True),
    },
    "ops_alerts": {
        "stale_hours": (_NUM, 0, True),
        "min_failed_accounts": (_INT, 1, False),
        # 0 is meaningful: only warn once a deadline is today or past.
        "correction_warn_days": (_INT, 0, False),
    },
    "disk": {
        "min_free_mb": (_NUM, 0, False),
    },
    "login": {
        # A cached token older than this is refreshed; 0 would mean "always
        # expired", i.e. a fresh login for every single request.
        "token_max_age_hours": (_INT, 1, False),
    },
    "archive": {
        # 0 is meaningful: "as soon as the record is finished".
        "release_after_terminal_days": (_INT, 0, False),
    },
}

_CLASSES: dict[str, type] = {
    "run": RunSettings,
    "retention": RetentionSettings,
    "scraper": ScraperSettings,
    "attachments": AttachmentSettings,
    "ops_alerts": OpsAlertSettings,
    "disk": DiskSettings,
    "login": LoginSettings,
    "archive": ArchiveSettings,
}

# Every rule table covers exactly its dataclass — checked at import so a new
# field cannot be added without a validation rule (or vice versa).
for _section_name, _cls in _CLASSES.items():
    _declared = {f.name for f in fields(_cls)}
    _ruled = set(_RULES[_section_name])
    if _declared != _ruled:
        raise RuntimeError(
            f"runtime_settings: rules for `{_section_name}` do not match "
            f"{_cls.__name__} fields: {sorted(_declared ^ _ruled)}"
        )


# ---------------------------------------------------------------------------
# Warn-once bookkeeping
# ---------------------------------------------------------------------------

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


def reset_warnings() -> None:
    """Forget which keys have already been warned about (tests)."""
    with _warned_lock:
        _warned.clear()


def _warn_once(section: str, key: str, message: str) -> None:
    with _warned_lock:
        if (section, key) in _warned:
            return
        _warned.add((section, key))
    logger.warning(message)


def _clear_warning(section: str, key: str) -> None:
    with _warned_lock:
        _warned.discard((section, key))


# ---------------------------------------------------------------------------
# Reading + validation
# ---------------------------------------------------------------------------

def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_num(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        # A YAML integer with hundreds of digits cannot be converted to a
        # float. It is not a usable delay or timeout either, so it is simply
        # invalid — letting the OverflowError escape would abort the caller
        # (run_all reads these while holding the global run lock).
        return False


def _section(name: str) -> dict:
    """The raw mapping for one section; {} when absent, empty or malformed."""
    data = get_settings()
    raw = data.get(name) if isinstance(data, dict) else None
    if raw is None:
        # Absent, or `run:` with nothing under it — both mean "all defaults".
        _clear_warning(name, "")
        return {}
    if not isinstance(raw, dict):
        _warn_once(
            name, "",
            f"settings.yaml → {name} 应为一组「键: 值」，实际是 {type(raw).__name__}"
            f"（{raw!r:.60}）— 该组全部使用默认值",
        )
        return {}
    _clear_warning(name, "")
    return raw


def _read(raw: dict, section: str, key: str, default: Any, rule: _Rule) -> Any:
    if key not in raw:
        _clear_warning(section, key)
        return default
    value = raw[key]
    kind, lower, exclusive = rule
    ok = _is_int(value) if kind == _INT else _is_num(value)
    if ok:
        ok = value > lower if exclusive else value >= lower
    if not ok:
        _warn_once(
            section, key,
            f"settings.yaml → {section}.{key} = {value!r:.60} 无效"
            f"（应为{'整数' if kind == _INT else '数字'}，"
            f"{'>' if exclusive else '≥'} {lower:g}）— 使用默认值 {default}",
        )
        return default
    _clear_warning(section, key)
    return value


T = TypeVar("T")


def _build(section: str, cls: Callable[..., T]) -> T:
    raw = _section(section)
    defaults = cls()
    rules = _RULES[section]
    return cls(**{
        f.name: _read(raw, section, f.name, getattr(defaults, f.name), rules[f.name])
        for f in fields(cls)  # type: ignore[arg-type]
    })


# ---------------------------------------------------------------------------
# Public getters — one per section, cheap enough to call per request/page
# ---------------------------------------------------------------------------

def run_settings() -> RunSettings:
    s = _build("run", RunSettings)
    if s.account_delay_max_seconds < s.account_delay_min_seconds:
        d = RunSettings()
        _warn_once(
            "run", "account_delay_*",
            f"settings.yaml → run.account_delay_max_seconds ({s.account_delay_max_seconds:g}) "
            f"小于 account_delay_min_seconds ({s.account_delay_min_seconds:g}) — "
            f"两项均使用默认值 {d.account_delay_min_seconds:g}~{d.account_delay_max_seconds:g} 秒",
        )
        return replace(
            s,
            account_delay_min_seconds=d.account_delay_min_seconds,
            account_delay_max_seconds=d.account_delay_max_seconds,
        )
    _clear_warning("run", "account_delay_*")
    return s


def retention_settings() -> RetentionSettings:
    return _build("retention", RetentionSettings)


def scraper_settings() -> ScraperSettings:
    return _build("scraper", ScraperSettings)


def attachment_settings() -> AttachmentSettings:
    return _build("attachments", AttachmentSettings)


def ops_alert_settings() -> OpsAlertSettings:
    return _build("ops_alerts", OpsAlertSettings)


def disk_settings() -> DiskSettings:
    return _build("disk", DiskSettings)


def login_settings() -> LoginSettings:
    return _build("login", LoginSettings)


def archive_settings() -> ArchiveSettings:
    return _build("archive", ArchiveSettings)
