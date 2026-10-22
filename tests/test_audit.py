"""src/audit.py — the operation log writer."""

import json
import os
import stat
from datetime import datetime, timezone

from src import audit


def _lines(d):
    out = []
    for f in sorted(d.glob("audit-*.jsonl")):
        out += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln]
    return out


def test_one_line_per_record_with_empty_fields_left_out(_isolated_audit):
    audit.record("alice", "account.edit", actor_name="张三", role="operator", via="cookie",
                 ip="203.0.113.9", company="甲公司", target="acct01",
                 detail={"password_changed": True})
    audit.record("bob", "auth.logout")

    first, second = _lines(_isolated_audit)
    assert first["actor"] == "alice" and first["actor_name"] == "张三"
    assert first["action"] == "account.edit" and first["company"] == "甲公司"
    assert first["ok"] is True and first["detail"] == {"password_changed": True}
    assert datetime.fromisoformat(first["ts"]).tzinfo is not None
    assert set(second) == {"ts", "actor", "action", "ok"}


def test_month_files_follow_the_beijing_calendar(_isolated_audit, monkeypatch):
    # 16:30 UTC on Jan 31 is already Feb 1 in Beijing.
    monkeypatch.setattr(audit, "_now", lambda: datetime(2026, 1, 31, 16, 30, tzinfo=timezone.utc))
    audit.record("a", "x")
    monkeypatch.setattr(audit, "_now", lambda: datetime(2026, 1, 31, 15, 59, tzinfo=timezone.utc))
    audit.record("a", "y")
    assert (_isolated_audit / "audit-2026-02.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (_isolated_audit / "audit-2026-01.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert [f.name for f in audit.files()] == ["audit-2026-01.jsonl", "audit-2026-02.jsonl"]


def test_files_are_private(_isolated_audit):
    audit.record("a", "x")
    (f,) = audit.files()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600


def test_a_failed_write_neither_raises_nor_prints(_isolated_audit, capsys):
    """The action already happened, and archive_release.py's stdout is a
    JSON protocol its caller parses — a failure may only reach the log."""
    _isolated_audit.parent.mkdir(parents=True, exist_ok=True)
    _isolated_audit.write_text("a file where the directory should be")
    audit.record("a", "x")  # must not raise
    assert _isolated_audit.is_file()
    assert capsys.readouterr().out == ""


def test_secrets_are_masked_as_a_backstop(_isolated_audit):
    audit.record("a", "notify.dingtalk", detail={
        "webhook": "https://oapi.dingtalk.com/robot/send?access_token=SENTINEL",
        "secret": "SENTINEL2",
        "smtp_password": "SENTINEL3",
        "nested": {"api_key": "SENTINEL4"},
        "webhook_changed": True,
        "changed": ["webhook", "secret"],
        "enabled": False,
    })
    raw = "".join(f.read_text(encoding="utf-8") for f in audit.files())
    assert "SENTINEL" not in raw
    (entry,) = _lines(_isolated_audit)
    assert entry["detail"]["webhook_changed"] is True
    assert entry["detail"]["changed"] == ["webhook", "secret"]
    assert entry["detail"]["enabled"] is False


def test_long_strings_and_lists_are_capped(_isolated_audit):
    audit.record("a" * 500, "x", detail={"note": "z" * 1000, "items": list(range(500))})
    (entry,) = _lines(_isolated_audit)
    assert len(entry["actor"]) == 200
    assert len(entry["detail"]["note"]) == 200
    assert len(entry["detail"]["items"]) == 50


def test_concurrent_appends_never_interleave(_isolated_audit):
    """Two writers (web + archive_release) share the file without a lock:
    each line is a single O_APPEND write."""
    import threading

    def burst(name):
        for i in range(200):
            audit.record(name, "x", detail={"i": i, "pad": "p" * 150})

    threads = [threading.Thread(target=burst, args=(n,)) for n in ("w1", "w2", "w3")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    entries = _lines(_isolated_audit)  # json.loads would fail on a torn line
    assert len(entries) == 600


def test_unknown_types_are_stringified(_isolated_audit):
    audit.record("a", "x", detail={"path": os.path.join("a", "b"), "when": datetime(2026, 1, 1)})
    (entry,) = _lines(_isolated_audit)
    assert entry["detail"]["when"].startswith("2026-01-01")
