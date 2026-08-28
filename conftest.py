"""Shared pytest fixtures, in-memory fakes, and offline third-party stubs.

The whole test suite runs fully offline: `requests`, `markdown`, `resend`,
`feedparser`, `dotenv`, and `bs4` are stubbed in `sys.modules` *before* any
application module is imported, so no real network, no real OpenRouter HTTP
call, and no real Resend send are ever touched. This mirrors the repo's
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
def _build_mini_bs4() -> types.ModuleType:
    """A minimal but *real* BeautifulSoup stand-in built on stdlib
    ``html.parser``.

    The AI Release Tracker fetcher must genuinely parse a captured HTML string
    offline (feed an HTML string, get mapped Items), so the bs4 stub can no
    longer be a no-op that returns empty text. This builds a tiny DOM supporting
    the CSS-subset selectors the bespoke HTML fetcher uses (``tag``,
    ``.class``, ``[attr^="prefix"]``, and simple compounds like
    ``span.text-white.truncate``), ``select``/``select_one`` scoped to
    descendants, ``get_text``, and ``.get("href")``. It only needs to be
    faithful enough for the offline suite; real deployments use real bs4 from
    requirements.txt.
    """
    import html.parser as _hp
    import re

    class _Node:
        __slots__ = ("name", "attrs", "children",)

        def __init__(self, name=None, attrs=None):
            self.name = name          # tag name, or None for a text node
            self.attrs = attrs if attrs is not None else {}
            self.children = []

        def __repr__(self):
            return "<Node %s children=%d>" % (self.name, len(self.children))

        # -- text extraction ----------------------------------------------
        def get_text(self, separator="", strip=False):
            texts = []
            stack = list(self.children)
            while stack:
                node = stack.pop()
                if node.name is None:
                    texts.append(node.attrs["_text"])
                else:
                    stack.extend(reversed(node.children))
            text = separator.join(texts) if separator else "".join(texts)
            if strip:
                text = re.sub(r"\s+", " ", text).strip()
            return text

        # -- attributes -----------------------------------------------------
        def get(self, key, default=None):
            return self.attrs.get(key, default)

        # -- element finders (descendants only, matching real bs4) ----------
        def select(self, selector):
            parsed = _parse_selector(selector)
            found = []
            stack = list(self.children)
            while stack:
                node = stack.pop()
                if node.name is not None and _matches(node, parsed):
                    found.append(node)
                stack.extend(reversed(node.children))
            return found

        def select_one(self, selector):
            for el in self.select(selector):
                return el
            return None

        def __getitem__(self, names):
            # ``soup(["script", "style"])`` from prefetch's full-article path.
            wanted = set(names)
            found = []
            stack = list(self.children)
            while stack:
                node = stack.pop()
                if node.name in wanted:
                    found.append(node)
                stack.extend(reversed(node.children))
            return found

        def decompose(self):
            pass  # removing nodes is not needed by the offline-suite paths

        def __str__(self):
            return self.get_text()

    # --- minimal CSS-subset selector support -------------------------------
    # Supports the compound selectors the bespoke HTML fetcher uses: a tag,
    # one or more ``.class`` parts, and a single ``[attr^="prefix"]`` /
    # ``[attr="val"]`` / ``[attr]`` attribute clause (e.g.
    # ``a[href^="/model/"]`` or ``span.text-white.truncate``). A manual
    # scanner avoids embedding quote characters in a raw regex.
    def _parse_selector(selector):
        parsed = {"tag": None, "classes": [], "attrs": []}
        i, n = 0, len(selector)
        while i < n:
            c = selector[i]
            if c == ".":
                j = i + 1
                while j < n and re.match(r"[a-zA-Z0-9_-]", selector[j]):
                    j += 1
                parsed["classes"].append(selector[i + 1:j])
                i = j
            elif c == "[":
                j = selector.find("]", i)
                if j == -1:
                    j = n
                body = selector[i + 1:j].strip()
                for op in ("^=", "="):
                    if op in body:
                        key, _, val = body.partition(op)
                        parsed["attrs"].append((key.strip(), val.strip().strip('"').strip("'"), op))
                        break
                else:
                    parsed["attrs"].append((body, None, None))
                i = j + 1
            elif re.match(r"[a-zA-Z]", c):
                j = i + 1
                while j < n and re.match(r"[a-zA-Z0-9-]", selector[j]):
                    j += 1
                parsed["tag"] = selector[i:j]
                i = j
            else:
                i += 1
        return parsed

    def _matches(node, parsed):
        if node.name is None:
            return False
        if parsed["tag"] and node.name != parsed["tag"]:
            return False
        classes = node.attrs.get("class") or []
        for cls in parsed["classes"]:
            if cls not in classes:
                return False
        for key, val, op in parsed["attrs"]:
            if key not in node.attrs:
                return False
            actual = str(node.attrs[key])
            if op == "^=" and not actual.startswith(val):
                return False
            if op == "=" and actual != val:
                return False
        return True

    class _Parser(_hp.HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.root = _Node("__root__")
            self.stack = [self.root]

        def handle_starttag(self, tag, attrs):
            node = _Node(tag.lower(), dict(attrs))
            if "class" in node.attrs:
                node.attrs["class"] = node.attrs["class"].split()
            self.stack[-1].children.append(node)
            self.stack.append(node)

        def handle_startendtag(self, tag, attrs):
            node = _Node(tag.lower(), dict(attrs))
            if "class" in node.attrs:
                node.attrs["class"] = node.attrs["class"].split()
            self.stack[-1].children.append(node)

        def handle_endtag(self, tag):
            if len(self.stack) > 1:
                self.stack.pop()

        def handle_data(self, data):
            self.stack[-1].children.append(_Node(None, {"_text": data}))

    def BeautifulSoup(markup, *args, **kwargs):
        p = _Parser()
        p.feed(markup or "")
        p.close()
        return p.root

    bs = types.ModuleType("bs4")
    bs.BeautifulSoup = BeautifulSoup
    return bs


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
    # the offline stub must be faithful. Each HTTP verb a test monkeypatches
    # must be pre-declared here: monkeypatch.setattr resolves the attribute
    # before replacing it, so a missing verb fails at patch time. Declared as
    # None so an unpatched call fails loudly; tests that exercise a fetch
    # monkeypatch it.
    fake_req.RequestException = fake_req.exceptions.RequestException
    fake_req.get = None
    fake_req.post = None
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

    # bs4: a minimal but real parser (stdlib html.parser) so the offline suite
    # can genuinely parse the captured HTML the AI Release Tracker fetcher test
    # feeds it. Other paths (prefetch full-article) only need safe get_text.
    sys.modules.setdefault("bs4", _build_mini_bs4())

    # youtube-transcript-api: only the names prefetch.py imports — the
    # exception hierarchy (the four spec failure types under their base) and
    # the API class, left None so an unpatched construction fails loudly (the
    # transcript tests always monkeypatch prefetch.YouTubeTranscriptApi, so no
    # real fetch is ever attempted).
    yta = types.ModuleType("youtube_transcript_api")
    class CouldNotRetrieveTranscript(Exception):
        pass
    for _name in ("TranscriptsDisabled", "NoTranscriptFound",
                  "VideoUnplayable", "RequestBlocked"):
        setattr(yta, _name, type(_name, (CouldNotRetrieveTranscript,), {}))
    yta.CouldNotRetrieveTranscript = CouldNotRetrieveTranscript
    yta.YouTubeTranscriptApi = None
    sys.modules.setdefault("youtube_transcript_api", yta)


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
    from categories import Category, Section
    return Category(
        id=cat_id,
        name=name,
        schedule="17 16 * * *",
        recipient=recipient,
        prompt="prompts/%s.md" % cat_id,
        prompt_path=Path(REPO_ROOT) / "categories" / "prompts" / ("%s.md" % cat_id),
        sources=(make_source(name="S1"), make_source(name="S2")),
        sections=(Section("General news", "fallback test section"),),
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
    """Returns a fixed CurateResult without invoking OpenRouter/pre-fetch."""

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
