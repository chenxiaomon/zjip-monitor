"""python -m archive bundle <企业> — the 交付包 from this Mac's archive.

The same package the console's 交付包 button gives (web/services/handover.py,
same bytes from the same snapshot), but built from ~/zjip-archive: the
certificates an earlier release took off the server are still here, and so
are companies the server no longer scans.

The zip goes to ARCHIVE_DIR/交付包/ by default. Nothing there is ever
indexed, released or pulled over: index reads attachments/, .overwritten/,
snapshots/ and events/; pull writes attachments/, snapshots/, events/,
audit/ and reports/; release only acts on database rows.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from web.services import handover
from web.services.snapshot import _DATE_RE, _safe_name, latest_snapshot_file

from .config import ArchiveConfig

DEFAULT_DIRNAME = "交付包"
# What pull writes and index reads: a package must never land in there
_ARCHIVE_DIRS = ("attachments", ".overwritten", "snapshots", "events", "audit", "reports")
_STALE_PULL_HOURS = 24


def _latest_by_company(snapshots_dir: Path) -> dict[str, Path]:
    """{company as the snapshot names it: its newest snapshot file}."""
    prefixes = {m.group(1) for f in snapshots_dir.glob("*.json") if (m := _DATE_RE.match(f.stem))}
    out: dict[str, Path] = {}
    for prefix in sorted(prefixes):
        path = latest_snapshot_file(snapshots_dir, prefix)
        if path is None:
            continue
        try:
            company = json.loads(path.read_text(encoding="utf-8")).get("company") or prefix
        except (OSError, ValueError):
            company = prefix
        out[str(company)] = path
    return out


def resolve_company(snapshots_dir: Path, arg: str) -> tuple[str, Path]:
    """(company, newest snapshot) for a company name, its folder name, or a
    piece of the name that fits only one company. LookupError otherwise —
    its message lists what was found."""
    arg = arg.strip()
    known = _latest_by_company(snapshots_dir)
    if not arg or not known:
        raise LookupError("本机归档里没有快照，先运行 python -m archive pull" if not known else "请给出企业名称")
    for company, path in known.items():
        if arg == company or _safe_name(arg) == _safe_name(company):
            return company, path
    hits = [c for c in known if arg in c or _safe_name(arg) in _safe_name(c)]
    if len(hits) == 1:
        return hits[0], known[hits[0]]
    if hits:
        raise LookupError(f"「{arg}」对得上 {len(hits)} 家，请写全：" + "、".join(sorted(hits)))
    raise LookupError(f"本机归档里没有「{arg}」这家企业")


def output_dir(cfg: ArchiveConfig, out: Path | None) -> Path:
    """Where the zip goes; never inside the archive's own data."""
    target = (out.expanduser() if out else cfg.archive_dir / DEFAULT_DIRNAME).resolve()
    root = cfg.archive_dir.resolve()
    for name in _ARCHIVE_DIRS:
        if target == root / name or target.is_relative_to(root / name):
            raise ValueError(f"交付包不能放进归档目录 {name}/ 里，换一个 --out")
    return target


def last_pull(archive_dir: Path) -> datetime | None:
    """When the last pull that went through finished, from pull.log."""
    try:
        lines = (archive_dir / "pull.log").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        parts = line.split("\t")
        if len(parts) >= 4 and parts[3] == "ok":
            try:
                return datetime.fromisoformat(parts[0])
            except ValueError:
                continue
    return None


def run_bundle(cfg: ArchiveConfig, company: str, *, out: Path | None = None,
               now: datetime | None = None) -> int:
    try:
        name, snap_path = resolve_company(cfg.archive_dir / "snapshots", company)
        dest_dir = output_dir(cfg, out)
    except (LookupError, ValueError) as exc:
        print(f"✗ {exc.args[0]}")
        return 1
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    pkg = handover.plan(name, snap, cfg.archive_dir / "attachments")
    if not pkg.rows:
        print(f"✗ {name} 还没有登记成功的记录")
        return 1

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / pkg.zip_name
    part = dest_dir / f".{pkg.zip_name}.part"
    try:
        size = handover.write_zip(pkg, part)
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)

    national = sum(1 for c in pkg.present if c.label == handover.NATIONAL_CERTIFICATE)
    provincial = len(pkg.present) - national
    print(f"✓ {dest}（{size / 1048576:.1f}MB）")
    print(f"  登记成功/续展 {len(pkg.rows)} 条；国家局证书 {national} 张、浙江省证书 {provincial} 张"
          + (f"；尚有 {pkg.open_count} 条在审，未列入" if pkg.open_count else ""))
    for c in pkg.missing:
        row = next(r for r in pkg.rows if c in r.certs)
        print(f"  ! {row.reg_no} {c.cell}")
    if pkg.as_of:
        print(f"  数据截至 {pkg.as_of:%Y-%m-%d %H:%M}（北京时间，最近一次拉到本机的快照）")
    pulled = last_pull(cfg.archive_dir)
    now = now or datetime.now(timezone.utc)
    if pulled is None or (now - pulled).total_seconds() > _STALE_PULL_HOURS * 3600:
        when = pulled.astimezone().strftime("%Y-%m-%d %H:%M") if pulled else "从未"
        print(f"  ! 上次拉取：{when}，可能不是最新——先运行 python -m archive pull 再打包")
    return 0
