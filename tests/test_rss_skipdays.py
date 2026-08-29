"""Regression tests: a valid-but-empty RSS channel with declared skip days.

arXiv serves a well-formed, zero-entry channel on its documented skip days
(Sat/Sun, occasional holidays). Before the fix, the smoke tests reported that
as FAIL ("likely bot-blocked or empty response"); now the fetcher attaches a
`FetchResult.note` when the channel is well-formed AND declares <skipDays>,
and the smoke test downgrades that case to WARN. A zero-entry response
without the skipDays signal still fails — that remains the bot-block
signature these tests guard against.

Fully offline: `requests.get` is stubbed, no network.
"""

from __future__ import annotations

import feedparser
import pytest

import fetchers.rss as rss
from categories import Source
from fetchers.common import FetchResult
from scripts.smoke_fetch_category import check_fetch, smoke_category


def _rss_xml(body_entries: str, skip_days: bool) -> bytes:
    skip = "<skipDays><day>Saturday</day><day>Sunday</day></skipDays>" \
        if skip_days else ""
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<rss version="2.0"><channel>
  <title>Test feed</title><link>http://example.com/rss</link>
  <description>test</description>
  <lastBuildDate>Sat, 29 Aug 2026 04:00:00 +0000</lastBuildDate>
  {skip}
  {body_entries}
</channel></rss>""".encode()


ENTRY = """<item><title>A paper</title>
<link>https://arxiv.org/abs/2608.01234</link>
<description>An abstract.</description></item>"""


class _FakeResponse:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.encoding = "utf-8"          # real requests.Response always sets this


@pytest.fixture
def _stub_get(monkeypatch):
    """Stub rss.requests.get to serve a canned body, recording the URL."""
    calls: list[str] = []

    def install(body: bytes, status_code: int = 200):
        def fake_get(url, **kwargs):
            calls.append(url)
            return _FakeResponse(body, status_code)
        monkeypatch.setattr(rss.requests, "get", fake_get)

    install.calls = calls
    return install


def _source() -> Source:
    return Source(
        name="Test feed", tier=1, sections=["Research"], kind="rss",
        url="https://rss.example.com/rss/xx", homepage="https://arxiv.org",
    )


def test_empty_channel_with_skipdays_gets_note(_stub_get):
    _stub_get(_rss_xml("", skip_days=True))
    result = rss.fetch(_source())
    assert result.success
    assert result.items == []
    assert result.note is not None
    assert "skip days" in result.note
    assert "Saturday, Sunday" in result.note      # both declared days, not just the last
    assert "Sat, 29 Aug 2026" in result.note


def test_empty_channel_without_skipdays_gets_no_note(_stub_get):
    _stub_get(_rss_xml("", skip_days=False))
    result = rss.fetch(_source())
    assert result.success
    assert result.items == []
    assert result.note is None


def test_nonempty_channel_gets_no_note(_stub_get):
    _stub_get(_rss_xml(ENTRY, skip_days=True))
    result = rss.fetch(_source())
    assert result.success
    assert len(result.items) == 1
    assert result.note is None


def test_check_fetch_fail_and_note_paths(_stub_get):
    src = _source()
    empty = FetchResult(src.name, True, [])
    assert check_fetch(src, empty)          # no note -> problem
    noted = FetchResult(src.name, True, [], note="valid but empty")
    assert check_fetch(src, noted)          # note does not remove the problem;
                                            # the WARN decision is smoke_category's


def test_smoke_category_noted_empty_is_warn_not_fail(_stub_get, capsys):
    _stub_get(_rss_xml("", skip_days=True))
    ok, failures = smoke_category(
        _make_category(_source()), fetcher_registry=rss.fetch,
        transcript_fn=lambda vid: ("", "no transcript attempted"))
    out = capsys.readouterr().out
    assert ok and not failures
    assert "  WARN" in out and "  FAIL" not in out


def test_smoke_category_plain_empty_still_fails(_stub_get, capsys):
    _stub_get(_rss_xml("", skip_days=False))
    ok, failures = smoke_category(
        _make_category(_source()), fetcher_registry=rss.fetch,
        transcript_fn=lambda vid: ("", "no transcript attempted"))
    out = capsys.readouterr().out
    assert not ok and failures
    assert "  FAIL" in out


def _make_category(source: Source):
    from pathlib import Path

    from categories import Category
    return Category(
        id="test", name="Test", schedule="0 8 * * *", recipient=None,
        prompt="prompts/ai-ml.md", prompt_path=Path("prompts/ai-ml.md"),
        sources=(source,),
        sections=({"name": "Research", "description": "d", "max_items": 5},),
    )
