"""Ticket 03 — YouTube-transcript deep-read in the pre-fetch seam.

Seams:
  - ``prefetch.fetch_transcript_excerpt`` — the transcript fetch + deterministic
    reduction seam (bounded excerpt, surfaced caption origin, failure mapping).
  - ``prefetch.prefetch`` — the pre-fetch stage composition: video items take
    the transcript path (never an HTML deep-read), each transcript fetch counts
    against the shared ``TOOL_CALL_CAP`` budget, and a transcript failure
    isolates to that item's error without crashing the stage.

All tests are offline: ``prefetch.YouTubeTranscriptApi`` is monkeypatched with
a fake whose boundary mirrors youtube-transcript-api v1.2.4 (``list`` returns
transcripts with ``language_code``/``is_generated`` and a ``fetch`` returning
an iterable of snippets carrying ``.text``). The live transcript fetch on a
datacenter IP is a smoke-test concern, per the spec's Testing Decisions.
"""

from __future__ import annotations

import pytest

from conftest import make_item, make_source
import prefetch as pf
from config import TRANSCRIPT_MAX_CHARS


# ---------------------------------------------------------------------------
# Fake youtube-transcript-api boundary
# ---------------------------------------------------------------------------
class FakeSnippet:
    def __init__(self, text):
        self.text = text


class FakeFetched:
    """Stand-in for FetchedTranscript: iterable of snippets + is_generated."""

    def __init__(self, text, is_generated=False):
        self.snippets = [FakeSnippet(t) for t in text.split(" ")]
        self.is_generated = is_generated

    def __iter__(self):
        return iter(self.snippets)


class FakeTranscript:
    def __init__(self, fetched=None, error=None, language_code="en"):
        self.fetched = fetched
        self.error = error
        self.language_code = language_code
        self.is_generated = fetched.is_generated if fetched else False

    def fetch(self):
        if self.error is not None:
            raise self.error
        return self.fetched


class FakeTranscriptList:
    def __init__(self, transcripts):
        self._transcripts = transcripts

    def find_transcript(self, language_codes):
        for t in self._transcripts:
            if t.language_code in language_codes:
                return t
        raise pf.NoTranscriptFound("vid1", list(language_codes), self)

    def __iter__(self):
        return iter(self._transcripts)


class FakeApi:
    """video_id -> transcript list OR exception to raise from ``list``."""

    def __init__(self, by_video):
        self._by_video = by_video
        self.listed = []

    def list(self, video_id):
        self.listed.append(video_id)
        entry = self._by_video[video_id]
        if isinstance(entry, Exception):
            raise entry
        return FakeTranscriptList(entry)


def make_video_item(url="https://www.youtube.com/watch?v=vid1",
                    video_id="vid1", snippet="short"):
    from fetchers.common import Item
    return Item(
        title="T", source_name="S1", url=url,
        published="2099-01-01T00:00:00+00:00",
        content_snippet=snippet, video_id=video_id,
    )


@pytest.fixture
def patch_api(monkeypatch):
    """Patch prefetch's YouTubeTranscriptApi; returns the FakeApi instance."""
    def _patch(by_video):
        api = FakeApi(by_video)
        monkeypatch.setattr(pf, "YouTubeTranscriptApi", lambda: api)
        return api
    return _patch


# ---------------------------------------------------------------------------
# fetch_transcript_excerpt
# ---------------------------------------------------------------------------
LONG_TEXT = " ".join(f"word{i}" for i in range(4000))  # ~30k chars


