"""Which application years a counting or listing page shows: 全部年份, one
year, or 关注范围 (src/focus.py).

?year= takes "all", "focus" or a four-digit year. Without it the console
pages fall back to the default — 关注范围 when settings.yaml →
display.focus_default is on, 全部年份 otherwise — and the links they build
leave the parameter out whenever the choice IS the default, so URLs read as
they always did until someone picks something else. The mini program's
/api endpoints keep their own default (全部年份, what old builds expect) and
resolve ?year=default to the console's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src import focus
from src.scraper import int_or_none

ALL = "all"
FOCUS = "focus"
DEFAULT = "default"


@dataclass(frozen=True)
class YearScope:
    kind: str                 # ALL | FOCUS | "year"
    year: int | None          # the year when kind == "year"
    current: int              # the year it is now (Beijing)
    is_default: bool          # what the page shows without ?year=

    @property
    def value(self) -> str:
        """The ?year= value that selects this scope explicitly."""
        return str(self.year) if self.kind == "year" else self.kind

    @property
    def query(self) -> str | None:
        """The ?year= value for links, None when it is the page's default."""
        return None if self.is_default else self.value

    @property
    def stats_key(self) -> int | str | None:
        """What daily_stats.series() filters by."""
        return self.year if self.kind == "year" else (FOCUS if self.kind == FOCUS else None)

    @property
    def is_all(self) -> bool:
        return self.kind == ALL

    @property
    def label(self) -> str:
        """Short, for a metric card's note or the trend legend."""
        if self.kind == FOCUS:
            return f"关注范围（{self.current} 年申请 + {self.current - 1} 年在审）"
        if self.kind == "year":
            return f"{self.year} 年申请"
        return "全部年份"

    @property
    def describe(self) -> str:
        return focus.describe(self.current)

    def matches(self, record: dict) -> bool:
        if self.kind == ALL:
            return True
        if self.kind == FOCUS:
            return focus.record_in_focus(record, self.current)
        return focus.apply_year(record.get("dataRegApplyTime")) == self.year


def default_kind() -> str:
    return FOCUS if focus.focus_default() else ALL


def resolve(raw: Any, *, default: str | None = None, current: int | None = None) -> YearScope:
    """?year= → a YearScope. `default` is what a missing, blank or unreadable
    value means (the console's setting unless given); a value of "default"
    asks for the console's setting whatever `default` says."""
    current = current or focus.this_year()
    console_default = default_kind()
    fallback = console_default if default is None else default
    text = str(raw).strip().lower() if raw is not None else ""
    if text == DEFAULT:
        text = console_default
    if text in (ALL, FOCUS):
        kind, year = text, None
    elif (y := int_or_none(text)) is not None and 1900 < y < 3000:
        kind, year = "year", y
    else:
        kind, year = (fallback if fallback in (ALL, FOCUS) else ALL), None
    page_default = fallback if fallback in (ALL, FOCUS) else ALL
    return YearScope(kind=kind, year=year, current=current,
                     is_default=(kind == page_default and year is None))
