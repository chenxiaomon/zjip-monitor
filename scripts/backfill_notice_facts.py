#!/usr/bin/env python3
"""Fill in .sync.json → facts for notices the scan stored before facts existed.

    python scripts/backfill_notice_facts.py [--dry-run]
        parse every notice PDF under data/attachments that has no facts entry yet
    ... | python scripts/backfill_notice_facts.py --from-stdin
        additionally merge facts the laptop parsed for notices this server no
        longer holds (released to the archive):
        {"<企业目录>/<登记号>": {"<文件名>": {kind, deadline, ...}}, ...}
        (sent by `python -m archive push-facts`)

Existing entries are never overwritten; stdin entries only fill gaps. Holds
the global run lock so no scan rewrites a manifest underneath it. Output: a
JSON line with counts.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from loguru import logger  # noqa: E402

from src import control, pdftext  # noqa: E402
from src.attachments import _ATTACHMENTS_DIR, _MANIFEST_NAME, _load_manifest  # noqa: E402
from src.fsutil import atomic_write_text  # noqa: E402
from src.logging_setup import setup_logging  # noqa: E402


def _is_document(f: Path) -> bool:
    return f.is_file() and not f.name.startswith(".") and not f.name.endswith(".part")


def _valid_fact(value) -> bool:
    return isinstance(value, dict) and value.get("kind") in pdftext.NOTICE_KINDS


def backfill(incoming: dict | None = None, *, dry_run: bool = False) -> dict:
    incoming = incoming if isinstance(incoming, dict) else {}
    counts = {"records": 0, "parsed": 0, "merged": 0, "unreadable": 0, "dry_run": dry_run}
    if not _ATTACHMENTS_DIR.is_dir():
        return counts
    for rec_dir in sorted(p for p in _ATTACHMENTS_DIR.glob("*/*") if p.is_dir()):
        manifest = _load_manifest(rec_dir) or {}
        facts = dict(manifest.get("facts") or {})
        before = dict(facts)
        for f in sorted(rec_dir.iterdir()):
            if not _is_document(f) or f.name in facts:
                continue
            if pdftext.kind_of(f.stem) not in pdftext.NOTICE_KINDS or f.suffix.lower() != ".pdf":
                continue
            record = pdftext.notice_facts(f)
            if record is None:
                counts["unreadable"] += 1
                continue
            facts[f.name] = record
            counts["parsed"] += 1
        key = f"{rec_dir.parent.name}/{rec_dir.name}"
        sent = incoming.get(key)
        if isinstance(sent, dict):
            for name, value in sent.items():
                if isinstance(name, str) and name not in facts and _valid_fact(value):
                    facts[name] = pdftext.as_record(pdftext.Facts(**{
                        k: value.get(k) for k in ("kind", "submitted_on", "issued_on", "deadline",
                                                  "valid_from", "valid_to", "reasons")
                    }))
                    counts["merged"] += 1
        if facts != before:
            counts["records"] += 1
            if not dry_run:
                manifest["facts"] = facts
                atomic_write_text(rec_dir / _MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
    return counts


def main(argv: list[str] | None = None) -> int:
    setup_logging("cli")
    parser = argparse.ArgumentParser(description="为已下载的通知书补写 .sync.json 的 facts（截止日期等）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    parser.add_argument("--from-stdin", action="store_true", help="同时合并本机归档推送来的 facts（JSON）")
    args = parser.parse_args(argv)

    incoming = None
    if args.from_stdin:
        incoming = json.load(sys.stdin)
        if not isinstance(incoming, dict):
            print(json.dumps({"error": "stdin 应为 JSON 对象"}, ensure_ascii=False))
            return 1

    ctx = control.try_acquire(kind="backfill_facts", trigger="cli")
    if ctx is None:
        print(json.dumps({"error": "另一巡检进行中，稍后再试"}, ensure_ascii=False))
        return 2
    status, error = "done", None
    try:
        counts = backfill(incoming, dry_run=args.dry_run)
        logger.info(f"facts 回填：{counts}")
        print(json.dumps(counts, ensure_ascii=False))
        return 0
    except Exception as exc:  # noqa: BLE001
        status, error = "error", f"{type(exc).__name__}: {exc}"
        print(json.dumps({"error": error}, ensure_ascii=False))
        return 1
    finally:
        ctx.finish(status, error=error)


if __name__ == "__main__":
    sys.exit(main())
