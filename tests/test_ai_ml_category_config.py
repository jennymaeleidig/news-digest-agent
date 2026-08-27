"""Tests for the broadened ai-ml category config and native RSS sources (ticket 02).

Ticket 02 broadens the single `ai-ml` category to cover the expanded reader ask:
the display `name` reads "AI" (the stable `ai-ml` id is unchanged, so existing
seen and source-health state survives), the curation prompt welcomes AI ecosystem
and regional news with substance (especially the Chinese model ecosystem), and
two plain `kind: rss` sources land in the category — radarai.top (tier 3) and
reddit r/LocalLLaMA (tier 4) — each with its own homepage and no topics
allow-list so curation ranks their quality.

The seam under test is the category loader (`load_category`) against the real
`categories/ai-ml.json` config and its sibling prompt file — the public
interface through which the category's name, sources, tiers, homepages, and
(un)filtering are validated and carried. These tests assert that external,
config-level behavior; they never assert parsing or fetch mechanics, which the
plain `kind: rss` sources intentionally do not introduce (they ride the existing
RSS fetcher).
"""

from __future__ import annotations

from pathlib import Path

from categories import load_category

REPO_ROOT = Path(__file__).resolve().parent.parent
AI_ML = REPO_ROOT / "categories" / "ai-ml.json"
PROMPT = REPO_ROOT / "categories" / "prompts" / "ai-ml.md"

RADARAI_URL = "https:" + "//radarai.top/en/feed.xml"
REDDIT_URL = "https:" + "//www.reddit.com/r/LocalLLaMA.rss"
HFPAPERS_URL = "https:" + "//huggingface.co/api/daily_papers"


# --- AC 1: display name "AI" with stable id "ai-ml" -------------------------
def test_category_name_is_ai_and_id_stays_ai_ml():
    """The category reads "AI" (email subject) while its id stays `ai-ml`, so
    seen and source-health state keeps its stable namespace."""
    cat = load_category(AI_ML)
    assert cat.id == "ai-ml"
    assert cat.name == "AI"


def test_prompt_survives_and_is_referenced():
    """The category's prompt file reference resolves to an existing sibling
    file — the curation-driving text stays present (loader hard-requires it)."""
    cat = load_category(AI_ML)
    assert cat.prompt_path.is_file()
    assert cat.prompt == "prompts/ai-ml.md"


# --- AC 2: curation prompt welcomes AI ecosystem / regional news ------------
def test_prompt_welcomes_ecosystem_and_chinese_models():
    """The curation prompt explicitly welcomes AI ecosystem and regional news
    with substance — especially the Chinese model ecosystem (DeepSeek, Qwen,
    GLM and peers) — while keeping the existing cut list. No AI-infrastructure /
    energy bucket is introduced."""
    text = PROMPT.read_text(encoding="utf-8").lower()
    for sub in ("deepseek", "qwen", "glm", "ecosystem", "regional"):
        assert sub in text, f"prompt should mention {sub!r}"
    # Keeps the existing cut list: linguistics / off-focus ML / hype listicles.
    for cut in ("listicle", "linguistic"):
        assert cut in text, f"prompt should keep cut-list term {cut!r}"
    # No AI-infrastructure/energy bucket is introduced — the prompt explicitly
    # rules energy OUT of scope (mentions it only to exclude it), instead of
    # dedicating a section/bucket to it.
    assert "energy" in text
    assert "off this digest's focus" in text or "out of scope" in text
    # Digest subject reflects the broadened coverage ("AI digest").
    assert "ai" in text and "llm" in text


# --- AC 3 & 4: the two native RSS sources land with tiers and homepages -----
def test_radarai_is_tier3_rss_with_homepage_unfiltered():
    src = _source_named("radarai")
    assert src.kind == "rss"
    assert src.tier == 3
    assert src.url == RADARAI_URL
    assert src.homepage and src.homepage.startswith("https:")
    assert src.topics == ()          # unfiltered


def test_reddit_localllama_is_tier4_rss_with_homepage_unfiltered():
    src = _source_named("LocalLLaMA")
    assert src.kind == "rss"
    assert src.tier == 4
    assert src.url == REDDIT_URL
    assert src.homepage and src.homepage.startswith("https:")
    assert src.topics == ()          # unfiltered


# --- AC 3 & 4: they flow through the unchanged RSS path (kind: rss) --------
def test_new_sources_are_plain_rss_no_bespoke_config():
    """Both new sources are plain `kind: rss` — no fetcher_config, so they ride
    the existing RSS fetcher unchanged."""
    for name in ("radarai", "LocalLLaMA"):
        src = _source_named(name)
        assert src.kind == "rss"
        assert src.fetcher_config is None


# --- ticket 03: the HF Daily Papers bespoke source --------------------------
def test_hf_papers_is_tier3_bespoke_with_homepage_and_topics():
    """The HF Daily Papers source is a tier-3 huggingface_papers bespoke kind
    with its own homepage, a topics allow-list scoping the broad feed to
    on-focus LLM/agent items, and the shared fetcher-config (item/title/link/
    date field paths)."""
    src = _source_named("Daily Papers")
    assert src.kind == "huggingface_papers"
    assert src.tier == 3
    assert src.homepage and src.homepage.startswith("https:")
    assert src.homepage == "https:" + "//huggingface.co/papers"
    # topics allow-list keeps only on-focus LLM/agent items.
    assert src.topics
    assert all(isinstance(t, str) and t for t in src.topics)
    for on_focus in ("llm", "language model", "agent", "reasoning",
                     "fine-tun", "chain-of-thought"):
        assert on_focus in src.topics
    # configured through the shared fetcher-config schema.
    fc = src.fetcher_config
    assert fc is not None
    assert fc.url == src.url
    assert set((fc.item, fc.title, fc.link, fc.date)) == {"$", "title", "paper.id", "paper.submittedOnDailyAt"}


def test_hf_papers_fetch_endpoint():
    """The HF Daily Papers source points at the JSON endpoint (no RSS / no
    scraping / no headless browser)."""
    src = _source_named("Daily Papers")
    assert src.url == HFPAPERS_URL


# --- helpers ---------------------------------------------------------------
def _source_named(name_fragment) -> object:
    cat = load_category(AI_ML)
    for src in cat.sources:
        if name_fragment.lower() in src.name.lower():
            return src
    raise AssertionError(f"no source named like {name_fragment!r} in ai-ml")
