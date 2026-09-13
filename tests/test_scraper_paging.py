"""Pagination stop-condition tests for scraper.fetch_all_records (fake HTTP).

The server's totalSize is advisory only — pagination must be driven by what
the pages themselves say (empty page / no new unique records), and a
mid-pagination error must return the pages fetched so far instead of
discarding the whole account. A SHORT page is deliberately not a stop
condition: a throttled HTTP 200 carrying fewer rows would otherwise truncate
the sweep while still reporting complete=True.
"""

import pytest

from src import scraper
from src.exceptions import ScraperError, TokenExpiredError


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
    result = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in result.records) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert result.complete is True


def test_total_size_zero_returns_empty(monkeypatch):
    """totalSize=0 with an empty first page is a legitimate empty account."""
    _serve(monkeypatch, [[]], total=0)
    result = scraper.fetch_all_records("tok")
    assert result.records == []
    assert result.complete is True
    assert result.reported_total == 0


def test_short_last_page_is_followed_by_an_empty_page(monkeypatch):
    """The short page itself no longer stops the crawl — the empty page after
    it does. That costs one extra request and is what makes a *mid*-crawl
    short page recoverable (see the next test)."""
    pages = [[_rec(1), _rec(2), _rec(3)], [_rec(4)]]
    _serve(monkeypatch, pages, total=99)  # server lies about total
    result = scraper.fetch_all_records("tok")
    assert [r["id"] for r in result.records] == [1, 2, 3, 4]
    assert result.complete is True
    assert result.reported_total == 99
    assert result.pages_fetched == 3  # 2 data pages + the empty probe


def test_short_middle_page_does_not_truncate(monkeypatch):
    """A degraded page 2 (HTTP 200, fewer rows) must not end pagination.

    Under the old `len(page) < page_len_max` heuristic this returned only 4
    records with complete=True, so main.py skipped the partial-merge guard,
    saved the truncated snapshot and diffed records 5-7 as 「消失」.
    """
    pages = [
        [_rec(1), _rec(2), _rec(3)],
        [_rec(4)],                       # throttled/degraded page, not the end
        [_rec(5), _rec(6), _rec(7)],
    ]
    _serve(monkeypatch, pages, total=7)
    result = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in result.records) == [1, 2, 3, 4, 5, 6, 7]
    assert result.complete is True


def test_underreported_total_does_not_truncate(monkeypatch):
    """totalSize=0 (or under-reported) while pages have data: the old loop
    condition `len(records) < total` never ran and silently kept only page 1."""
    pages = [
        [_rec(1), _rec(2), _rec(3)],
        [_rec(4), _rec(5), _rec(6)],
        [_rec(7)],
    ]
    _serve(monkeypatch, pages, total=0)
    result = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in result.records) == [1, 2, 3, 4, 5, 6, 7]
    assert result.complete is True
    assert result.reported_total == 0


def test_ragged_first_page_does_not_stop_early(monkeypatch):
    """A first page shorter than later pages must not end pagination at page 2."""
    pages = [
        [_rec(1), _rec(2)],                    # ragged first page
        [_rec(3), _rec(4), _rec(5)],
        [_rec(6), _rec(7), _rec(8)],
        [_rec(9)],
    ]
    _serve(monkeypatch, pages, total=9)
    result = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in result.records) == list(range(1, 10))
    assert result.complete is True


def test_server_repeating_last_page_stops(monkeypatch):
    """A server that returns the same full page forever must stop on
    'no new unique records', not loop to the safety cap."""
    page = [_rec(1), _rec(2), _rec(3)]

    def fake_fetch(session, base_url, page_num, page_size):
        session.calls += 1
        return _page_payload(page, 3)

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    result = scraper.fetch_all_records("tok")
    assert sorted(r["id"] for r in result.records) == [1, 2, 3]
    assert result.complete is True
    assert result.pages_fetched == 2  # page 1 + the first repeat


def test_error_on_later_page_keeps_partial(monkeypatch):
    """A non-retryable error on page 3 keeps pages 1-2 with complete=False."""
    def fake_fetch(session, base_url, page_num, page_size):
        if page_num == 3:
            raise ScraperError("HTTP 500 on page 3")
        return _page_payload([_rec(page_num * 10 + i) for i in range(3)], 9)

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    result = scraper.fetch_all_records("tok")
    assert len(result.records) == 6  # pages 1 and 2
    assert result.complete is False
    assert "page 3" in result.error


def test_error_on_first_page_still_raises(monkeypatch):
    def fake_fetch(session, base_url, page_num, page_size):
        raise ScraperError("HTTP 500 on page 1")

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    with pytest.raises(ScraperError):
        scraper.fetch_all_records("tok")


def test_token_expired_mid_pagination_propagates(monkeypatch):
    """TokenExpiredError must propagate so main.py can re-login and retry."""
    def fake_fetch(session, base_url, page_num, page_size):
        if page_num == 2:
            raise TokenExpiredError("401")
        return _page_payload([_rec(i) for i in range(3)], 9)

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    with pytest.raises(TokenExpiredError):
        scraper.fetch_all_records("tok")


def test_max_pages_cap_marks_incomplete(monkeypatch):
    """Hitting the safety cap keeps the records but flags the result."""
    def fake_fetch(session, base_url, page_num, page_size):
        return _page_payload([_rec(page_num * 100 + i) for i in range(3)], 10_000)

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    monkeypatch.setattr(
        scraper, "_load_settings",
        lambda: {"scraper": {"max_pages": 4}, "base_url": "https://x"},
    )
    result = scraper.fetch_all_records("tok")
    assert result.complete is False
    assert "max_pages" in result.error
    assert len(result.records) == 12  # 4 pages × 3


def test_parse_response_total_zero_not_falsy():
    total, records = scraper._parse_response(
        {"code": 200, "data": {"elements": [], "totalSize": 0, "total": 7}}, page_num=1
    )
    assert total == 0  # must not fall through to the legacy "total" key
    assert records == []


def test_missing_total_is_not_from_server(monkeypatch):
    """A response without any total field must mark reported_total as
    synthesized so it can never 'confirm' a shrink downstream."""
    def fake_fetch(session, base_url, page_num, page_size):
        records = [_rec(1), _rec(2)] if page_num == 1 else []
        return {"code": 200, "data": {"elements": records}}  # no totalSize

    monkeypatch.setattr(scraper, "_fetch_page", fake_fetch)
    result = scraper.fetch_all_records("tok")
    assert len(result.records) == 2
    assert result.total_from_server is False
    assert result.reported_total == 0


def test_server_total_marks_from_server(monkeypatch):
    _serve(monkeypatch, [[_rec(1)]], total=1)
    result = scraper.fetch_all_records("tok")
    assert result.total_from_server is True
    assert result.reported_total == 1
