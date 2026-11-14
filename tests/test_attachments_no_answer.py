"""A document query the platform did not answer must not mark the record synced.

A record is queried again only when its status or updateTime moves, so the
pass right after a change is its one chance at its documents. An HTTP or API
error on getNotification / confirmation used to read as "no documents": the
manifest was written anyway, and the notice or certificate stayed missing
until the record changed again — for a 登记成功 record, typically never.
An answer that lists nothing is still a real answer and is not asked again.
"""

import json

import pytest

from src import attachments
from src.exceptions import TokenExpiredError

UPDATED_MS = 1_760_000_000_000  # long before any test run: the record never "moves"
NOTICE = {"type": "登记公告通知书", "url": "https://oss.example/n/gonggao.pdf", "id": 11}
CERT = {"type": "国家局登记证书", "url": "https://oss.example/c/guojia.pdf"}
CORRECTION = {"type": "补正通知书", "url": "https://oss.example/n/buzheng.pdf", "id": 12}


class _DummySession:
    def close(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "_ATTACHMENTS_DIR", tmp_path)
    monkeypatch.setattr(attachments, "_build_session", lambda token: _DummySession())
    monkeypatch.setattr(attachments, "_load_settings", lambda: {"attachments": {"enabled": True}})
    monkeypatch.setattr(attachments.time, "sleep", lambda s: None)

    def fake_download(url, dest, **kw):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(url.encode())
        return True

    monkeypatch.setattr(attachments, "_download_file", fake_download)
    return tmp_path


def _platform(monkeypatch, notices, certificates):
    """Each fetcher answers from its list in turn (the last answer repeats);
    None stands for "the platform did not answer". Returns the call log."""
    calls: list[str] = []

    def answer(kind, answers):
        def fetch(session, base_url, reg_no):
            n = sum(1 for c in calls if c.startswith(kind))
            calls.append(f"{kind}:{reg_no}")
            a = answers[min(n, len(answers) - 1)]
            return None if a is None else list(a)
        return fetch

    monkeypatch.setattr(attachments, "_fetch_notifications", answer("notif", notices))
    monkeypatch.setattr(attachments, "_fetch_certificates", answer("cert", certificates))
    return calls


def _record(reg_no="R1", status=7):
    return {"dataRegNo": reg_no, "dataRegAuditStatus": status, "updateTime": UPDATED_MS}


def _manifest(root, reg_no="R1"):
    path = root / "甲公司" / reg_no / attachments._MANIFEST_NAME
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


# ---------------------------------------------------------------------------
# sync_attachments
# ---------------------------------------------------------------------------

def test_unanswered_certificate_query_is_asked_again_next_scan(env, monkeypatch):
    calls = _platform(monkeypatch, notices=[[NOTICE]], certificates=[None, [CERT]])

    attachments.sync_attachments("tok", "甲公司", [_record()])
    assert _manifest(env) is None, "no answer must not mark the record synced"

    attachments.sync_attachments("tok", "甲公司", [_record()])
    assert calls.count("cert:R1") == 2
    assert (env / "甲公司" / "R1" / "国家局登记证书.pdf").exists()
    manifest = _manifest(env)
    assert manifest["status"] == 7
    assert "国家局登记证书.pdf" in manifest["files"]


def test_unanswered_notice_query_is_asked_again_next_scan(env, monkeypatch):
    calls = _platform(monkeypatch, notices=[None, [CORRECTION]], certificates=[[]])

    attachments.sync_attachments("tok", "甲公司", [_record(status=2)])
    assert _manifest(env) is None

    attachments.sync_attachments("tok", "甲公司", [_record(status=2)])
    assert calls == ["notif:R1", "notif:R1"]  # 待补正: no certificate query at all
    assert (env / "甲公司" / "R1" / "补正通知书.pdf").exists()
    assert _manifest(env)["status"] == 2


