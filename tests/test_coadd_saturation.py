"""Regression test for saturated cores (no "no data" ring around bright stars).

Run:  python tests/test_coadd_saturation.py      (all five gallery targets, all six detector bands)
Writes handoff/results/saturation_check_v2.json.

Rules checked inside 36" of the target, on grid pixels that have coverage (flag_frac finite):
  1. NaN count = 0: NaN only ever means "no exposure covers this pixel".
  2. barnard, r-dra: the target pixel is +inf (saturated) in every band where > 15% of the exposures are truly
     saturated at the target (early overflow / NONLINEAR); spx.coadd uses the same 15% rule.
  3. rw-aur and any finite core: PSF-like, the brightest finite pixel within 3 px lies within 2 px (6", less than
     half the ~10" FWHM) of the target; rw-aur cores are finite where <= 15% of exposures are truly saturated.
The fraction of exposures truly saturated at the target is reported per band and epoch (it decides 2 and 3).
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import numpy as np

from spx import access
from spx.coadd import build_frames, half_px_for, saturated_pixels, target_at
from spx.targets import TARGETS, target_coord

OUT = ROOT / "handoff" / "results" / "saturation_check_v2.json"
R_ARCSEC = 36.0


def sat_fraction(cuts, tgt):
    """Fraction of exposures whose pixel at the target is truly saturated."""
    out = []
    for c in cuts:
        x, y = [int(round(float(v))) for v in c.wcs.world_to_pixel(target_at(tgt, c.mjd))]
        if 0 <= y < c.flags.shape[0] and 0 <= x < c.flags.shape[1]:
            out.append(bool(saturated_pixels(c.flags[y, x])))
    return float(np.mean(out)) if out else 0.0


def run():
    rows = []
    for key in TARGETS:
        tgt = target_coord(key)
        c = target_at(tgt, 61000.0)
        refs = access.find_images(c.ra.deg, c.dec.deg)
        _, ep = access.epochs_for(refs)
        cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
        for d in range(1, 7):
            dc = [x for x in cuts if x.detector == d]
            fs = build_frames(dc, tgt, epochs=ep, center=c)
            for f in fs:
                ids = set(f.obs_ids)
                sf = sat_fraction([x for x in dc if x.obs_id in ids], tgt)  # this epoch's exposures
                yy, xx = np.mgrid[0:f.image.shape[0], 0:f.image.shape[1]]
                tx, ty = f.target_xy
                near = np.hypot(xx - tx, yy - ty) * f.grid.pix_arcsec <= R_ARCSEC
                covered = near & np.isfinite(f.flag_frac)
                xi, yi = int(round(tx)), int(round(ty))
                core = float(f.image[yi, xi])
                fin = np.where(np.isfinite(f.image) & (np.hypot(xx - tx, yy - ty) <= 3), f.image, -np.inf)
                j = np.argmax(fin)
                peak_off = float(np.hypot(xx.flat[j] - tx, yy.flat[j] - ty)) if np.isfinite(fin.flat[j]) else None
                rows.append({"target": key, "band": f.band, "epoch": f.epoch, "date": f.epoch_label,
                             "sat_fraction_at_target": round(sf, 3), "nan_covered": int(np.isnan(f.image[covered]).sum()),
                             "inf_near": int(np.isposinf(f.image[near]).sum()), "covered_px": int(covered.sum()),
                             "core": "inf" if np.isposinf(core) else ("nan" if np.isnan(core) else round(core, 4)),
                             "peak_offset_px": round(peak_off, 2) if peak_off is not None else None})
    OUT.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return rows


def test_saturated_cores():
    rows = run()
    print("target      band ep  sat%  NaN(cov) +inf  core        peak_off")
    for r in rows:
        print(f"{r['target']:11s} {r['band']}  {r['epoch']}  {100 * r['sat_fraction_at_target']:4.0f}  {r['nan_covered']:7d} {r['inf_near']:5d}  "
              f"{str(r['core']):10s}  {r['peak_offset_px']}")
    bad = [r for r in rows if r["nan_covered"] > 0]
    assert not bad, f"NaN inside covered area: {[(r['target'], r['band'], r['epoch'], r['nan_covered']) for r in bad]}"
    for r in rows:
        if r["target"] in ("barnard", "r-dra") and r["sat_fraction_at_target"] > 0.15:
            assert r["core"] == "inf", r
        if r["target"] == "rw-aur" and r["sat_fraction_at_target"] <= 0.15:
            assert r["core"] not in ("inf", "nan"), r
        if r["core"] not in ("inf", "nan") and r["target"] in ("barnard", "rw-aur", "r-dra"):
            assert r["peak_offset_px"] is not None and r["peak_offset_px"] <= 2.0, r


if __name__ == "__main__":
    test_saturated_cores()
