"""One-off audit: fetch every source, dump recent titles to /tmp/audit.txt."""
import pathlib
import sys

from categories import load_category
from fetchers.registry import fetch_one

N = int(sys.argv[1]) if len(sys.argv) > 1 else 14

with open("/tmp/audit.txt", "w") as out:
    for f in sorted(pathlib.Path("categories").glob("*.json")):
        cat = load_category(str(f))
        print(f"===== {cat.id} — {cat.name}", file=out)
        seen = set()
        for s in cat.sources:
            if s.name in seen:
                print(f"  [{s.name}] (dup across categories, skipped here)", file=out)
                continue
            seen.add(s.name)
            r = fetch_one(s)
            items = r.items or []
            ok = "OK" if r.success else "FAIL " + str(r.error)
            print(f"  [{s.name}] {ok} ({len(items)} items)", file=out)
            for it in items[:N]:
                print(f"      - {it.title[:95]}", file=out)
print("done")