def test_no_certificate_query_after_an_unanswered_notice_query(env, monkeypatch):
    calls = _platform(monkeypatch, notices=[None], certificates=[[CERT]])

    attachments.sync_attachments("tok", "甲公司", [_record()])

    assert calls == ["notif:R1"]
    assert not (env / "甲公司" / "R1").exists()


def test_an_answer_with_no_certificate_is_final_until_the_record_moves(env, monkeypatch):
    calls = _platform(monkeypatch, notices=[[NOTICE]], certificates=[[]])

    attachments.sync_attachments("tok", "甲公司", [_record()])
    attachments.sync_attachments("tok", "甲公司", [_record()])

    assert calls == ["notif:R1", "cert:R1"]  # second scan skipped the record
    assert _manifest(env)["status"] == 7


def test_one_unanswered_record_does_not_hold_up_the_others(env, monkeypatch):
    def notices(session, base_url, reg_no):
        return None if reg_no == "R1" else [NOTICE]

    monkeypatch.setattr(attachments, "_fetch_notifications", notices)
    monkeypatch.setattr(attachments, "_fetch_certificates", lambda *a: [CERT])

    attachments.sync_attachments("tok", "甲公司", [_record("R1"), _record("R2")])

    assert _manifest(env, "R1") is None
    assert _manifest(env, "R2")["status"] == 7


def test_unanswered_query_keeps_the_previous_manifest(env, monkeypatch):
    """A record that moved on (待发证 → 登记成功) keeps its old manifest, so
    the status still differs next scan and the record is asked again."""
    rec = env / "甲公司" / "R1"
    rec.mkdir(parents=True)
    before = {"status": 17, "synced_at": "2026-11-01T00:00:00+00:00", "files": []}
    (rec / attachments._MANIFEST_NAME).write_text(json.dumps(before), encoding="utf-8")
    _platform(monkeypatch, notices=[[NOTICE]], certificates=[None])

    attachments.sync_attachments("tok", "甲公司", [_record()])

    assert _manifest(env) == before


# ---------------------------------------------------------------------------
# _api_data / the fetchers: no answer vs. an empty answer
# ---------------------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self.ok = 200 <= status < 400
        self._body = body
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)

    def json(self):
        if self._body is None and self.text and not self.text.startswith(("{", "[")):
            raise ValueError("not JSON")
        return self._body


class _Session:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **kw):
        return self.resp

    def post(self, *a, **kw):
        return self.resp


@pytest.mark.parametrize("resp", [
    _Resp(502, text="<html>Bad Gateway</html>"),
    _Resp(200, text="<html>maintenance</html>"),
    _Resp(200, body=["not", "an", "envelope"]),
    _Resp(200, body={"code": 500, "message": "系统繁忙"}),
    _Resp(200, body={"code": 500, "message": None}),
])
def test_no_answer_is_none_for_both_fetchers(resp):
    assert attachments._fetch_notifications(_Session(resp), "https://x", "R1") is None
    assert attachments._fetch_certificates(_Session(resp), "https://x", "R1") is None


@pytest.mark.parametrize("data", [None, {}, []])
def test_an_empty_answer_is_an_empty_list(data):
    resp = _Resp(200, body={"code": 200, "data": data})
    assert attachments._fetch_notifications(_Session(resp), "https://x", "R1") == []
    assert attachments._fetch_certificates(_Session(resp), "https://x", "R1") == []


def test_certificates_without_a_file_yet_are_left_out():
    body = {"code": 200, "data": {
        "confirmationType": 2,
        "systemConfirmation": None,
        "nationalConfirmation": {"confirmationUrl": "https://oss.example/c/guojia.pdf"},
    }}
    got = attachments._fetch_certificates(_Session(_Resp(200, body=body)), "https://x", "R1")
    assert got == [{"type": "国家局登记证书", "url": "https://oss.example/c/guojia.pdf"}]


@pytest.mark.parametrize("resp", [_Resp(401, text="unauthorized"), _Resp(200, body={"code": 401})])
def test_expired_token_still_raises(resp):
    with pytest.raises(TokenExpiredError):
        attachments._fetch_certificates(_Session(resp), "https://x", "R1")
