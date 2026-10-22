"""Groups — settings.yaml → groups: {key: 中文名} — managed from the 用户 page.

A group is the isolation boundary between colleagues: a non-admin sees the
companies of their groups and nothing else, and companies left 未分组 are
seen by admins only. With one group per colleague, colleagues cannot see
each other's companies or the owner's.

Saves go through settings_svc.editing() — a strict read-modify-write of the
whole file — so a broken settings.yaml is refused rather than replaced.
"""

from __future__ import annotations

import re

from src.config_loader import ConfigError, load_accounts
from web.services import settings_svc, users
from web.services.settings_svc import save_settings

_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
MAX_LABEL = 32


class GroupError(ValueError):
    """A group change that is not allowed; str() is shown as-is."""


def _groups(data: dict) -> dict:
    groups = data.get("groups")
    if not isinstance(groups, dict):
        groups = data["groups"] = {}
    return groups


def _clean_label(label: str) -> str:
    label = (label or "").strip()
    if not label:
        raise GroupError("分组名称不能为空")
    if len(label) > MAX_LABEL:
        raise GroupError(f"分组名称最多 {MAX_LABEL} 个字")
    return label


def personal_key(username: str) -> str:
    """The key of a colleague's own group: u_<username> (dots and dashes → _)."""
    return "u_" + re.sub(r"[^a-z0-9_]", "_", users.normalize_username(username))


def create(label: str, key: str | None = None) -> str:
    """Add a group; returns its key. Without `key`, one is derived from the
    existing ones (g1, g2, …) — keys are internal, people see the label."""
    label = _clean_label(label)
    with settings_svc.editing() as data:
        groups = _groups(data)
        if label in {str(v) for v in groups.values()}:
            raise GroupError(f"已有名为「{label}」的分组")
        if key is None:
            n = 1
            while f"g{n}" in groups:
                n += 1
            key = f"g{n}"
        if not _KEY_RE.match(key):
            raise GroupError("分组标识只能是小写字母、数字和下划线")
        if key in groups:
            raise GroupError(f"分组标识 {key} 已存在")
        groups[key] = label
        save_settings(data)
    return key


def rename(key: str, label: str) -> str:
    """Returns the old label."""
    label = _clean_label(label)
    with settings_svc.editing() as data:
        groups = _groups(data)
        if key not in groups:
            raise GroupError("分组不存在")
        if label in {str(v) for k, v in groups.items() if k != key}:
            raise GroupError(f"已有名为「{label}」的分组")
        old = str(groups[key])
        groups[key] = label
        save_settings(data)
    return old


def usage(key: str) -> tuple[int, list[str]]:
    """(companies in the group, users who belong to it)."""
    try:
        companies = sum(1 for a in load_accounts() if a.group == key)
    except ConfigError:
        companies = 0
    members = sorted(n for n, r in users.load().items() if key in r["groups"])
    return companies, members


def delete(key: str) -> str:
    """Remove an empty group; returns its label. A group still holding
    companies or users is refused — deleting it would silently move those
    companies to 未分组 and leave those users seeing nothing."""
    companies, members = usage(key)
    if companies or members:
        raise GroupError(f"分组里还有 {companies} 家企业、{len(members)} 位同事，先移走再删除")
    with settings_svc.editing() as data:
        groups = _groups(data)
        if key not in groups:
            raise GroupError("分组不存在")
        label = str(groups.pop(key))
        save_settings(data)
    return label
