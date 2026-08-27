"""Ticket 01 — multi-section source schema expand (categories.py loader).

Seam: the category load/validate unit module (``categories.load_category`` /
``Category.from_dict``). All tests are deterministic and offline: configs are
written to tmp_path with a real sibling prompt file, no network is touched.

The schema expands: a source's singular ``section`` is joined by a
``sections`` list (required non-empty in the final form), validated against
the category's own declared section names. The old singular form must keep
loading during the transition (expand, not break).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from categories import CategoryError, load_category


def write_category(tmp_path: Path, config: dict) -> Path:
    """Write a category config (with a real sibling prompt file) and return
    its path so ``load_category`` can be exercised end to end."""
    prompt_ref = config.setdefault("prompt", "prompts/test.md")
    prompt_path = tmp_path / prompt_ref
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("# Scope\n\nTest scope.\n", encoding="utf-8")
    config_path = tmp_path / "test.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def make_config(source: dict) -> dict:
    return {
        "id": "test-cat",
        "name": "Test",
        "schedule": "17 16 * * *",
        "recipient": None,
        "prompt": "prompts/test.md",
        "sections": [
            {"name": "Alpha", "description": "Alpha scope.", "max_items": 5},
            {"name": "Beta", "description": "Beta scope."},
        ],
        "sources": [source],
    }


BASE_SOURCE = {
    "name": "feed",
    "tier": 2,
    "kind": "rss",
    "url": "https://example.com/rss",
}


class TestSingularSectionStillLoads:
    """The old singular ``section`` form keeps loading during the expand."""

    def test_singular_section_loads_identically(self, tmp_path):
        path = write_category(tmp_path, make_config({**BASE_SOURCE, "section": "Beta"}))
        category = load_category(path)
        source = category.sources[0]
        assert source.section == "Beta"
        # The singular form normalizes to a one-element sections list.
        assert source.sections == ("Beta",)


class TestSectionsListLoads:
    """A non-empty ``sections`` list loads cleanly."""

    def test_single_element_list_loads(self, tmp_path):
        path = write_category(tmp_path, make_config({**BASE_SOURCE, "sections": ["Alpha"]}))
        category = load_category(path)
        source = category.sources[0]
        assert source.sections == ("Alpha",)
        assert source.section == "Alpha"

    def test_multi_element_list_loads(self, tmp_path):
        path = write_category(
            tmp_path,
            make_config({**BASE_SOURCE, "sections": ["Alpha", "Beta"]}),
        )
        source = load_category(path).sources[0]
        assert source.sections == ("Alpha", "Beta")

    def test_list_entries_are_whitespace_stripped(self, tmp_path):
        path = write_category(
            tmp_path,
            make_config({**BASE_SOURCE, "sections": ["  Alpha ", "Beta"]}),
        )
        source = load_category(path).sources[0]
        assert source.sections == ("Alpha", "Beta")


class TestSectionsValidation:
    """Bad ``sections`` shapes are rejected with a path-qualified error that
    names the offending field."""

    def test_unknown_section_name_rejected(self, tmp_path):
        path = write_category(
            tmp_path,
            make_config({**BASE_SOURCE, "sections": ["Alpha", "Gamma"]}),
        )
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        msg = str(excinfo.value)
        assert "test.json" in msg            # path-qualified
        assert "sections" in msg             # names the offending field
        assert "Gamma" in msg                # names the offending value
        assert "Alpha" in msg and "Beta" in msg  # names the valid choices

    def test_empty_list_rejected(self, tmp_path):
        path = write_category(tmp_path, make_config({**BASE_SOURCE, "sections": []}))
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        assert "sections" in str(excinfo.value)

    def test_missing_section_and_sections_rejected(self, tmp_path):
        path = write_category(tmp_path, make_config(dict(BASE_SOURCE)))
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        msg = str(excinfo.value)
        assert "section" in msg  # names the field(s) that were expected

    def test_non_list_rejected(self, tmp_path):
        path = write_category(
            tmp_path, make_config({**BASE_SOURCE, "sections": "Alpha"})
        )
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        assert "sections" in str(excinfo.value)

    def test_non_string_entry_rejected(self, tmp_path):
        path = write_category(
            tmp_path, make_config({**BASE_SOURCE, "sections": ["Alpha", 3]})
        )
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        assert "sections" in str(excinfo.value)

    def test_blank_entry_rejected(self, tmp_path):
        path = write_category(
            tmp_path, make_config({**BASE_SOURCE, "sections": ["  "]})
        )
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        assert "sections" in str(excinfo.value)


class TestExistingConfigsStillLoad:
    """The real shipped ai-ml config (singular ``section``) still loads."""

    def test_ai_ml_config_loads(self):
        category = load_category(Path("categories/ai-ml.json"))
        assert category.id == "ai-ml"
        for source in category.sources:
            assert source.section
            assert source.sections == (source.section,)
        declared = {s.name for s in category.sections}
        for source in category.sources:
            assert set(source.sections) <= declared
