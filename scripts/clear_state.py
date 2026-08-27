"""Clear dedup + observability state for a debugging re-run.

Resets data/seen_items.json (dedup) so the next run treats every recent item
as unseen again, and data/run_log.jsonl (observability) so the next run starts
a clean log. Optionally also data/source_health.json.

Usage:
    python -m scripts.clear_state [--health]

This exists for debugging only: it deliberately erases history the pipeline
otherwise keeps. Use it to reproduce a fresh, pathological first-run — the
same state a brand-new checkout has before any digest has shipped.
"""

from __future__ import annotations

import argparse
import sys

from state import SEEN_ITEMS_PATH, RUN_LOG_PATH, SOURCE_HEALTH_PATH


def _clear_seen() -> None:
    SEEN_ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_ITEMS_PATH.write_text("{}\n")


def _clear_run_log() -> None:
    RUN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUN_LOG_PATH.write_text("")


def _clear_health() -> None:
    SOURCE_HEALTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_HEALTH_PATH.write_text("{}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clear_state", description=__doc__)
    parser.add_argument(
        "--health",
        action="store_true",
        help="also clear data/source_health.json (per-source failure history)",
    )
    args = parser.parse_args(argv)

    _clear_seen()
    print(f"cleared {SEEN_ITEMS_PATH} (dedup state)")
    _clear_run_log()
    print(f"cleared {RUN_LOG_PATH} (run log)")
    if args.health:
        _clear_health()
        print(f"cleared {SOURCE_HEALTH_PATH} (source health)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
