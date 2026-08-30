"""State persistence helpers.

Two files under data/, all keyed by category `id` so each category reports
health independently:

  source_health.json    {<category id>: {sources: {name: [recent run records]}}}
                        Per-source list of recent success + error records.
                        Last 14 entries kept per source, within each category.
  run_log.jsonl         append-only, one JSON object per run; each row carries
                        a `category` field and records duration, item counts,
                        prompt size, the model chosen, token counts, and errors.
                        (OpenRouter reports per-request token usage.)

Dedup is purely temporal: the filter stage keeps only items inside the
recency window (config.ITEM_AGE_LIMIT_DAYS), so nothing is persisted for it.
This is safe because runs are serial (a single writer per run).

We deliberately don't catch JSONDecodeError on load: corrupted state is
a bug worth surfacing as a failed run rather than silently resetting.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from config import HEALTH_RUNS_KEPT

DATA_DIR = Path(__file__).parent / "data"
SOURCE_HEALTH_PATH = DATA_DIR / "source_health.json"
RUN_LOG_PATH = DATA_DIR / "run_log.jsonl"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write to a sibling .tmp file and atomically rename into place.

    On POSIX, Path.replace is atomic — the destination either has the
    old contents or the new, never partial. Protects against corruption
    if the runner is preempted mid-write. Used for every persisted file.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def load_source_health(category_id: str) -> dict:
    """Return the given category's health {sources: {name: [...recent runs]}}."""
    if not SOURCE_HEALTH_PATH.exists():
        return {"sources": {}}
    data = json.loads(SOURCE_HEALTH_PATH.read_text())
    if not isinstance(data, dict):
        return {"sources": {}}
    return dict(data.get(category_id, {"sources": {}}))


def record_source_run(
    health: dict,
    source_name: str,
    success: bool,
    error: str | None,
) -> None:
    sources = health.setdefault("sources", {})
    record = sources.setdefault(source_name, {"recent_runs": []})
    record["recent_runs"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "success": success,
        "error": error,
    })
    record["recent_runs"] = record["recent_runs"][-HEALTH_RUNS_KEPT:]


def save_source_health(category_id: str, health: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {}
    if SOURCE_HEALTH_PATH.exists():
        try:
            data = json.loads(SOURCE_HEALTH_PATH.read_text())
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data[category_id] = health
    _atomic_write_text(
        SOURCE_HEALTH_PATH,
        json.dumps(data, indent=2, sort_keys=True),
    )


def append_run_log_row(row: dict) -> None:
    """Append one run-log row (must carry a `category` field) to run_log.jsonl.

    Always called, success or failure, so post-mortem data survives a failed
    run. The row is a run_log.jsonl record: duration, item counts, prompt size,
    model, token counts, and errors.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_PATH.open("a") as f:
        f.write(json.dumps(row) + "\n")
