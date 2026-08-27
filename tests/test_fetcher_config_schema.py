"""Tests for the shared fetcher-config schema (ticket 01 prefactor).

A shared fetcher-config schema is introduced so future feedless sites slot in
as config. Each bespoke feedless source kind's config takes the same shape —
a ``url`` plus ``item``/``title``/``link``/``date`` field paths or selectors —
and the category loader validates a source's ``fetcher_config`` block and
carries it on the ``Source``.

These tests assert that external, loader-level behavior: building a ``Source``
from a category config that declares a ``fetcher_config``, and loud rejection
of malformed configs. They never assert parsing mechanics — the schema
performs none, and parsing is deliberately left to each consuming kind.

The seam under test is the category loader (``load_category``): it is the
public interface through which a source's config-shape is validated and
carried, and it is where a misconfigured feedless source fails loudly rather
than surfacing at fetch time.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from categories import CategoryError, load_category
from fetchers.config_schema import FetcherConfig

HOST = "https://" + "example.com/"
PROMPT = "prompts/ai-ml.md"


def _write_category(tmp_path, source_extras):
    """Write a minimal one-source category config and return its path."""
    promp = tmp_path / "prompts"
    promp.mkdir()
    (promp / "ai-ml.md").write_text("# prompt")
    cfg = {
        "id": "ai-ml",
        "name": "AI",
        "schedule": "17 16 * * *",
        "recipient": None,
        "prompt": PROMPT,
        "sources": [
            {
                "name": "Bespoke",
                "tier": 3,
                "kind": "huggingface_papers",
                "url": HOST + "papers",
                **source_extras,
            }
        ],
    }
    path = tmp_path / "cat.json"
    path.write_text(json.dumps(cfg))
    return path


# --- AC 1 & 2: a shared schema exists and bespoke kinds consume it ---------
def test_bespoke_source_loads_with_shared_fetcher_config(tmp_path):
    """A source declaring a fetcher_config block loads into a Source carrying a
    FetcherConfig — url inherited from the source, item/title/link/date from
    the block (the shared url + item/title/link/date contract)."""
    cat = load_category(_write_category(
        tmp_path,
        {
            "fetcher_config": {
                "item": "item", "title": "title", "link": "link", "date": "date"
            }
        },
    ))
    src = cat.sources[0]
    fc = src.fetcher_config
    assert fc is not None
    assert isinstance(fc, FetcherConfig)
    # The shared contract: url plus item/title/link/date.
    assert fc.url == HOST + "papers"      # inherited from the source's url
    assert fc.item == "item"
    assert fc.title == "title"
    assert fc.link == "link"
    assert fc.date == "date"


# --- AC 3: schema pins the shape, existing sources unchanged ---------------
def test_source_without_fetcher_config_has_none(tmp_path):
    """A plain source (e.g. kind: rss) that declares no fetcher_config loads
    with fetcher_config None — no behavior change for existing sources."""
    cat = load_category(_write_category(tmp_path, {}))
    assert cat.sources[0].fetcher_config is None


@pytest.mark.parametrize(
    "config_block, expected_fields",
    [
        ("not-a-dict", ()),                              # not an object
        ({"item": "item"}, ("title", "link", "date")),  # missing fields
        ({"item": "item", "title": "", "link": "link", "date": "date"}, ("title",)),  # blank
    ],
)
def test_malformed_fetcher_config_rejected(tmp_path, config_block, expected_fields):
    """A fetcher_config that breaks the shared shape is a loud, path-qualified
    config error — the schema pins the shape at load time."""
    with pytest.raises(CategoryError) as ei:
        load_category(_write_category(tmp_path, {"fetcher_config": config_block}))
    msg = str(ei.value)
    assert "fetcher_config" in msg
    assert all(f in msg for f in expected_fields)


# --- AC 3 & 4: pure config-shape contract, no fetch framework --------------
def test_fetcher_config_is_a_pure_data_contract():
    """FetcherConfig pins only the config shape: exactly the url +
    item/title/link/date fields, frozen, and with no functional methods that
    impose a parsing or fetch mechanism. Parsing is left to each consuming
    kind (JSON field mapping vs HTML-selector scraping)."""
    assert set(FetcherConfig.__dataclass_fields__) == {
        "url", "item", "title", "link", "date"
    }
    assert FetcherConfig.__dataclass_params__.frozen is True
    assert dataclasses.is_dataclass(FetcherConfig)

    fc = FetcherConfig(url="u", item="i", title="t", link="l", date="d")
    callables = [n for n in dir(fc) if callable(getattr(fc, n)) and not n.startswith("__")]
    assert callables == []          # no behavior, just data

    with pytest.raises(Exception):  # frozen => immutable
        fc.title = "other"


def test_fetcher_config_frozen_instances_equal_by_value():
    """Two identical configs are equal and hashable — cheap, value-semantic
    data usable in sets/dicts by consuming kinds."""
    a = FetcherConfig(url="u", item="i", title="t", link="l", date="d")
    b = FetcherConfig(url="u", item="i", title="t", link="l", date="d")
    assert a == b
    assert hash(a) == hash(b)