class TestExcerptBoundedAndDeterministic:
    def test_long_transcript_bounded_to_cap(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched(LONG_TEXT))]})
        block, err = pf.fetch_transcript_excerpt("vid1")
        assert err is None
        assert len(block) <= TRANSCRIPT_MAX_CHARS

    def test_excerpt_is_evenly_spaced_deterministic(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched(LONG_TEXT))]})
        b1, _ = pf.fetch_transcript_excerpt("vid1")
        b2, _ = pf.fetch_transcript_excerpt("vid1")
        assert b1 == b2
        body = b1.split("\n", 1)[1]
        marks = [i for i in range(len(body)) if body.startswith("[…]", i)]
        assert marks, "expected [ … ] separators between excerpts"
        # evenly spaced: gaps between consecutive excerpt starts are equal
        gaps = {second - first for first, second in zip(marks, marks[1:])}
        assert len(gaps) == 1

    def test_short_transcript_passes_through_whole(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched("hello world"))]})
        block, err = pf.fetch_transcript_excerpt("vid1")
        assert err is None
        assert "hello world" in block

    def test_excerpts_cover_head_and_tail(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched(LONG_TEXT))]})
        block, _ = pf.fetch_transcript_excerpt("vid1")
        assert block.startswith("[Video transcript")
        body = block.split("\n", 1)[1]
        assert LONG_TEXT.split()[0] in body        # head present
        assert LONG_TEXT.split()[-1] in body       # tail present


class TestCaptionOriginSurfaced:
    def test_manual_captions_surfaced(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched("hello"))]})
        block, _ = pf.fetch_transcript_excerpt("vid1")
        assert "[Video transcript — manual captions]" in block

    def test_auto_generated_captions_surfaced(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched("hello",
                                           is_generated=True))]})
        block, _ = pf.fetch_transcript_excerpt("vid1")
        assert "[Video transcript — auto-generated captions]" in block


class TestFailureMapping:
    def test_transcripts_disabled_maps_to_error(self, patch_api):
        patch_api({"vid1": pf.TranscriptsDisabled("vid1")})
        block, err = pf.fetch_transcript_excerpt("vid1")
        assert block == ""
        assert "TranscriptsDisabled" in err
        assert err.startswith("Error:")

    def test_no_transcript_found_maps_to_error(self, patch_api):
        patch_api({"vid1": pf.NoTranscriptFound("vid1", ["en"], None)})
        _, err = pf.fetch_transcript_excerpt("vid1")
        assert "NoTranscriptFound" in err

    def test_video_unplayable_maps_to_error(self, patch_api):
        patch_api({"vid1": pf.VideoUnplayable("vid1", None, [])})
        _, err = pf.fetch_transcript_excerpt("vid1")
        assert "VideoUnplayable" in err

    def test_request_blocked_maps_to_error(self, patch_api):
        patch_api({"vid1": pf.RequestBlocked("vid1")})
        _, err = pf.fetch_transcript_excerpt("vid1")
        assert "RequestBlocked" in err

    def test_unexpected_exception_never_raises(self, patch_api):
        class Boom(Exception):
            pass
        patch_api({"vid1": Boom("network imploded")})
        block, err = pf.fetch_transcript_excerpt("vid1")
        assert block == ""
        assert err and err.startswith("Error:")

    def test_empty_transcript_is_an_error(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched(""))]})
        block, err = pf.fetch_transcript_excerpt("vid1")
        assert block == ""
        assert err and err.startswith("Error:")


# ---------------------------------------------------------------------------
# prefetch() composition
# ---------------------------------------------------------------------------
SOURCES = [make_source(name="yt", kind="youtube",
                       url="UCQoOmu6mKZkXTnwZcpD8Ciw", homepage=None),
           make_source(name="S1")]


