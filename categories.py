"""Category schema and loader.

A category is a standalone JSON config plus a sibling prompt file. This
module loads and validates a single category definition from its JSON
config and the referenced prompt file. Discovery over the categories/
directory and the per-category run are wired later by the run seam; here
we only load one category by path, so that a future category is "drop one
JSON + one prompt file" against a locked schema.

Schema (see spec decision 5 — locked via prototype-ai-ml-category.json):

    id          str   stable state-namespace key (required, non-empty)
    name        str   display name, used for the email subject (required)
                  schedule    str   cron in UTC; the category's own staggered schedule —
                  the per-category workflow (.github/workflows/
                  digest-<id>.yml) must carry this same cron
                  (stagger: base 08:00 UTC, +30m each)
    recipient   str|null  recipient; null => default RECIPIENT_EMAIL
    prompt      str   file reference to a sibling prompts/<id>.md
                      (required, and the referenced file must exist)
    sections[]  list  non-empty and ordered; each section has:
        name          str    display name / digest heading (required, unique)
        description   str    what belongs in the section; rendered into the
                             curation prompt (optional, defaults to "")
        max_items     int|null per-section selection ceiling (optional): stage 1
                             is asked to pick at most this many items and its
                             result is hard-clipped to it. Omitted/null => the
                             global CURATION_SELECT_MAX_ITEMS applies.
    sources[]   list  non-empty; each source has:
        name        str    (required)
        tier        int    Kagi trust tier 1-4 (required, static)
        kind        str    "rss" at launch (required)
        url         str    feed URL (required)
        homepage    str    display link + static allowlist (optional)
        sections    [str]  list of digest section names this source feeds —
                           each must be one of the names in the category's
                           top-level ``sections`` (required, non-empty). The
                           legacy singular ``section`` form is no longer
                           accepted: a config that still declares it is
                           rejected.
        age_limit_days int|null  per-source recency-window override (optional).
                           When set, this source's items stay eligible for that
                           many days instead of the global ITEM_AGE_LIMIT_DAYS.
                           For a slow-moving canonical feed whose "latest"
                           list spans weeks, a 24-hour window shows almost
                           nothing — set a longer window (e.g. 30).
                           Omitted/null => the global window applies.
        user_agent   str|null  optional per-source User-Agent override for the
                           RSS fetcher. For hosts that bot-block the shared
                           browser-impersonation string (e.g. PBS returns
                           "202 Accepted" with an empty body for it) but serve
                           a plain client fine. Omitted/null => the shared
                           config.USER_AGENT applies.
        topics      [str]  optional relevance allow-list: an item from this
                           source is kept only if one of these terms appears in
                           its title or abstract/snippet (case-insensitive).
                           Omitted/empty -> all items kept. Used to scope a
                           broad feed (e.g. arXiv cs.AI+cs.LG) down to a topic
                           like "the LLM stuff" without a bespoke fetcher.
        fetcher_config object  optional, bespoke kinds only. The shared
                           fetcher-config schema: ``item``/``title``/``link``/
                           ``date`` field paths or selectors for the kind's
                           fetched representation. The ``url`` is inherited
                           from the source's top-level ``url`` (single source
                           of truth). The schema pins only the config shape;
                           each consuming kind does its own parsing. Only
                           present on feedless/bespoke kinds; omitted on
                           ``kind: rss`` sources.

The loader validates the shape and raises ValueError on any violation,
with a path-qualified message that names the offending field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from fetchers.config_schema import FetcherConfig


class CategoryError(ValueError):
    """Raised when a category config fails schema validation."""


@dataclass(frozen=True)
class Section:
    """One digest section: display name, optional description, and (optional)
    per-section selection ceiling.

    Sections are category-specific and defined in the category JSON, which is
    the single source of truth: the loader validates each source's ``section``
    against these names, the curator emits sections in this order, and the
    curation prompt's section definitions are rendered from these descriptions.
    ``max_items`` overrides the global CURATION_SELECT_MAX_ITEMS for the
    section's stage-1 selection; None means use the global default.
    """
    name: str
    description: str = ""
    max_items: int | None = None


@dataclass(frozen=True)
class Source:
    name: str
    tier: int          # Kagi trust tier 1-4 (static default)
    kind: str          # "rss" at launch
    url: str           # the feed URL
    homepage: str | None = None   # display link + static allowlist
    sections: tuple[str, ...] = ()  # digest sections this source feeds (see docstring)
    topics: tuple[str, ...] = ()  # relevance allow-list (see module docstring)
    fetcher_config: FetcherConfig | None = None  # bespoke kinds only (see docstring)
    age_limit_days: int | None = None  # per-source recency override (see docstring)
    user_agent: str | None = None  # per-source RSS User-Agent override (see docstring)


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    schedule: str
    recipient: str | None      # None => default RECIPIENT_EMAIL
    prompt: str                # file ref to a sibling prompts/<id>.md
    prompt_path: Path          # resolved path to the referenced prompt file
    sources: tuple[Source, ...]
    sections: tuple[Section, ...]

    @classmethod
    def from_dict(cls, data: dict, config_path: Path) -> "Category":
        """Validate a parsed config dict and build a Category.

        `config_path` is the path the JSON was loaded from; it anchors the
        resolution of the (relative) prompt file reference so the referenced
        sibling `prompts/<id>.md` is found relative to the category file's
        directory.
        """
        err = lambda msg: CategoryError(f"{config_path}: {msg}")

        # ---- top-level required fields ----------------------------------
        cat_id = data.get("id")
        if not isinstance(cat_id, str) or not cat_id.strip():
            raise err("'id' is required and must be a non-empty string")

        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            raise err(f"category {cat_id!r}: 'name' is required and must be a non-empty string")

        schedule = data.get("schedule")
        if not isinstance(schedule, str):
            # The category's own staggered cron; the per-category workflow
            # (.github/workflows/digest-<id>.yml) must carry the same value.
            # Allow
            # missing (backward-friendly) but reject a schedule that is
            # present but
            # not a string so a config typo is caught early.
            if schedule is not None:
                raise err(f"category {cat_id!r}: 'schedule' must be a string (the category's own cron, mirrored by its workflow)")
            schedule = ""

        recipient = data.get("recipient")
        if recipient is not None and not isinstance(recipient, str):
            raise err(f"category {cat_id!r}: 'recipient' must be a string or null")

        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise err(f"category {cat_id!r}: 'prompt' is required and must be a non-empty string file reference")

        # The prompt is a file reference to a sibling prompts/<id>.md,
        # resolved relative to the category file's directory.
        prompt_path = (config_path.parent / prompt).resolve()
        if not prompt_path.is_file():
            raise err(
                f"category {cat_id!r}: prompt file reference {prompt!r} "
                f"does not exist (resolved to {prompt_path})"
            )

        # ---- sections (single source of truth for digest sections) ------
        raw_sections = data.get("sections")
        if not isinstance(raw_sections, list) or not raw_sections:
            raise err(
                f"category {cat_id!r}: 'sections' is required and must be a non-empty list"
            )
        parsed_sections: list[Section] = []
        seen_sections: set[str] = set()
        for j, sec in enumerate(raw_sections):
            if not isinstance(sec, dict):
                raise err(f"category {cat_id!r}: sections[{j}] must be an object")
            sec_label = f"category {cat_id!r}: sections[{j}]"
            sec_name = sec.get("name")
            if not isinstance(sec_name, str) or not sec_name.strip():
                raise err(f"{sec_label}: 'name' is required and must be a non-empty string")
            sec_name = sec_name.strip()
            if sec_name in seen_sections:
                raise err(f"{sec_label}: duplicate section name {sec_name!r}")
            seen_sections.add(sec_name)
            sec_description = sec.get("description", "") or ""
            if not isinstance(sec_description, str):
                raise err(f"{sec_label}: 'description' must be a string")
            sec_max_items = None
            mi = sec.get("max_items")
            if mi is not None:
                if not isinstance(mi, int) or isinstance(mi, bool) or mi < 1:
                    raise err(
                        f"{sec_label}: 'max_items' must be a positive integer or null"
                    )
                sec_max_items = mi
            parsed_sections.append(
                Section(sec_name, sec_description.strip(), sec_max_items)
            )
        section_names = tuple(sec.name for sec in parsed_sections)

        # ---- sources ----------------------------------------------------
        sources = data.get("sources")
        if not isinstance(sources, list) or not sources:
            raise err(f"category {cat_id!r}: 'sources' must be a non-empty list")

        parsed_sources = []
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                raise err(f"category {cat_id!r}: sources[{i}] must be an object")
            label = f"category {cat_id!r}: sources[{i}]"

            s_name = src.get("name")
            if not isinstance(s_name, str) or not s_name.strip():
                raise err(f"{label}: 'name' is required and must be a non-empty string")

            tier = src.get("tier")
            if not isinstance(tier, int) or isinstance(tier, bool):
                raise err(f"{label} ({s_name!r}): 'tier' is required and must be an integer")
            if tier < 1 or tier > 4:
                raise err(f"{label} ({s_name!r}): 'tier' must be 1-4, got {tier}")

            kind = src.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise err(f"{label} ({s_name!r}): 'kind' is required and must be a non-empty string")

            url = src.get("url")
            if not isinstance(url, str) or not url.strip():
                raise err(f"{label} ({s_name!r}): 'url' is required and must be a non-empty string")

            homepage = src.get("homepage")
            if homepage is not None and not isinstance(homepage, str):
                raise err(f"{label} ({s_name!r}): 'homepage' must be a string or missing")

            topics = src.get("topics")
            if topics is None:
                topics = ()
            elif (
                not isinstance(topics, list)
                or not topics
                or not all(isinstance(t, str) and t.strip() for t in topics)
            ):
                raise err(
                    f"{label} ({s_name!r}): 'topics' must be a non-empty list "
                    f"of non-empty strings"
                )
            else:
                topics = tuple(t.strip() for t in topics)

            # A source feeds one or more declared Sections, given as the
            # ``sections`` list — the only accepted form. The legacy singular
            # ``section`` key is rejected outright so a stale config fails
            # loudly instead of silently curating into one Section.
            if "section" in src:
                raise err(
                    f"{label} ({s_name!r}): 'section' is no longer accepted; "
                    f"use the 'sections' list"
                )
            raw_sections = src.get("sections")
            if not isinstance(raw_sections, list) or not raw_sections:
                raise err(
                    f"{label} ({s_name!r}): 'sections' is required and must be "
                    f"a non-empty list naming one or more of: "
                    f"{', '.join(section_names)}"
                )
            for entry in raw_sections:
                if not isinstance(entry, str) or not entry.strip():
                    raise err(
                        f"{label} ({s_name!r}): 'sections' entries must "
                        f"be non-empty strings"
                    )
            sections = tuple(entry.strip() for entry in raw_sections)
            unknown = [s for s in sections if s not in section_names]
            if unknown:
                raise err(
                    f"{label} ({s_name!r}): 'sections' names undeclared "
                    f"section(s) {', '.join(unknown)!r}; must be one of: "
                    f"{', '.join(section_names)}"
                )

            age_limit_days = src.get("age_limit_days")
            if age_limit_days is not None:
                if not isinstance(age_limit_days, int) or isinstance(age_limit_days, bool):
                    raise err(
                        f"{label} ({s_name!r}): 'age_limit_days' must be an integer or null"
                    )
                if age_limit_days < 1:
                    raise err(f"{label} ({s_name!r}): 'age_limit_days' must be >= 1")

            user_agent = src.get("user_agent")
            if user_agent is not None and (not isinstance(user_agent, str) or not user_agent.strip()):
                raise err(f"{label} ({s_name!r}): 'user_agent' must be a non-empty string or null")

            # Optional shared fetcher-config (bespoke feedless kinds). Pins
            # the config-shape contract; parsing is left to each consuming
            # kind. ``url`` is inherited from the source's top-level url so
            # there is a single source of truth for the URL.
            fetcher_config: FetcherConfig | None = None
            fc = src.get("fetcher_config")
            if fc is not None:
                if not isinstance(fc, dict):
                    raise err(f"{label} ({s_name!r}): 'fetcher_config' must be an object")
                _CONFIG_FIELDS = ("item", "title", "link", "date")
                missing = [
                    f for f in _CONFIG_FIELDS
                    if not isinstance(fc.get(f), str) or not fc[f].strip()
                ]
                if missing:
                    raise err(
                        f"{label} ({s_name!r}): 'fetcher_config' requires non-empty "
                        f"string fields: {', '.join(missing)}"
                    )
                fetcher_config = FetcherConfig(
                    url=url,
                    item=fc["item"].strip(),
                    title=fc["title"].strip(),
                    link=fc["link"].strip(),
                    date=fc["date"].strip(),
                )

            parsed_sources.append(Source(
                name=s_name,
                tier=tier,
                kind=kind,
                url=url,
                homepage=homepage,
                sections=sections,
                topics=topics,
                fetcher_config=fetcher_config,
                age_limit_days=age_limit_days,
                user_agent=user_agent.strip() if user_agent else None,
            ))

        return cls(
            id=cat_id,
            name=name,
            schedule=schedule,
            recipient=recipient,
            prompt=prompt,
            prompt_path=prompt_path,
            sources=tuple(parsed_sources),
            sections=tuple(parsed_sections),
        )


def load_category(path: str | Path) -> Category:
    """Load and validate a category config from a JSON file path.

    Raises:
        CategoryError (a ValueError) on schema violations.
        OSError / json.JSONDecodeError if the file can't be read or parsed.
    """
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return Category.from_dict(data, config_path)
