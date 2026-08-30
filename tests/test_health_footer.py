"""Regression test: the source-health footer must not break the formatting
of the digest element before it.

Observed in a rendered digest email: the footer was appended to a stripped
body as "<last line>\\n---", and in markdown a paragraph followed by a line
of dashes is a setext H2 heading — the digest's final entry got swallowed
into a heading the moment source failures appeared. The footer must carry
its own blank lines ("\\n\\n---\\n\\n") so the separator renders as a
thematic break regardless of how the body ends.
"""

from __future__ import annotations

import markdown as md_lib

from fetchers.common import FetchResult
from main import build_health_footer


def _fail(name: str) -> FetchResult:
    return FetchResult(name, False, error="HTTP 502")


def test_footer_starts_with_blank_line_then_rule():
    footer = build_health_footer([_fail("s")])
    assert footer.startswith("\n\n---\n\n")
    assert "*Source health:*" in footer
    assert "- s: HTTP 502" in footer


def test_no_failures_no_footer():
    assert build_health_footer([FetchResult("s", True)]) == ""


def test_rendered_html_keeps_last_element_and_rule_separate():
    body = "### [Some item](https://e.test/x)\n\nSummary text."
    html = md_lib.markdown(body + build_health_footer([_fail("s")]), extensions=["extra"])
    # The summary stays a paragraph, the rule renders as <hr>, never a setext
    # heading around the summary.
    assert "<p>Summary text.</p>" in html
    assert "<hr" in html
    assert "<h2>Summary text.</h2>" not in html
