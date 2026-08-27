"""Shared pytest fixtures, in-memory fakes, and offline third-party stubs.

The whole test suite runs fully offline: `requests`, `markdown`, `resend`,
`feedparser`, `dotenv`, and `bs4` are stubbed in `sys.modules` *before* any
application module is imported, so no real network, no real Copilot CLI
subprocess, and no real Resend send are ever touched. This mirrors the repo's
existing `verify_*.py` scripts, which use the same in-memory stub pattern.

Application modules are imported lazily (inside fixtures) so the stubs are
guaranteed to be installed first. Adding this conftest at the repo root also
puts the repo root on `sys.path` so `import main`, `import emails`, etc.
resolve from the tests/ modules.
"""

from __future__ import annotations

import os
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repo root on sys.path so application modules import from the repo root.
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

HOST = "https://" + "example.com/"      # IANA-reserved test host; never fetched


# ---------------------------------------------------------------------------
# Offline stubs for third-party runtime deps (installed before app imports).
# ---------------------------------------------------------------------------
def _install_offline_stubs() -> None:
    """Stub third-party deps so app modules import and run with no network."""

    # requests: prefetch/curator import it; we only ever exercise code paths
    # that return before a real HTTP call, or branch through a monkeypatched
    # fake session (see tests/test_prefetch_gating.py).
    fake_req = types.ModuleType("requests")
    fake_req.Session = None
    fake_req.exceptions = types.SimpleNamespace(
        TooManyRedirects=Exception,
        Timeout=Exception,
        RequestException=Exception,
    )
    # Top-level aliases the real `requests` package exposes; the new bespoke
    # fetchers reference them (requests.get / requests.RequestException), so
    # the offline stub must be faithful. Real outbound calls are monkeypatched
    # by the tests that exercise a fetch.
    fake_req.RequestException = fake_req.exceptions.RequestException
    sys.modules.setdefault("requests", fake_req)

    # markdown: map markdown -> a distinguishable HTML wrapper.
    md = types.ModuleType("markdown")
    md.markdown = lambda text, **kw: "<html>%s</html>" % text
    sys.modules.setdefault("markdown", md)

    # resend: capture nothing by default; the seam injects a fake emailer, so
    # the real send path is never reached in tests.
    rs = types.ModuleType("resend")
    rs.api_key = None
    rs.Emails = types.SimpleNamespace(send=lambda payload: {"id": "msg_stub"})
    sys.modules.setdefault("resend", rs)

    # feedparser / dotenv: not exercised by the seam tests.
    fp = types.ModuleType("feedparser")
    fp.parse = lambda *a, **k: {"bozo": False, "entries": []}
    fp.FeedParserDict = dict
    sys.modules.setdefault("feedparser", fp)

    de = types.ModuleType("dotenv")
    de.load_dotenv = lambda *a, **k: None
    sys.modules.setdefault("dotenv", de)

    # bs4: extract used by prefetch's full-article path; the seam tests never
    # serialize an enriched article, and the redirect test returns before it.
    bs = types.ModuleType("bs4")
    bs.BeautifulSoup = lambda *a, **k: types.SimpleNamespace(
        get_text=lambda *a, **k: ""
    )
    sys.modules.setdefault("bs4", bs)


_install_offline_stubs()

# ---------------------------------------------------------------------------
# In-memory state operator that replaces StateStore (no data/ file writes).
# ---------------------------------------------------------------------------
@dataclass
class FakeState:
    """In-memory StateStore capturing state deltas; no data/ writes."""

    category_id: str
    seen: dict = field(default_factory=dict)
    health: dict = field(default_factory=lambda: {"sources": {}})
    saved_seen: list = field(default_factory=list)
    saved_health: list = field(default_factory=list)
    log_rows: list = field(default_factory=list)

    def load_seen(self) -> dict:
        return dict(self.seen)

    def save_seen(self, seen: dict) -> None:
        self.seen = dict(seen)
        self.saved_seen.append(dict(seen))

    def load_health(self) -> dict:
        return {
            "sources": {
                n: dict(r) for n, r in self.health.get("sources", {}).items()
            }
        }

    def save_health(self, health: dict) -> None:
        self.health = health
        self.saved_health.append(health)

    def record_source(self, health, name, success, error) -> None:
        health.setdefault("sources", {}).setdefault(name, {"recent_runs": []})
        health["sources"][name]["recent_runs"].append(
            {"success": success, "error": error}
        )

    def log_run(self, row: dict) -> None:
        self.log_rows.append(row)


# ---------------------------------------------------------------------------
# Fakes injected into run_category (fetcher registry, curator, emailer).
# ---------------------------------------------------------------------------
def make_source(name="S1", kind="rss", url=None, homepage=None):
    from categories import Source
    return Source(
        name=name,
        tier=1,
        kind=kind,
        url=url or (HOST + "feed"),
        homepage=homepage or (HOST[:-1]),
    )


def make_category(cat_id="ai-ml", name="AI/ML", recipient=None):
    """Build a Category. prompt_path is only read by the real curator, never
    by the fake curator the seam tests inject, so any Path is fine."""
    from categories import Category
    return Category(
        id=cat_id,
        name=name,
        schedule="17 16 * * *",
        recipient=recipient,
        prompt="prompts/%s.md" % cat_id,
        prompt_path=Path(REPO_ROOT) / "categories" / "prompts" / ("%s.md" % cat_id),
        sources=(make_source(name="S1"), make_source(name="S2")),
    )


def make_item(url=None, snippet="", linked_url=None):
    from fetchers.common import Item
    return Item(
        title="T",
        source_name="S1",
        url=url or (HOST + "a"),
        published="2099-01-01T00:00:00+00:00",
        content_snippet=snippet,
        linked_url=linked_url,
    )


def make_fetch_results(items_by_source):
    """Return a fake fetcher-registry dispatching per source name."""
    from fetchers.common import FetchResult

    def registry(source):
        items = items_by_source.get(source.name, [])
        return FetchResult(source.name, success=True, items=list(items))

    return registry


class FakeCurator:
    """Returns a fixed CurateResult without invoking Copilot/pre-fetch."""

    def __init__(self, digest="## Digest\n\nitem A", fail=None):
        self._digest = digest
        self._fail = fail
        self.calls = []

    def __call__(self, items, category, **kw):
        from curator import CurateResult
        self.calls.append((list(items), category))
        if self._fail is not None:
            raise self._fail
        return CurateResult(
            digest_markdown=self._digest,
            items_input=len(items),
            items_output=1,
            prompt_size=len(self._digest),
        )


# ---------------------------------------------------------------------------
# Fixtures (idiomatic pytest: fixtures are auto-available to test modules).
# ---------------------------------------------------------------------------
@pytest.fixture
def source_factory():
    return make_source


@pytest.fixture
def category_factory():
    return make_category


@pytest.fixture
def item_factory():
    return make_item


@pytest.fixture
def fake_registry_factory():
    return make_fetch_results


@pytest.fixture
def fake_curator_factory():
    return FakeCurator


@pytest.fixture
def state_factory():
    return FakeState


@pytest.fixture
def emailer_factory():
    """Return a ((captured_payloads) -> emailer) builder.

    The emailer captured sends as (markdown, subject, recipient) tuples and
    returns a fake Resend message id, so the seam's Resend send is injected
    and never asserted on its internals.
    """

    def _build():
        sent = []

        def emailer(markdown, subject, recipient):
            sent.append((markdown, subject, recipient))
            return "msg_%d" % len(sent)

        return emailer, sent

    return _build
