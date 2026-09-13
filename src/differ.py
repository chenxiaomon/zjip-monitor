"""Snapshot diff: compare current records against the previous snapshot.

Three change types:
  - added:   record present now, absent before
  - changed: same record, dataRegAuditStatus differs
  - removed: present before, absent now (anomaly — warns)

Identity is the registration number (dataRegNo) when the platform has assigned
one, and the platform row id otherwise. Matching falls back to the row id in
both directions, so a record that merely *gains* its number between two runs is
recognised as the same record instead of being reported as one 消失 plus one
新增. Records carrying neither key cannot be compared at all; they are counted
and warned about rather than dropped in silence.

Only dataRegAuditStatus is compared, and only after int normalization — the
platform has been observed to send a code as both 7 and "7", which a raw !=
would report as a status change on every record in the account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from loguru import logger

from .scraper import decode_status, int_or_none


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


def _key(value) -> str:
    return str(value).strip() if value is not None else ""


def _label(rec: dict) -> str:
    """Human/stable handle for a record: its number, else its platform row id."""
    no = _key(rec.get("dataRegNo"))
    if no:
        return no
    rid = _key(rec.get("id"))
    return f"id:{rid}" if rid else "(无编号)"


def _index(
    records: list[dict], company: str, side: str
) -> tuple[list[dict], dict[str, dict], dict[str, dict]]:
    """Return (comparable records, by dataRegNo, by row id) and warn about
    anything that cannot take part in the comparison."""
    ordered: list[dict] = []
    by_no: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    keyless = 0
    duplicate_no = 0

    for rec in records:
        no = _key(rec.get("dataRegNo"))
        rid = _key(rec.get("id"))
        if not no and not rid:
            keyless += 1
            continue
        if no:
            if no in by_no:
                duplicate_no += 1
            by_no[no] = rec  # last wins, as the previous dict comprehension did
        if rid:
            by_id[rid] = rec
        ordered.append(rec)

    if keyless:
        logger.warning(
            f"[{company}] {side}有 {keyless} 条记录既无 dataRegNo 也无 id，"
            "无法参与差分对比（不会被判为新增或消失）"
        )
    if duplicate_no:
        logger.warning(
            f"[{company}] {side}有 {duplicate_no} 条记录的 dataRegNo 重复，"
            "差分只保留最后一条"
        )
    return ordered, by_no, by_id


def _counterpart(
    rec: dict, by_no: dict[str, dict], by_id: dict[str, dict]
) -> dict | None:
    """Find the same record on the other side: by number first, then by row id
    (which survives a record gaining its number between two runs)."""
    no = _key(rec.get("dataRegNo"))
    if no and no in by_no:
        return by_no[no]
    rid = _key(rec.get("id"))
    if rid and rid in by_id:
        return by_id[rid]
    return None


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

    curr_list, curr_by_no, curr_by_id = _index(current_records, company, "本次抓取")
    prev_list, prev_by_no, prev_by_id = _index(previous_records, company, "上次快照")

    matched_prev: set[int] = set()

    for curr_rec in curr_list:
        prev_rec = _counterpart(curr_rec, prev_by_no, prev_by_id)
        if prev_rec is None:
            result.added.append(curr_rec)
            logger.info(
                f"[{company}] 🆕 New: {_label(curr_rec)} "
                f"{curr_rec.get('dataRegName', '')[:30]}"
            )
            continue
        matched_prev.add(id(prev_rec))
        old_code = int_or_none(prev_rec.get("dataRegAuditStatus"))
        new_code = int_or_none(curr_rec.get("dataRegAuditStatus"))
        if old_code != new_code:
            change = RecordChange(
                reg_no=_label(curr_rec) or _label(prev_rec),
                name=curr_rec.get("dataRegName", ""),
                old_status_code=old_code,
                new_status_code=new_code,
            )
            result.changed.append(change)
            logger.info(
                f"[{company}] 🔄 Changed: {change.reg_no} "
                f"{change.old_status} → {change.new_status}"
            )

    for prev_rec in prev_list:
        if id(prev_rec) in matched_prev:
            continue
        result.removed.append(prev_rec)
        logger.warning(
            f"[{company}] ⚠  Removed (anomaly): {_label(prev_rec)} "
            f"{prev_rec.get('dataRegName', '')[:30]}"
        )

    if result.has_changes:
        logger.info(f"[{company}] Diff: {result.summary()}")
    else:
        logger.info(f"[{company}] No changes since last snapshot")

    return result
