"""RW Aurigae variability against control stars, as defined in spx.photometry.VARIABILITY_METHOD.

Run:  python tests/test_coadd_variability.py
Writes handoff/results/variability_v2.json.

Controls: select_controls() on Gaia DR3 (G < 19) around the target, measured with the same spectrum_points ->
binned_series -> variability chain in the same cutouts. A band is variable when >= 5 controls have a ratio, the
target ratio > 1.15 and > the largest control ratio (is_variable). Bands resting on a single wavelength bin are
marked weak (n_bins = 1).
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
import pyvo
from astropy.coordinates import SkyCoord

from spx import access
from spx.coadd import half_px_for, target_at
from spx.photometry import VARIABILITY_METHOD, binned_series, is_variable, select_controls, spectrum_points, variability
from spx.targets import target_coord

OUT = ROOT / "handoff" / "results" / "variability_v2.json"
KEY, TARGET_G = "rw-aur", 12.18


def run():
    tgt = target_coord(KEY)
    c = target_at(tgt, 61000.0)
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec, phot_g_mean_mag as g_mag from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{c.ra.deg},{c.dec.deg},0.09)) and phot_g_mean_mag < 19").to_table()
    rows = [{"ra": float(r["ra"]), "dec": float(r["dec"]), "g_mag": float(r["g_mag"])} for r in t]
    ctl = select_controls(rows, (c.ra.deg, c.dec.deg), TARGET_G, saturated=True)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    tv = variability(binned_series(spectrum_points(cuts, tgt, epochs=ep)["points"]))
    cv = [variability(binned_series(spectrum_points(cuts, SkyCoord(r["ra"], r["dec"], unit="deg"), epochs=ep)["points"]))
          for r in ctl]
    bands = {}
    for b in sorted(tv):
        cr = [v[b]["ratio"] for v in cv if b in v]
        bands[b] = {"target_ratio": tv[b]["ratio"], "n_bins": tv[b]["n_bins"], "n_controls": len(cr),
                    "control_max": max(cr) if cr else None, "control_median": round(float(np.median(cr)), 3) if cr else None,
                    "variable": is_variable(tv[b]["ratio"], cr), "weak": tv[b]["n_bins"] < 2}
    res = {"target": KEY, "method": VARIABILITY_METHOD, "n_controls": len(ctl),
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
