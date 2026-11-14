"""End-to-end timing of a running API server (default http://127.0.0.1:8000).

    python scripts/check_server.py                     # random position + edge cases
    python scripts/check_server.py --ra 210.8 --dec -35.3

Prints, per position: manifest seconds, first band seconds (the band the page asks for first), the same
two requests again, and frame download time.
"""
from __future__ import annotations

import argparse
import random
import sys
import time

import requests


def timed(s, url, **params):
    t = time.time()
    r = s.get(url, params=params, timeout=200)
    return r, time.time() - t


def check(base, ra, dec, fov=None, band=None, label=""):
    s = requests.Session()
    p = {"ra": ra, "dec": dec}
    if fov:
        p["fov"] = fov
    r, t_man = timed(s, f"{base}/api/manifest", **p)
    m = r.json()
    line = f"{label:28s} ra={ra} dec={dec}: manifest {r.status_code} {t_man:5.1f}s"
    if r.status_code != 200 or not m.get("epochs"):
        print(line + f"  epochs={len(m.get('epochs', []))}  message={m.get('message')!r}", flush=True)
        return
    key = m["key"]
    bands = [b for b in m["bands"] if b["status"] != "empty"]
    b0 = band or m.get("default_band") or bands[0]["id"]
    r2, t_band = timed(s, f"{base}/api/band", key=key, band=b0)
    bj = r2.json()
    ep = next((e for e in bj.get("epochs", []) if e["n_used"] > 0), None)
    t_fr = None
    if ep:
        r3, t_fr = timed(s, f"{base}/api/frame", key=key, band=b0, epoch=ep["id"])
        assert r3.status_code == 200 and len(r3.content) == 4 * m["grid"]["shape"][0] * m["grid"]["shape"][1]
    r4, t_man2 = timed(s, f"{base}/api/manifest", **p)
    r5, t_band2 = timed(s, f"{base}/api/band", key=key, band=b0)
    print(line + f"  key={key} images={m.get('n_images')} epochs={len(m['epochs'])}"
          f"\n{'':28s} first band {b0}: {r2.status_code} {t_band:5.1f}s  n_used={[e['n_used'] for e in bj.get('epochs', [])]}"
          f"  frame {t_fr and round(t_fr, 2)}s"
          f"\n{'':28s} again: manifest {t_man2:4.2f}s  band {t_band2:4.2f}s  ({r4.status_code}, {r5.status_code})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--ra", type=float)
    ap.add_argument("--dec", type=float)
    ap.add_argument("--edges", action="store_true", help="also run the edge cases")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if a.ra is not None:
        check(a.base, a.ra, a.dec, label="given")
    else:
        rnd = random.Random()
        ra, dec = round(rnd.uniform(0, 360), 4), round(rnd.uniform(-60, 60), 4)
        check(a.base, ra, dec, label="random")
    if a.edges:  # random each run so the cache is cold
        rnd = random.Random()
        d = round(rnd.uniform(-60, 60), 4)
        check(a.base, round(rnd.uniform(0.0001, 0.001), 4), d, label="RA just above 0")
        check(a.base, round(rnd.uniform(359.999, 359.9999), 4), d, label="RA just below 360")
        check(a.base, round(rnd.uniform(0, 360), 4), round(rnd.uniform(85.5, 89.9), 4), label="|Dec| > 85 (north)")
        check(a.base, round(rnd.uniform(0, 360), 4), round(rnd.uniform(-89.9, -85.5), 4), label="|Dec| > 85 (south)")


if __name__ == "__main__":
    main()
