"""RW Aurigae variability against control stars, as defined in spx.photometry.VARIABILITY_METHOD.

Run:  python tests/test_coadd_variability.py
Writes handoff/results/variability_v2.json.

Same method as server/pipeline.variability_report (the published numbers): target points PSF-deblended against
Gaia DR3 neighbours (G < 19, sources within 3" of the target at 2016.0 - RW Aur A and its 1.5" companion B - are not
neighbours); "saturated" decided from the points (> 50% saturated); controls from select_controls with that flag;
every control measured with spectrum_points -> binned_series -> variability in the same cutouts. A band is variable
when >= 5 controls have a ratio, the target ratio > 1.15 and > the largest control ratio (is_variable). Bands
resting on a single wavelength bin are marked weak (n_bins = 1).
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
import astropy.units as u
import pyvo
from astropy.coordinates import SkyCoord
from astropy.time import Time

from spx import access
from spx.coadd import half_px_for, target_at
from spx.photometry import VARIABILITY_METHOD, binned_series, is_variable, select_controls, spectrum_points, variability
from spx.targets import TARGETS, target_coord

OUT = ROOT / "handoff" / "results" / "variability_v2.json"
KEY = "rw-aur"


def run():
    tgt = target_coord(KEY)
    c = target_at(tgt, 61000.0)
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec, pmra, pmdec, phot_g_mean_mag as g_mag from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{c.ra.deg},{c.dec.deg},0.09)) and phot_g_mean_mag < 19").to_table()
    rows = [{"ra": float(r["ra"]), "dec": float(r["dec"]), "g_mag": float(r["g_mag"]),
             "pmra": float(r["pmra"]) if np.isfinite(r["pmra"]) else 0.0,
             "pmdec": float(r["pmdec"]) if np.isfinite(r["pmdec"]) else 0.0} for r in t]
    t16 = SkyCoord(TARGETS[KEY]["ra"], TARGETS[KEY]["dec"], unit="deg")
    sep = [SkyCoord(g["ra"], g["dec"], unit="deg").separation(t16).arcsec for g in rows]
    target_g = min((g["g_mag"] for g, d in zip(rows, sep) if d < 3), default=None)
    keep = [g for g, d in zip(rows, sep) if d >= 3]
    nb = SkyCoord([g["ra"] for g in keep] * u.deg, [g["dec"] for g in keep] * u.deg,
                  pm_ra_cosdec=[g["pmra"] for g in keep] * u.mas / u.yr, pm_dec=[g["pmdec"] for g in keep] * u.mas / u.yr,
                  obstime=Time(2016.0, format="jyear"))
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    pts = spectrum_points(cuts, tgt, epochs=ep, neighbors=nb)["points"]
    saturated = sum(p["saturated"] for p in pts) / max(1, len(pts)) > 0.5
    ctl = select_controls(rows, (c.ra.deg, c.dec.deg), target_g, saturated=saturated)
    tv = variability(binned_series(pts))
    cv = [variability(binned_series(spectrum_points(cuts, SkyCoord(r["ra"], r["dec"], unit="deg"), epochs=ep)["points"]))
          for r in ctl]
    bands = {}
    for b in sorted(tv):
        cr = [v[b]["ratio"] for v in cv if b in v]
        bands[b] = {"target_ratio": tv[b]["ratio"], "n_bins": tv[b]["n_bins"], "n_controls": len(cr),
                    "control_max": max(cr) if cr else None, "control_median": round(float(np.median(cr)), 3) if cr else None,
                    "variable": is_variable(tv[b]["ratio"], cr), "weak": tv[b]["n_bins"] < 2}
    res = {"target": KEY, "method": VARIABILITY_METHOD, "target_g": target_g, "saturated": saturated,
           "photometry": "psf_deblended", "n_controls": len(ctl),
           "controls": [{"ra": round(r["ra"], 5), "dec": round(r["dec"], 5), "g_mag": round(r["g_mag"], 2)} for r in ctl],
           "bands": bands}
    OUT.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def test_rw_aur_variable():
    res = run()
    print(f"{res['target']}: {res['n_controls']} controls")
    for b, d in res["bands"].items():
        print(f"  {b}: target x{d['target_ratio']} ({d['n_bins']} bins) | controls n={d['n_controls']} max x{d['control_max']} "
              f"median x{d['control_median']} -> variable={d['variable']}{' (weak: one bin)' if d['weak'] else ''}")
    assert res["n_controls"] >= 5
    strong = [d for d in res["bands"].values() if not d["weak"]]
    assert sum(d["variable"] is True for d in strong) >= 2


if __name__ == "__main__":
    test_rw_aur_variable()
