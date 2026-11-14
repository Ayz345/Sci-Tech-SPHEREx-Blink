"""Static WISE layers of the gallery (web/data/<key>/wise.json, wise_W1.f32, wise_W2.f32), offline.

(1) orientation and scale: the 10 brightest Gaia stars in the field, moved to the WISE epoch, sit on a W1 peak;
    median offset < 1 grid pixel.
(2) WISE 0855: W2 has a source within 2 px of the challenge position (133.7951, -7.2451; the object's ~2010
    position) and W1 has no comparable source there (the object is a Y dwarf, very red in W1-W2).
    python -m pytest -q tests/test_wise_images.py
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from astropy.wcs import WCS

DATA = Path(__file__).resolve().parent.parent / "web" / "data"
KEYS = ["barnard", "eps-indi-b", "wise-0855", "rw-aur", "r-dra"]
GAIA_MJD = 57388.5            # Gaia DR3 reference epoch J2016.0


def _load(key):
    d = DATA / key
    if not (d / "wise.json").exists():
        pytest.skip(f"{key}: no wise.json (scripts/make_wise_images.py)")
    m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
    w = json.loads((d / "wise.json").read_text(encoding="utf-8"))
    ny, nx = m["grid"]["shape"]
    imgs = {b["id"]: np.fromfile(d / b["file"], dtype="<f4").reshape(ny, nx) for b in w["bands"]}
    g = m["grid"]
    wcs = WCS(naxis=2)
    wcs.wcs.ctype, wcs.wcs.crval, wcs.wcs.crpix, wcs.wcs.cdelt = g["ctype"], g["crval"], g["crpix"], g["cdelt"]
    gaia = json.loads((d / "gaia.json").read_text(encoding="utf-8")) if (d / "gaia.json").exists() else []
    return m, w, imgs, wcs, gaia


def _peak_offset(img, x, y, r=3):
    """Distance (px) from (x, y) to the brightest pixel within r px, and that pixel's value."""
    ny, nx = img.shape
    x0, x1, y0, y1 = max(0, int(x) - r), min(nx, int(x) + r + 2), max(0, int(y) - r), min(ny, int(y) + r + 2)
    sub = img[y0:y1, x0:x1]
    if not np.isfinite(sub).any():
        return None, None
    j, i = np.unravel_index(np.nanargmax(sub), sub.shape)
    return float(np.hypot(x0 + i - x, y0 + j - y)), float(sub[j, i])


@pytest.mark.parametrize("key", KEYS)
def test_wise_files_match_the_grid(key):
    m, w, imgs, _, _ = _load(key)
    assert w["schema"] == 1 and [b["id"] for b in w["bands"]] == ["W1", "W2"]
    for b in w["bands"]:
        assert imgs[b["id"]].shape == tuple(m["grid"]["shape"]) and b["scale"]["vmax"] > b["scale"]["vmin"]
    assert (w["epoch_mjd"] is None) == (w["date"] is None) and w["epoch_source"]


@pytest.mark.parametrize("key", KEYS)
def test_brightest_gaia_stars_sit_on_w1_peaks(key):
    m, w, imgs, wcs, gaia = _load(key)
    if w["epoch_mjd"] is None or not gaia:
        pytest.skip("no WISE epoch or no Gaia list")
    dt = (w["epoch_mjd"] - GAIA_MJD) / 365.25
    ny, nx = m["grid"]["shape"]
    stars = []
    for s in sorted((s for s in gaia if s.get("g_mag") is not None), key=lambda s: s["g_mag"]):
        dec = s["dec"] + (s.get("pmdec") or 0.0) / 3.6e6 * dt
        ra = s["ra"] + (s.get("pmra") or 0.0) / 3.6e6 * dt / np.cos(np.radians(s["dec"]))
        x, y = wcs.world_to_pixel_values(ra, dec)
        if 5 <= x <= nx - 6 and 5 <= y <= ny - 6:
            stars.append((s["g_mag"], float(x), float(y)))
        if len(stars) == 10:
            break
    offs = [o for _, x, y in stars for o in [_peak_offset(imgs["W1"], x, y)[0]] if o is not None]
    assert len(offs) >= 5, stars
    assert float(np.median(offs)) < 1.0, (key, [round(o, 2) for o in offs])


def test_wise0855_red_in_w1_w2():
    m, w, imgs, wcs, _ = _load("wise-0855")
    x, y = wcs.world_to_pixel_values(133.7951, -7.2451)
    res = {}
    for b in ("W1", "W2"):
        img = imgs[b]
        fin = img[np.isfinite(img)]
        med = float(np.median(fin))
        sig = float(np.median(np.abs(fin - med))) * 1.4826
        off, val = _peak_offset(img, float(x), float(y), r=2)
        res[b] = {"offset_px": off, "peak_sigma": (val - med) / sig}
    assert res["W2"]["offset_px"] <= 2.0 and res["W2"]["peak_sigma"] > 5, res
    assert res["W1"]["peak_sigma"] < res["W2"]["peak_sigma"] / 3, res
