"""The dashboard's 账号状态 layout — settings.yaml → display.dashboard.

An admin decides, for everyone, which 来源 (group) comes first, the order of
the companies inside each one, and which companies are folded away under
「已隐藏」:

    display:
      dashboard:
        group_order: [group_b, group_a, ""]   # "" = 未分组
        account_order: [login1, login2]       # platform logins, as status_store keys them
        hidden: [login3]

Only the cards follow it. Every count above them, the 待办, the timeline and
the banners still include hidden companies: hiding is "not on my wall", not
"not my business". Each viewer gets the layout applied to the companies they
may see; it never widens what anyone sees.

Reads are lenient (a missing or malformed section is the default layout);
saves go through settings_svc.editing(), the strict read-modify-write every
console save uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from src.config_loader import Account
from src.settings_cache import get_settings
from web.deps import get_groups, group_label
from web.services import settings_svc
from web.services.settings_svc import save_settings

UNGROUPED = ""


@dataclass
class Layout:
    group_order: list[str] = field(default_factory=list)
    account_order: list[str] = field(default_factory=list)
    hidden: set[str] = field(default_factory=set)

    @property
    def is_default(self) -> bool:
        return not (self.group_order or self.account_order or self.hidden)


@dataclass
class Segment:
    key: str
    label: str
    items: list


def _strings(value: Any) -> list[str]:
    """A YAML list as unique strings in order; anything else is empty."""
    if not isinstance(value, list):
        return []
    seen: dict[str, None] = {}
    for v in value:
        if v is None:
            v = ""  # `- ` / `- null` in a hand edit means 未分组
        if isinstance(v, (str, int)) and not isinstance(v, bool):
            seen.setdefault(str(v).strip(), None)
    return list(seen)


def _section(data: Any) -> dict:
    display = data.get("display") if isinstance(data, dict) else None
    section = display.get("dashboard") if isinstance(display, dict) else None
    return section if isinstance(section, dict) else {}


def _parse(section: dict) -> Layout:
    return Layout(
        group_order=_strings(section.get("group_order")),
        account_order=[u for u in _strings(section.get("account_order")) if u],
        hidden={u for u in _strings(section.get("hidden")) if u},
    )


def current() -> Layout:
    """What the dashboard uses now; never raises."""
    return _parse(_section(get_settings()))


def segment_order(keys: Iterable[str], layout: Layout) -> list[str]:
    """The 来源 present in `keys`, first to last: the ones the layout names,
    then the rest in settings.yaml → groups order, then keys settings does
    not know in the order they appear, and 未分组 last unless placed."""
    present = list(dict.fromkeys(keys))
    have = set(present)
    order = [k for k in layout.group_order if k in have]
    placed = set(order)
    rest = [k for k in get_groups() if k in have and k not in placed]
    placed.update(rest)
    unknown = [k for k in present if k not in placed and k != UNGROUPED]
    tail = [UNGROUPED] if UNGROUPED in have and UNGROUPED not in placed else []
    return order + rest + unknown + tail


def arrange(items: list, layout: Layout,
            account: Callable[[Any], Account] = lambda x: x) -> list[Segment]:
    """`items` cut into one Segment per 来源, in layout order. Inside a
    segment the companies the layout names come first, in its order; the
    others keep the order they came in (accounts.enc order — a company
    added later lands at the end of its segment)."""
    rank = {u: i for i, u in enumerate(layout.account_order)}
    by_group: dict[str, list] = {}
    for item in items:
        by_group.setdefault(account(item).group or UNGROUPED, []).append(item)
    segments = []
    for key in segment_order(by_group, layout):
        members = by_group[key]
        ranked = sorted((m for m in members if account(m).username in rank),
                        key=lambda m: rank[account(m).username])
        ranked += [m for m in members if account(m).username not in rank]
        segments.append(Segment(key=key, label=group_label(key), items=ranked))
    return segments


def _as_saved(layout: Layout) -> dict:
    return {
        "group_order": list(layout.group_order),
        "account_order": list(layout.account_order),
        "hidden": sorted(layout.hidden),
    }


def save(group_order: list[str], account_order: list[str], shown: Iterable[str],
         accounts: list[Account]) -> dict:
    """Store a layout submitted from the editor; returns {key: [old, new]}
    for what changed (empty when nothing did).

    Only names that exist are kept. Hidden = listed on the form but not
    ticked, so a company added after the editor was opened stays visible.
    Companies of the existing layout that the form did not list (none, for
    an admin, unless one was added meanwhile) keep their hidden state."""
    logins = {a.username for a in accounts}
    groups = {a.group or UNGROUPED for a in accounts} | set(get_groups())
    listed = [u for u in _strings(account_order) if u in logins]
    ticked = set(_strings(list(shown)))
    with settings_svc.editing() as data:
        display = settings_svc.section(data, "display")
        old = _parse(_section(data))
        new = Layout(
            group_order=[g for g in _strings(group_order) if g in groups],
            account_order=listed,
            hidden={u for u in listed if u not in ticked}
                   | {u for u in old.hidden if u in logins and u not in listed},
        )
        before, after = _as_saved(old), _as_saved(new)
        diff = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
        if not diff:
            return {}
        if new.is_default:
            display.pop("dashboard", None)
        else:
            display["dashboard"] = after
        save_settings(data)
    return diff


def reset() -> bool:
    """Back to the default layout; False when it already was."""
    with settings_svc.editing() as data:
        display = data.get("display")
        if not isinstance(display, dict) or "dashboard" not in display:
            return False
        del display["dashboard"]
        save_settings(data)
    return True
