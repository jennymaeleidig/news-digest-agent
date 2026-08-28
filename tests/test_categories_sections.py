"""Ticket 11 — multi-section source schema contract (categories.py loader).

Seam: the category load/validate unit module (``categories.load_category`` /
``Category.from_dict``). All tests are deterministic and offline: configs are
written to tmp_path with a real sibling prompt file, no network is touched.

The schema contracts the expand from ticket 01: the ``sections`` list is the
only source-section form. A config that still declares the legacy singular
``section`` is rejected with a path-qualified error rather than silently
accepted, and every listed Section is validated against the category's own
declared section names.
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


class TestSingularSectionRejected:
    """The singular ``section`` form is no longer accepted: the ``sections``
    list is the only source-section contract."""

    def test_singular_section_rejected(self, tmp_path):
        path = write_category(tmp_path, make_config({**BASE_SOURCE, "section": "Beta"}))
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        msg = str(excinfo.value)
        assert "test.json" in msg   # path-qualified
        assert "'section'" in msg   # names the offending legacy field (quoted token)
        assert "'sections'" in msg  # names the required form

    def test_singular_section_rejected_even_alongside_sections(self, tmp_path):
        # Both forms at once is still the legacy form's presence: rejected.
        path = write_category(
            tmp_path,
            make_config({**BASE_SOURCE, "section": "Beta", "sections": ["Beta"]}),
        )
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        assert "'section'" in str(excinfo.value)

    def test_source_carries_no_singular_section_attribute(self, tmp_path):
        # The dataclass drops the legacy accessor entirely so no engine
        # reader can silently fall back to the first-listed Section.
        path = write_category(
            tmp_path, make_config({**BASE_SOURCE, "sections": ["Alpha"]})
        )
        source = load_category(path).sources[0]
        assert source.sections == ("Alpha",)
        assert not hasattr(source, "section")


class TestSectionsListLoads:
    """A non-empty ``sections`` list loads cleanly."""

    def test_single_element_list_loads(self, tmp_path):
        path = write_category(tmp_path, make_config({**BASE_SOURCE, "sections": ["Alpha"]}))
        source = load_category(path).sources[0]
        assert source.sections == ("Alpha",)

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

    def test_missing_sections_rejected(self, tmp_path):
        path = write_category(tmp_path, make_config(dict(BASE_SOURCE)))
        with pytest.raises(CategoryError) as excinfo:
            load_category(path)
        msg = str(excinfo.value)
        assert "sections" in msg  # names the required field

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


class TestShippedConfigsUseTheListForm:
    """Every real shipped category config (ai-ml migrated in ticket 10;
    tech / video-games / politics-news built on the list form) loads under
    the list-only contract."""

    @pytest.mark.parametrize(
        "config_path", sorted((Path("categories") / p for p in
                               ("ai-ml.json", "tech.json",
                                "video-games.json", "politics-news.json"))),
        ids=lambda p: p.stem,
    )
    def test_config_loads_with_list_only_sections(self, config_path):
        category = load_category(config_path)
        assert category.id
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        declared = {s.name for s in category.sections}
        for source, raw_source in zip(category.sources, raw["sources"]):
            assert source.sections
            assert "section" not in raw_source, source.name
            assert set(source.sections) <= declared
