"""candidates.json for the gallery targets, from the frames already in web/data (nothing is rebuilt or downloaded).

    python scripts/make_candidates.py                 # every target in web/data/index.json
    python scripts/make_candidates.py wise-0855 barnard
    python scripts/make_candidates.py --data some/other/web/data

Same call as the live server (server.pipeline.make_candidates -> spx.detect.find_candidates(load_field(dir))),
so a gallery file and a live answer for the same frames are the same. Each target folder must hold manifest.json,
the band files and gaia.json (precompute.py writes them). PLAN.md 17 F2; these small JSON files may be committed
(exception to 14.6).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import pipeline as P  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="*", help="target keys (default: all in index.json)")
    ap.add_argument("--data", default=str(ROOT / "web" / "data"))
    a = ap.parse_args(argv)
    data = Path(a.data)
    if P.detect_module() is None:
        print("spx.detect.find_candidates is not available (quality session, PLAN.md 17 F2)", file=sys.stderr)
        return 2
    keys = a.keys or [t["key"] for t in json.loads((data / "index.json").read_text(encoding="utf-8"))["targets"]]
    bad = 0
    for key in keys:
        d = data / key
        if not (d / "manifest.json").exists():
            print(f"{key}: no manifest.json in {d}", file=sys.stderr)
            bad += 1
            continue
        if not (d / "gaia.json").exists():
            print(f"{key}: no gaia.json in {d} (the known-star check needs it)", file=sys.stderr)
            bad += 1
            continue
        t0 = time.time()
        out = P.make_candidates(d, key)
        out["seconds"] = round(time.time() - t0, 2)
        P.write_json(d / "candidates.json", out)
        kinds = {}
        for c in out.get("candidates", []):
            kinds[c.get("kind")] = kinds.get(c.get("kind"), 0) + 1
        print(f"{key}: {len(out.get('candidates', []))} candidates {kinds}, rejected {out.get('rejected')}, "
              f"bands {out.get('bands_searched')}, {out['seconds']} s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
