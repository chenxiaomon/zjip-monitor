"""Pagination stop-condition tests for scraper.fetch_all_records (fake HTTP)."""

import pytest

from src import scraper


def _page_payload(records, total):
    return {"code": 200, "data": {"elements": records, "totalSize": total}}


def _rec(i):
    return {"id": i, "dataRegNo": f"R{i}", "dataRegAuditStatus": 1}


class _FakeSession:
    """Serves pre-baked pages; records how many pages were requested."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(scraper.time, "sleep", lambda s: None)
    monkeypatch.setattr(scraper, "_build_session", lambda token, timeout=30: _FakeSession(None))


def _serve(monkeypatch, pages, total):
    def fake_fetch(session, base_url, page_num, page_size):
        session.calls += 1
        records = pages[page_num - 1] if page_num <= len(pages) else []
        return _page_payload(records, total)

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)


def test_overlapping_pages_do_not_stop_early(monkeypatch):
    """Server repeats records across pages; dedup must not truncate the fetch."""
    pages = [
        [_rec(1), _rec(2), _rec(3)],
        [_rec(3), _rec(4), _rec(5)],  # overlaps with page 1
        [_rec(5), _rec(6), _rec(7)],  # overlaps with page 2
        [_rec(7), _rec(8)],
    ]
    _serve(monkeypatch, pages, total=8)
    records = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in records) == [1, 2, 3, 4, 5, 6, 7, 8]


def test_total_size_zero_returns_empty(monkeypatch):
    """totalSize=0 with an empty first page is a legitimate empty account."""
    _serve(monkeypatch, [[]], total=0)
    assert scraper.fetch_all_records("tok") == []


def test_short_page_ends_pagination(monkeypatch):
    pages = [[_rec(1), _rec(2), _rec(3)], [_rec(4)]]
    _serve(monkeypatch, pages, total=99)  # server lies about total
    records = scraper.fetch_all_records("tok")
    assert [r["id"] for r in records] == [1, 2, 3, 4]


def test_parse_response_total_zero_not_falsy():
    total, records = scraper._parse_response(
        {"code": 200, "data": {"elements": [], "totalSize": 0, "total": 7}}, page_num=1
    )
    assert total == 0  # must not fall through to the legacy "total" key
    assert records == []
