"""The live path may fetch layers=("IMAGE", "FLAGS") only (Cutout.variance = None). Check that co-adds and
photometry stay correct without VARIANCE.

Run:  python tests/test_coadd_novariance.py
Uses the cached WISE 0855 cutouts (fetched with VARIANCE) and drops the layer in memory.

Why it is safe: spx.coadd uses VARIANCE only as a validity mask (finite and > 0) and weights by a robust
per-exposure background sigma; every pixel with an invalid VARIANCE is already FLAGS-bad. spx.photometry
falls back to the annulus scatter for the flux error.
"""
from __future__ import annotations

import copy
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import numpy as np

from spx import access
from spx.coadd import BAD_MASK, build_frames, half_px_for, target_at
from spx.photometry import spectrum_points
from spx.targets import target_coord


def test_no_variance():
    tgt = target_coord("wise-0855")
    center = target_at(tgt, 61000.0)
    refs = access.find_images(center.ra.deg, center.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, center.ra.deg, center.dec.deg, half_px=half_px_for(7.0))
    assert all(c.variance is not None for c in cuts)

    # 1. every invalid-VARIANCE pixel is already masked by FLAGS
    unflagged_badvar = sum(int(((~np.isfinite(c.variance) | (c.variance <= 0)) & np.isfinite(c.image)
                                & ((c.flags & BAD_MASK) == 0)).sum()) for c in cuts)
    print("pixels with invalid VARIANCE but no bad flag:", unflagged_badvar)
    assert unflagged_badvar == 0

    nov = []
    for c in cuts:
        d = copy.copy(c)
        d.variance = None
        nov.append(d)

    # 2. frames identical (same masks, same weights)
    kw = dict(fov_arcmin=7.0, epochs=ep, center=center)
    for band_kw in ({}, {"band_um": (4.3, 5.0), "band_id": "W"}):
        a = build_frames(cuts, tgt, **kw, **band_kw)
        b = build_frames(nov, tgt, **kw, **band_kw)
        assert [(f.band, f.epoch, f.n_used) for f in a] == [(f.band, f.epoch, f.n_used) for f in b]
        worst = max(float(np.nanmax(np.abs(np.where(np.isfinite(x.image), x.image - y.image, 0)))) / x.noise
                    for x, y in zip(a, b))
        print(f"{band_kw.get('band_id', 'D1-D6')}: {len(a)} frames, max |difference| = {worst:.2e} noise")
        assert worst < 1e-3

    # 3. photometry: same fluxes, errors from the annulus instead of VARIANCE
    pa = spectrum_points(cuts, tgt, epochs=ep)["points"]
    pb = spectrum_points(nov, tgt, epochs=ep)["points"]
    assert len(pa) == len(pb)
    fa, fb = np.array([p["flux"] for p in pa]), np.array([p["flux"] for p in pb])
    ea, eb = np.array([p["err"] for p in pa]), np.array([p["err"] for p in pb])
    assert np.allclose(fa, fb)
    r = eb / ea
    print(f"spectrum: {len(pa)} points, flux identical; err(no VARIANCE)/err(VARIANCE) median {np.median(r):.2f}, "
          f"16-84% {np.percentile(r, 16):.2f}-{np.percentile(r, 84):.2f}")
    assert 0.5 < np.median(r) < 2.0


if __name__ == "__main__":
    test_no_variance()
