"""Validate wing_flux (relative brightness from the PSF wings) against aperture photometry before publishing it.

Run:  python tests/test_coadd_wings.py
Writes handoff/results/wing_validation_v2.json.

Condition (orchestrator, 14 Nov): on exposures where BOTH are valid, wing_flux must be proportional to the aperture
flux, i.e. the ratio wing/aperture must not change between epochs. Measured
  - on RW Aurigae D1 (clean or recovered-overflow apertures), and
  - on isolated, unsaturated Gaia stars (G 10-14) in the RW Aur and Eps Indi B fields, D1-D3,
comparing like with like: ratio per (band, wavelength bin, epoch), then max/min over epochs per bin.
Result (14 Nov): NOT proportional at the 10% level. Controls: median max/min 1.20, 90th percentile 1.47;
RW Aur D1 1.26. The wing measurement therefore carries a ~20-50% epoch-to-epoch systematic; the test records the
numbers and the significance threshold (controls p90) and checks that RW Aur D1 behaves like the controls.
It also measures the wing variability of RW Aur and R Dra (binned_series + variability) against that threshold.
spectrum_points(wings=...) stays False by default until publication is approved.
"""
from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
warnings.filterwarnings("ignore")

import astropy.units as u
import numpy as np
from astropy.coordinates import SkyCoord

from spx import access
from spx.coadd import half_px_for, target_at
from spx.photometry import spectrum_points
from spx.targets import target_coord
from test_coadd_wise0855 import gaia_stars

OUT = ROOT / "handoff" / "results" / "wing_validation_v2.json"
NBINS = 6


def ratio_stability(points, bands):
    """Per (band, bin): epoch medians of wing/aperture over points valid in both; max/min across epochs."""
    rows = []
    for b in bands:
        q = [p for p in points if p["band"] == b and not p["flagged"] and "wing_flux" in p
             and p["flux"] > 10 * p["err"] and p["wing_flux"] > 5 * p["wing_err"]]
        if len(q) < 4:
            continue
        wl = np.array([p["wavelength_um"] for p in q])
        edges = np.linspace(wl.min(), wl.max() + 1e-9, NBINS + 1)
        for i in range(NBINS):
            per_ep = {}
            for p in q:
                if edges[i] <= p["wavelength_um"] < edges[i + 1]:
                    per_ep.setdefault(p["epoch"], []).append(p["wing_flux"] / p["flux"])
            if len(per_ep) >= 2:
                med = [float(np.median(v)) for v in per_ep.values()]
                rows.append({"band": b, "wave_um": [round(float(edges[i]), 3), round(float(edges[i + 1]), 3)],
                             "epochs": len(per_ep), "n": sum(len(v) for v in per_ep.values()),
                             "max_over_min": round(max(med) / min(med), 3)})
    return rows


def field_controls(key, n_max=8):
    tgt = target_coord(key)
    c = target_at(tgt, 61000.0)
    refs = access.find_images(c.ra.deg, c.dec.deg)
    _, ep = access.epochs_for(refs)
    cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
    stars = gaia_stars(c, 3.0 / 60, gmax=17.0)
    g_all = stars
    out = []
    import pyvo
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec, pmra, pmdec, phot_g_mean_mag from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{c.ra.deg},{c.dec.deg},0.05)) and phot_g_mean_mag between 10 and 14 "
        "and pmra is not null order by phot_g_mean_mag").to_table()
    for r in t:
        s = SkyCoord(r["ra"] * u.deg, r["dec"] * u.deg)
        if s.separation(c).arcsec < 30:
            continue  # not the target
        sep = SkyCoord(g_all.ra, g_all.dec).separation(s).arcsec
        if np.sum((sep > 0.5) & (sep < 30)) > 0:
            continue  # a G<17 neighbour within 30"
        out.append((float(r["phot_g_mean_mag"]), s))
        if len(out) >= n_max:
            break
    return cuts, ep, out


def run():
    res = {"rw_aur_D1": None, "controls": []}
    # RW Aur itself, D1
    tgt = target_coord("rw-aur")
    cuts, ep, ctrl = field_controls("rw-aur")
    sp = spectrum_points(cuts, tgt, epochs=ep, wings=True)
    res["rw_aur_D1"] = {"wing_annulus_px": sp["meta"]["wing"]["annulus_px"].get("D1"), "bins": ratio_stability(sp["points"], ["D1"])}
    for key, (cs, e, cl) in (("rw-aur", (cuts, ep, ctrl)), ("eps-indi-b", field_controls("eps-indi-b"))):
        for g, s in cl:
            spc = spectrum_points(cs, s, epochs=e, wings=True)
            bins = ratio_stability(spc["points"], ["D1", "D2", "D3"])
            if bins:
                res["controls"].append({"field": key, "ra": round(s.ra.deg, 5), "dec": round(s.dec.deg, 5), "g_mag": round(g, 2),
                                        "annulus_px": spc["meta"]["wing"]["annulus_px"], "bins": bins})
    allc = [b["max_over_min"] for c in res["controls"] for b in c["bins"]]
    rw = [b["max_over_min"] for b in res["rw_aur_D1"]["bins"]]
    res["summary"] = {"controls_n_stars": len(res["controls"]), "controls_n_bins": len(allc),
                      "controls_median_max_over_min": round(float(np.median(allc)), 3) if allc else None,
                      "controls_p90": round(float(np.percentile(allc, 90)), 3) if allc else None,
                      "rw_aur_D1_n_bins": len(rw), "rw_aur_D1_median_max_over_min": round(float(np.median(rw)), 3) if rw else None}
    res["summary"]["significance_threshold"] = res["summary"]["controls_p90"]
    from spx.photometry import binned_series, variability
    res["wing_variability"] = {}
    for key in ("rw-aur", "r-dra"):
        t = target_coord(key)
        c = target_at(t, 61000.0)
        refs = access.find_images(c.ra.deg, c.dec.deg)
        _, e = access.epochs_for(refs)
        cs, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
        pts = spectrum_points(cs, t, epochs=e, wings=True)["points"]
        res["wing_variability"][key] = variability(binned_series(pts, kind="wing"))
    OUT.write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def test_wings_proportional():
    res = run()
    s = res["summary"]
    print(json.dumps(s, indent=1))
    for c in res["controls"]:
        print(f"  control {c['field']:10s} G={c['g_mag']:5.2f}: " + ", ".join(f"{b['band']} {b['wave_um'][0]:.2f} x{b['max_over_min']}" for b in c["bins"]))
    print("  RW Aur D1:", [(b["wave_um"][0], b["max_over_min"], b["n"]) for b in res["rw_aur_D1"]["bins"]])
    print("  wing variability (median over wavelength bins of max/min between epochs):")
    for k, v in res["wing_variability"].items():
        print(f"   {k}: " + ", ".join(f"{b} x{d['ratio']} ({d['n_bins']} bins){' *' if d['ratio'] > s['significance_threshold'] else ''}" for b, d in v.items()))
    print(f"   * = above the significance threshold x{s['significance_threshold']}")
    assert s["controls_n_bins"] >= 5
    assert s["rw_aur_D1_n_bins"] >= 2 and s["rw_aur_D1_median_max_over_min"] <= s["controls_p90"]


if __name__ == "__main__":
    test_wings_proportional()
