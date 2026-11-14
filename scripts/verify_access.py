"""Measure spx.access on the five targets: images found, cutouts read, skipped, seconds, MB.

    python scripts/verify_access.py [key ...] [--half-px 53] [--cap 60] [--twice]

--cap N applies the contract cap (select_spread, N per epoch x detector) BEFORE downloading.
--twice runs fetch_cutouts again to show the second request comes from the disk cache.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from spx import access  # noqa: E402
from spx.targets import TARGETS, position_at  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", default=list(TARGETS))
    ap.add_argument("--half-px", type=int, default=53)
    ap.add_argument("--cap", type=int, default=0)
    ap.add_argument("--twice", action="store_true")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    for key in a.keys:
        t = TARGETS[key]
        t0 = time.time()
        refs = access.find_images(t["ra"], t["dec"])
        t_sia = time.time() - t0
        if not refs:
            print(f"{key}: no images")
            continue
        mjd_mid = float(np.median([r.mjd for r in refs]))
        ra, dec = position_at(key, mjd_mid)
        if a.cap:
            refs = access.select_refs(refs, a.cap)
        per_det = {d: sum(r.detector == d for r in refs) for d in range(1, 7)}
        for run in range(2 if a.twice else 1):
            st = {}
            cuts, nskip = access.fetch_cutouts(refs, ra, dec, half_px=a.half_px, stats=st)
            print(f"{key:11s} run{run}: SIA2 {t_sia:4.1f}s  refs {len(refs):3d} {per_det}  ok {st['n_ok']:3d}  "
                  f"skipped {nskip:2d}  from_cache {st['n_from_cache']:3d}  {st['seconds']:6.1f}s  "
                  f"{st['bytes'] / 1e6:7.1f} MB  reasons {st['skip_reasons']}", flush=True)
        if cuts:
            c = cuts[len(cuts) // 2]
            print(f"   sample: D{c.detector} {c.obs_id} mjd {c.mjd:.3f} shape {c.shape} x0,y0 {c.x0},{c.y0} "
                  f"wl {c.wavelength_um:.4f} um  finite {np.isfinite(c.image).mean():.2f}", flush=True)


if __name__ == "__main__":
    main()
