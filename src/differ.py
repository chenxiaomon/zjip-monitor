"""Snapshot diff: compare current records against the previous snapshot.

Three change types:
  - added:   registration number present now, absent before
  - changed: same number, dataRegAuditStatus differs
  - removed: present before, absent now (anomaly — warns)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .scraper import decode_status


@dataclass
class RecordChange:
    reg_no: str
    name: str
    old_status_code: Optional[int]
    new_status_code: Optional[int]

    @property
    def old_status(self) -> str:
        return decode_status(self.old_status_code)

    @property
    def new_status(self) -> str:
        return decode_status(self.new_status_code)


@dataclass
class DiffResult:
    company: str
    added: list[dict] = field(default_factory=list)
    changed: list[RecordChange] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.added or self.changed or self.removed)

    def summary(self) -> str:
        parts = []
        if self.changed:
            parts.append(f"状态变更 {len(self.changed)} 条")
        if self.added:
            parts.append(f"新增 {len(self.added)} 条")
        if self.removed:
            parts.append(f"消失 {len(self.removed)} 条")
        return "；".join(parts) if parts else "无变化"


def diff_snapshots(
    company: str,
    current_records: list[dict],
    previous_records: list[dict],
) -> DiffResult:
    """Compare current records against previous snapshot.

    Args:
        company:          Company name (for logging and result labeling).
        current_records:  Records fetched in this run.
        previous_records: Records from the last saved snapshot ([] on first run).

    Returns:
        DiffResult with added / changed / removed lists.
    """
    result = DiffResult(company=company)

    if not previous_records:
        logger.info(f"[{company}] No previous snapshot — first run, skipping diff")
        return result

    prev_by_no: dict[str, dict] = {
        r["dataRegNo"]: r for r in previous_records if r.get("dataRegNo")
    }
    curr_by_no: dict[str, dict] = {
        r["dataRegNo"]: r for r in current_records if r.get("dataRegNo")
    }

    # Added
    for no, rec in curr_by_no.items():
        if no not in prev_by_no:
            result.added.append(rec)
            logger.info(f"[{company}] 🆕 New: {no} {rec.get('dataRegName', '')[:30]}")

    # Changed / Removed
    for no, prev_rec in prev_by_no.items():
        if no not in curr_by_no:
            result.removed.append(prev_rec)
            logger.warning(
                f"[{company}] ⚠  Removed (anomaly): {no} "
                f"{prev_rec.get('dataRegName', '')[:30]}"
            )
        else:
            curr_rec = curr_by_no[no]
            old_code = prev_rec.get("dataRegAuditStatus")
            new_code = curr_rec.get("dataRegAuditStatus")
            if old_code != new_code:
                change = RecordChange(
                    reg_no=no,
                    name=curr_rec.get("dataRegName", ""),
                    old_status_code=old_code,
                    new_status_code=new_code,
                )
                result.changed.append(change)
                logger.info(
                    f"[{company}] 🔄 Changed: {no} "
                    f"{change.old_status} → {change.new_status}"
                )

    if result.has_changes:
        logger.info(f"[{company}] Diff: {result.summary()}")
    else:
        logger.info(f"[{company}] No changes since last snapshot")

    return result