class TestPrefetchTranscriptPath:
    def test_video_item_gets_transcript_enrichment(self, patch_api):
        patch_api({"vid1": [FakeTranscript(FakeFetched("hello world"))]})
        item = make_video_item()
        result = pf.prefetch([item], SOURCES)
        assert result.errors == {}
        assert result.enrichments[item.url].startswith("[Video transcript")
        assert "hello world" in result.enrichments[item.url]

    def test_video_item_never_takes_the_html_deep_read_path(self, patch_api,
                                                            monkeypatch):
        api = patch_api({"vid1": [FakeTranscript(FakeFetched("hello"))]})
        # If the watch URL reached fetch_full_article it would need a real
        # session; make any such call fail loudly instead.
        monkeypatch.setattr(pf, "fetch_full_article",
                            lambda *a, **k: (_ for _ in ()).throw(
                                AssertionError("html path used for a video")))
        # Empty allowlist: even with no hosts allowed the transcript runs.
        item = make_video_item()
        result = pf.prefetch([item], [])
        assert result.enrichments[item.url].startswith("[Video transcript")
        assert api.listed == ["vid1"]

    def test_transcript_fetch_counts_against_the_shared_cap(self, patch_api,
                                                            monkeypatch):
        patch_api({f"vid{i}": [FakeTranscript(FakeFetched("hello"))]
                   for i in range(4)})
        monkeypatch.setattr(pf, "TOOL_CALL_CAP", 2)
        items = [make_video_item(video_id=f"vid{i}",
                                 url=f"https://www.youtube.com/watch?v=vid{i}")
                 for i in range(4)]
        result = pf.prefetch(items, SOURCES)
        assert result.fetches_used == 2
        assert len(result.enrichments) == 2
        assert len(result.errors) == 2
        assert all("cap reached" in e for e in result.errors.values())

    def test_transcript_failure_leaves_item_judgable_and_run_continues(
            self, patch_api):
        patch_api({
            "vid1": pf.TranscriptsDisabled("vid1"),
            "vid2": [FakeTranscript(FakeFetched("substance"))],
        })
        items = [
            make_video_item(video_id="vid1",
                            url="https://www.youtube.com/watch?v=vid1"),
            make_video_item(video_id="vid2",
                            url="https://www.youtube.com/watch?v=vid2"),
        ]
        result = pf.prefetch(items, SOURCES)
        # Failure is per-item; the other video still enriches; nothing raised.
        assert "vid1" not in " ".join(result.enrichments)
        assert result.enrichments[
            items[1].url].startswith("[Video transcript")
        assert "TranscriptsDisabled" in result.errors[items[0].url]
        assert result.fetches_used == 2

    def test_shared_budget_with_articles(self, patch_api, monkeypatch):
        patch_api({"vid1": [FakeTranscript(FakeFetched("hello"))]})
        monkeypatch.setattr(pf, "TOOL_CALL_CAP", 2)
        # An allowlisted article URL takes the HTML path; patch it so no
        # network is touched and count its fetch.
        calls = []
        monkeypatch.setattr(pf, "fetch_full_article",
                            lambda url, al: calls.append(url) or "article text")
        items = [
            make_video_item(),
            make_item(url="https://example.com/a", snippet="thin"),
        ]
        result = pf.prefetch(items, SOURCES)
        assert calls == ["https://example.com/a"]
        assert result.fetches_used == 2
        assert result.enrichments[items[1].url] == "article text"
        assert result.enrichments[items[0].url].startswith("[Video transcript")

    def test_duplicate_video_ids_fetch_once(self, patch_api):
        api = patch_api({"vid1": [FakeTranscript(FakeFetched("hello"))]})
        item = make_video_item()
        result = pf.prefetch([item, make_video_item()], SOURCES)
        assert api.listed == ["vid1"]
        assert result.fetches_used == 1

    def test_transcript_failure_isolated_from_article_path(self, patch_api,
                                                           monkeypatch):
        patch_api({"vid1": pf.RequestBlocked("vid1")})
        monkeypatch.setattr(pf, "fetch_full_article",
                            lambda url, al: "article text")
        items = [
            make_video_item(),
            make_item(url="https://example.com/a", snippet="thin"),
        ]
        result = pf.prefetch(items, SOURCES)
        assert result.enrichments[items[1].url] == "article text"
        assert "RequestBlocked" in result.errors[items[0].url]

    def test_failed_transcript_item_stays_judgable_on_its_snippet(
            self, patch_api):
        # Criterion 6 end to end: a video whose transcript fails, with no
        # other enrichment, still reaches the curation prompt snippet-only —
        # judgable, never crashed, run stays green.
        patch_api({"vid1": pf.TranscriptsDisabled("vid1")})
        item = make_video_item(snippet="enough substance to judge")
        result = pf.prefetch([item], SOURCES)   # must not raise
        from curator import _enrichment_for, build_user_message
        assert _enrichment_for(item, result.enrichments) is None
        message = build_user_message([item], "2099-01-01", result.enrichments)
        assert "enough substance to judge" in message
        assert "Video transcript" not in message
