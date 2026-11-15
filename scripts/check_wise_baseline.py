"""WISE 0855 over 16 years: its position in the AllWISE W2 image (2010) and in the SPHEREx W pass of 2026, both on
the gallery's 3 arcsec grid, and the motion between them.

    python scripts/check_wise_baseline.py            # writes cache/wise0855_wise_baseline.json
    python scripts/check_wise_baseline.py --write    # also handoff/results/wise0855_wise_baseline.json (tracked)

Reads only committed gallery files (web/data/wise-0855/: manifest.json, W.json, wise.json, wise_W2.f32, 2_W.f32), no
network. Position in each image: background-subtracted first moment within 3 grid pixels of the brightest pixel near
the expected place. Dates: the Atlas tile's MIDOBS (2010-05-06) and the W pass's mid exposure time (2026-04-26). The
Atlas co-adds 2010 exposures from DATEOBS1 to DATEOBS2, so the rate is also given for both ends of that range.

The site's "8.1 ± 0.3 ″/yr" comes from the gallery tool itself (one mark in the WISE frame, one in the 2026 SPHEREx
frame, read 8.07 ± 0.27 and rounded); its ± assumes each mark is good to one grid pixel, sqrt(2) x 3″ / 15.97 yr.
This script is the same measurement without clicks.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path

import numpy as np
from astropy.time import Time
from astropy.wcs import WCS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "web" / "data" / "wise-0855"
LIT = {"ref": "Luhman & Esplin (2016)", "pm_east": -8.12, "pm_north": 0.68}   # arcsec/yr (mu_alpha* , mu_delta)
PASS = 2   # the 2026 SPHEREx pass


def centroid(a: np.ndarray, x0: float, y0: float, search: int = 5, r: int = 3) -> tuple[float, float]:
    """First moment (x, y) within r pixels of the brightest pixel within `search` pixels of (x0, y0)."""
    xi, yi = int(round(x0)), int(round(y0))
    box = a[yi - search:yi + search + 1, xi - search:xi + search + 1]
    yy, xx = np.mgrid[-search:search + 1, -search:search + 1]
    py, px = np.unravel_index(np.nanargmax(np.where(np.hypot(yy, xx) <= search, box, -np.inf)), box.shape)
    py, px = yi - search + py, xi - search + px
    bg = float(np.nanmedian(a[py - 15:py + 16, px - 15:px + 16]))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    s = np.clip(a[py - r:py + r + 1, px - r:px + r + 1] - bg, 0, None) * (np.hypot(yy, xx) <= r)
    return float(px + (s * xx).sum() / s.sum()), float(py + (s * yy).sum() / s.sum())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)
    m = json.loads((DATA / "manifest.json").read_text("utf-8"))
    wise = json.loads((DATA / "wise.json").read_text("utf-8"))
    band = json.loads((DATA / "W.json").read_text("utf-8"))
    g = m["grid"]
    w = WCS(naxis=2)
    w.wcs.ctype, w.wcs.crval, w.wcs.crpix, w.wcs.cdelt = g["ctype"], g["crval"], g["crpix"], g["cdelt"]
    ny, nx = g["shape"][1], g["shape"][0]
    pix = abs(g["cdelt"][1]) * 3600

    ep = next(e for e in band["epochs"] if e["id"] == PASS)
    t_spx, t_wise = ep["mjd_mid"], wise["epoch_mjd"]
    tile = wise["tiles"][0]
    tr = next(t for t in m["target_track"] if t["epoch"] == PASS)
    sx, sy = (float(v) for v in w.all_world2pix(tr["ra"], tr["dec"], 0))
    # expected 2010 place: the 2026 track position moved back with the literature motion
    yrs = (t_wise - t_spx) / 365.25
    ex = sx - LIT["pm_east"] * yrs / pix      # east is towards smaller x (cdelt1 < 0)
    ey = sy + LIT["pm_north"] * yrs / pix

    img_spx = np.fromfile(DATA / f"{PASS}_W.f32", np.float32).reshape(ny, nx)
    img_w2 = np.fromfile(DATA / "wise_W2.f32", np.float32).reshape(ny, nx)
    p_spx = centroid(img_spx, sx, sy)
    p_w2 = centroid(img_w2, ex, ey)
    (ra1, dec1), (ra2, dec2) = ([float(v) for v in w.all_pix2world(*p, 0)] for p in (p_w2, p_spx))
    east = (((ra2 - ra1 + 540) % 360) - 180) * math.cos(math.radians(dec1)) * 3600
    north = (dec2 - dec1) * 3600
    sep = math.hypot(east, north)
    years = (t_spx - t_wise) / 365.25
    y_end = {k: (t_spx - Time(tile[k]).mjd) / 365.25 for k in ("dateobs1", "dateobs2")}
    res = {
        "checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": "web/data/wise-0855/: wise_W2.f32 (AllWISE Atlas W2 on the 3 arcsec grid), "
                 f"{PASS}_W.f32 (SPHEREx W pass), wise.json, W.json, manifest.json",
        "grid_pixel_arcsec": round(pix, 3),
        "wise_2010": {"image": "AllWISE Atlas W2 (4.6 um)", "coadd_id": tile["coadd_id"],
                      "date": wise["date"], "mjd": t_wise, "dateobs1": tile["dateobs1"], "dateobs2": tile["dateobs2"],
                      "ra": round(ra1, 6), "dec": round(dec1, 6),
                      "offset_from_expected_arcsec": round(math.hypot(p_w2[0] - ex, p_w2[1] - ey) * pix, 2)},
        "spherex_2026": {"image": f"SPHEREx W (4.3-5.0 um) pass {PASS}", "date": ep["date"], "mjd": round(t_spx, 4),
                         "exposures": ep["n_used"], "ra": round(ra2, 6), "dec": round(dec2, 6)},
        "shift_arcsec": round(sep, 1), "east_arcsec": round(east, 1), "north_arcsec": round(north, 1),
        "years": round(years, 2),
        "rate_arcsec_yr": round(sep / years, 2),
        "pm_east_arcsec_yr": round(east / years, 2), "pm_north_arcsec_yr": round(north / years, 2),
        "rate_for_tile_date_range_arcsec_yr": [round(sep / y_end["dateobs1"], 2), round(sep / y_end["dateobs2"], 2)],
        "literature": LIT | {"rate": round(math.hypot(LIT["pm_east"], LIT["pm_north"]), 2)},
        "tool_two_marks": {
            "site_value": "8.1 ± 0.3 ″/yr (tool reading 8.07 ± 0.27, rounded)",
            "uncertainty_rule": "one grid pixel per mark: sqrt(2) x pixel / years",
            "uncertainty_arcsec_yr": round(math.sqrt(2) * pix / years, 2)},
        # an earlier, separate reading of the same two images (W2 centre on the same grid, 15 Nov 2026)
        "separate_check": {"shift_arcsec": 128.8, "years": 15.97, "rate_arcsec_yr": 8.06, "pm_east_arcsec_yr": -8.03,
                           "pm_north_arcsec_yr": 0.73, "rate_for_tile_date_range_arcsec_yr": [8.06, 8.34]},
    }
    text = json.dumps(res, indent=1, ensure_ascii=False)
    print(json.dumps(res, indent=1))
    (ROOT / "cache").mkdir(exist_ok=True)
    (ROOT / "cache" / "wise0855_wise_baseline.json").write_bytes(text.encode("utf-8"))
    if a.write:
        (ROOT / "handoff" / "results" / "wise0855_wise_baseline.json").write_bytes(text.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
