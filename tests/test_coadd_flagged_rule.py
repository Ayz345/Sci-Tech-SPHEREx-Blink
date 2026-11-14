"""The aperture 'flagged' rule and masked-pixel handling of spx.photometry.aperture_flux (v1.2, 14 Nov).

Run:  python tests/test_coadd_flagged_rule.py     (cached cutouts; result in cache/test_results/, --write updates
      handoff/results/flagged_rule_v2.json, see tests/resultio.py)

Rule under test: masked pixels (any non-informational FLAGS bit except recovered overflow, or no data) are left
out of the aperture sum and the flux is scaled by the aperture fraction (saturated cores are kept: lower limit);
flagged = masked aperture weight > 15%, or a masked pixel within 1 px of the centre, or a lower limit.

  stars      isolated, unsaturated Gaia DR3 stars (G 11-16.5, no other G < 19 source within 15") in the WISE 0855,
             Eps Indi B and RW Aur fields: flux / median of the UNFLAGGED points of the same star, band and epoch
             within 1.5% in wavelength, binned by masked fraction (core clean) and for a masked core.
  empty sky  80 random positions without a Gaia source within 15" (WISE 0855 and Eps Indi B fields), every
             exposure: flux / err (expected median 0, scatter 1). OUTLIER is never set on known-source pixels
             (Explanatory Supplement v2.1 sect. 3.2.10: SOURCE pixels are masked before the search), so its effect
             can only be measured here. Also fixed-position PSF photometry (psf_flux) at the same positions.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time

from resultio import write_json
from spx import access, code_hash, frame_code_hash
from spx.coadd import FLAG_BITS, BAD_MASK, assign_epochs, half_px_for, target_at, usable_pixels
from spx.photometry import FLAG_CORE_PX, FLAG_MASKED_FRAC, aperture_flux, psf_flux
from spx.targets import target_coord

OUT_NAME = "flagged_rule_v2.json"
OUTLIER = 1 << FLAG_BITS["OUTLIER"]
BINS = ((0.0, 1e-9, "none masked"), (1e-9, 0.05, "<5%"), (0.05, 0.15, "5-15%"), (0.15, 1.01, ">15%"))


def _cuts(key):
    tgt = target_coord(key)
    c = target_at(tgt, 61000.0)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    return c, cuts, assign_epochs(np.array([x.mjd for x in cuts]), ep)


def _core(cu, x, y):
    fl = np.asarray(cu.flags)
    yy, xx = np.mgrid[0:fl.shape[0], 0:fl.shape[1]]
    bad = ~(usable_pixels(fl) & np.isfinite(cu.image))
    near = np.hypot(xx - x, yy - y) <= FLAG_CORE_PX
    return bool(np.any(bad & near)), bool(np.any((fl & OUTLIER) != 0) and np.any(((fl & OUTLIER) != 0) & (np.hypot(xx - x, yy - y) <= 3)))


def _stats_ratio(v):
    v = np.asarray(v, float)
    if not v.size:
        return {"n": 0}
    return {"n": int(v.size), "median_ratio": round(float(np.median(v)), 3),
            "robust_scatter": round(float(1.4826 * np.median(np.abs(v - np.median(v)))), 3),
            "frac_off_by_10pct": round(float(np.mean(np.abs(v - 1) > 0.10)), 3)}


def _stats_pull(v):
    v = np.asarray([x for x in v if np.isfinite(x)], float)
    if not v.size:
        return {"n": 0}
    return {"n": int(v.size), "median": round(float(np.median(v)), 2),
            "robust_scatter": round(float(1.4826 * np.median(np.abs(v - np.median(v)))), 2),
            "frac_abs_gt_5": round(float(np.mean(np.abs(v) > 5)), 4)}


def stars_part():
    rows = []
    for key in ("wise-0855", "eps-indi-b", "rw-aur"):
        gaia = json.loads((ROOT / "web" / "data" / key / "gaia.json").read_text(encoding="utf-8"))
        c, cuts, lab = _cuts(key)
        allc = SkyCoord([g["ra"] for g in gaia], [g["dec"] for g in gaia], unit="deg")
        for i, g in enumerate(gaia):
            if not (11.0 <= g["g_mag"] <= 16.5) or allc[i].separation(c).arcmin > 2.8:
                continue
            sep = allc.separation(allc[i]).arcsec
            if np.sum((sep > 0.1) & (sep < 15)) > 0:
                continue
            sc = SkyCoord(g["ra"] * u.deg, g["dec"] * u.deg, pm_ra_cosdec=(g.get("pmra") or 0) * u.mas / u.yr,
                          pm_dec=(g.get("pmdec") or 0) * u.mas / u.yr, obstime=Time(2016.0, format="jyear"))
            pts = []
            for cu, e in zip(cuts, lab):
                x, y = [float(v) for v in cu.wcs.world_to_pixel(target_at(sc, cu.mjd))]
                r = aperture_flux(cu, x, y)
                if r is None or r[4]:
                    continue
                core, _ = _core(cu, x, y)
                pts.append({"band": cu.detector, "epoch": int(e), "wave": float(cu.wavelength_um), "flux": r[0],
                            "err": r[1], "flagged": r[2], "mfrac": r[6], "core": core})
            for p in pts:
                same = [q for q in pts if q is not p and not q["flagged"] and q["band"] == p["band"]
                        and q["epoch"] == p["epoch"] and abs(q["wave"] - p["wave"]) <= 0.015 * p["wave"]]
                if len(same) >= 2:
                    ref = float(np.median([q["flux"] for q in same]))
                    if ref > 5 * float(np.median([q["err"] for q in same])):
                        rows.append({**p, "ratio": p["flux"] / ref})
    out = {"n_points": len(rows), "n_flagged": sum(r["flagged"] for r in rows)}
    for lo, hi, name in BINS:
        out[f"masked {name}, core clean"] = _stats_ratio([r["ratio"] for r in rows if lo <= r["mfrac"] < hi and not r["core"]])
    out["masked core"] = _stats_ratio([r["ratio"] for r in rows if r["core"]])
    out["unflagged"] = _stats_ratio([r["ratio"] for r in rows if not r["flagged"]])
    out["flagged"] = _stats_ratio([r["ratio"] for r in rows if r["flagged"]])
    return out


def empty_part():
    rng = np.random.default_rng(1)
    rows, psf_pulls = [], []
    for key in ("wise-0855", "eps-indi-b"):
        gaia = json.loads((ROOT / "web" / "data" / key / "gaia.json").read_text(encoding="utf-8"))
        gc = SkyCoord([g["ra"] for g in gaia], [g["dec"] for g in gaia], unit="deg")
        c, cuts, _ = _cuts(key)
        pos = []
        while len(pos) < 40:
            p = c.spherical_offsets_by(*(rng.uniform(-150, 150, 2) * u.arcsec))
            if gc.separation(p).arcsec.min() > 15:
                pos.append(p)
        for cu in cuts:
            fw = cu.psf_fwhm_arcsec if np.isfinite(cu.psf_fwhm_arcsec) else 7.5
            for p in pos:
                x, y = [float(v) for v in cu.wcs.world_to_pixel(p)]
                r = aperture_flux(cu, x, y)
                if r is None or not (r[1] > 0):
                    continue
                core, outl = _core(cu, x, y)
                rows.append({"pull": r[0] / r[1], "flagged": r[2], "mfrac": r[6], "core": core, "outlier": outl})
                q = psf_flux(cu, [(x, y)], fw / 6.15)
                if q is not None and q[1][0] > 0 and not q[2]:
                    psf_pulls.append(float(q[0][0] / q[1][0]))
    out = {"n_points": len(rows), "n_flagged": sum(r["flagged"] for r in rows),
           "unflagged": _stats_pull([r["pull"] for r in rows if not r["flagged"]])}
    for lo, hi, name in BINS[1:]:
        for kind, sel in (("with OUTLIER", lambda r: r["outlier"]), ("without OUTLIER", lambda r: not r["outlier"])):
            out[f"masked {name} {kind}, core clean"] = _stats_pull(
                [r["pull"] for r in rows if lo <= r["mfrac"] < hi and sel(r) and not r["core"]])
    out["masked core"] = _stats_pull([r["pull"] for r in rows if r["core"]])
    out["psf_unflagged"] = _stats_pull(psf_pulls)
    return out


def run():
    res = {"code_hash": code_hash(), "frame_code_hash": frame_code_hash(),
           "rule": {"flag_masked_frac": FLAG_MASKED_FRAC, "flag_core_px": FLAG_CORE_PX,
                    "masked_pixels": "left out of the aperture sum, flux scaled by the aperture fraction; "
                                     "truly saturated pixels kept (lower limit)"},
           "stars": stars_part(), "empty_sky": empty_part()}
    write_json(OUT_NAME, res)
    return res


def test_flagged_rule():
    res = run()
    for part in ("stars", "empty_sky"):
        for k, v in res[part].items():
            print(f"{part:9s} {k:42s} {v}")
    s, e = res["stars"], res["empty_sky"]
    assert 0.97 <= s["unflagged"]["median_ratio"] <= 1.03
    for name in ("<5%", "5-15%"):
        b = s[f"masked {name}, core clean"]
        if b["n"] >= 10:
            assert 0.95 <= b["median_ratio"] <= 1.06, (name, b)
    assert e["unflagged"]["frac_abs_gt_5"] < 0.03
    assert abs(e["unflagged"]["median"]) < 0.3


if __name__ == "__main__":
    test_flagged_rule()
