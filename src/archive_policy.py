"""Which attachments the server may release once a verified local copy exists.

Shared by the laptop's `python -m archive release` (to pick candidates) and
scripts/archive_release.py on the server (to refuse anything else, judged from
the server's own latest snapshot): a record still being worked on keeps every
document on the server, whatever the laptop's database says.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# 主动撤回 不予登记 登记成功 终止登记 登记撤销 未续展失效 视为撤回 续展登记
TERMINAL_STATUSES = frozenset({5, 6, 7, 8, 9, 10, 11, 13})

# An older round kept under a dated name by src/attachments._archive_name()
_SUPERSEDED_RE = re.compile(r"_\d{4}-\d{2}-\d{2}(?:_\d+)?$")


def is_superseded(filename: str) -> bool:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return bool(_SUPERSEDED_RE.search(stem))


def releasable(
    filename: str,
    status_code: int | None,
    updated_at: datetime | None,
    *,
    days: int,
    now: datetime | None = None,
) -> bool:
    """True when the file is history rather than working material: an older
    round the platform no longer serves, or any document of a finished record
    the platform has not touched for `days` days."""
    if is_superseded(filename):
        return True
    if status_code not in TERMINAL_STATUSES or updated_at is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - updated_at).total_seconds() >= days * 86400
