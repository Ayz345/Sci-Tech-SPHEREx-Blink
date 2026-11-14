"""Regression test for saturated cores (no "no data" ring around bright stars).

Run:  python tests/test_coadd_saturation.py      (all five gallery targets, all six detector bands)
Result: saturation_check_v2.json (Default run writes to cache/test_results/; `--write` updates the tracked file (tests/resultio.py).) as {"spx_code_hash": spx.code_hash(), "frames": [...]}, so a check can
tell whether it was made by the same spx code as the gallery (provenance.json carries the same field).

Rules checked inside 36" of the target, on grid pixels that have coverage (flag_frac finite):
  1. NaN count = 0: NaN only ever means "no exposure covers this pixel".
  2. barnard, r-dra: the target pixel is +inf (saturated) in every band where > 15% of the exposures are truly
     saturated at the target (early overflow / NONLINEAR); spx.coadd uses the same 15% rule.
  3. rw-aur and any finite core: PSF-like, the brightest finite pixel within 3 px lies within 2 px (6", less than
     half the ~10" FWHM) of the target; rw-aur cores are finite where <= 15% of exposures are truly saturated.
The fraction of exposures truly saturated at the target is reported per band and epoch (it decides 2 and 3).
  4. The +inf rule as applied per GRID pixel (spx.coadd recomputed here with the same steps): grid_sat_frac =
     exposures whose truly-saturated pixels touch the grid pixel (bilinear weight > 0.25) / overlapping exposures.
     Inside 36": every +inf pixel has grid_sat_frac > 0.15 or no kept sample (weight 0); every finite pixel has
     grid_sat_frac <= 0.15. sat_fraction_at_target (rule 2/3) is per exposure at the nearest DETECTOR pixel and is a
     different quantity; the JSON gives both. The fraction branch needs >= 2 saturated exposures.
  6. No "no data" rim around saturated cores: over the WHOLE frame, NaN pixels with coverage within 2 px of a +inf
     pixel = 0 (nan_next_to_inf), in addition to the fixed 36" NaN check of rule 1.
  5. No false "saturated" claim in empty sky (scripts/acceptance.py check_empty_sky_inf, plus a 1-pixel gap
     tolerance): +inf pixels are grouped 8-connected after a 1-pixel dilation, so a saturated region broken by a
     single row of recovered (finite) pixels stays one region (R Dra D1 epoch 1: islands at 20" from the target);
     every group must contain a pixel within 12" of the target track or of a Gaia DR3 source with G < 12
     (inf_regions_unattached = 0). no_sample_inf_max_dist_arcsec = largest
     distance from a no-sample +inf pixel to a persistent overflow core (OVERFLOW in >= 2 exposures; rule <= 21").
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
import astropy.units as u
import pyvo
from astropy.coordinates import SkyCoord
from scipy.ndimage import binary_dilation, distance_transform_edt, label, map_coordinates

from spx.coadd import build_frames, half_px_for, prepare, saturated_pixels, target_at
from spx.targets import TARGETS, target_coord

from resultio import write_json

OUT_NAME = "saturation_check_v2.json"
R_ARCSEC = 36.0


def sat_fraction(cuts, tgt):
    """Fraction of exposures whose pixel at the target is truly saturated."""
    out = []
    for c in cuts:
        x, y = [int(round(float(v))) for v in c.wcs.world_to_pixel(target_at(tgt, c.mjd))]
        if 0 <= y < c.flags.shape[0] and 0 <= x < c.flags.shape[1]:
            out.append(bool(saturated_pixels(c.flags[y, x])))
    return float(np.mean(out)) if out else 0.0


def grid_sat_fraction(frame, cuts):
    """Per grid pixel: (n truly-saturated exposures, n overlapping exposures, any-exposure OVERFLOW map),
    recomputed like build_frames."""
    yy, xx = np.mgrid[0:frame.grid.shape[0], 0:frame.grid.shape[1]]
    sky = frame.grid.wcs.pixel_to_world(xx, yy)
    nsat = np.zeros(frame.grid.shape)
    nov = np.zeros(frame.grid.shape)
    novf = np.zeros(frame.grid.shape)
    for c in cuts:
        p = prepare(c)
        if p is None:
            continue
        px, py = c.wcs.world_to_pixel(sky)
        ny, nx = p[0].shape
        nov += (px >= 0) & (py >= 0) & (px <= nx - 1) & (py <= ny - 1)
        nsat += map_coordinates(p[3], [py, px], order=1, cval=0.0, prefilter=False) > 0.25
        o = ((np.asarray(c.flags) & (1 << 1)) != 0).astype(float)  # OVERFLOW
        novf += map_coordinates(o, [py, px], order=1, cval=0.0, prefilter=False) > 0.25
    return nsat, nov, novf >= 2


def bright_gaia(center, radius_deg=0.1, gmax=12.0):
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{center.ra.deg},{center.dec.deg},{radius_deg})) "
        f"and phot_g_mean_mag < {gmax}").to_table()
    return SkyCoord(np.array(t["ra"]) * u.deg, np.array(t["dec"]) * u.deg) if len(t) else None


def run():
    rows = []
    for key in TARGETS:
        tgt = target_coord(key)
        c = target_at(tgt, 61000.0)
        refs = access.find_images(c.ra.deg, c.dec.deg)
        _, ep = access.epochs_for(refs)
        cuts, _ = access.fetch_cutouts(refs, c.ra.deg, c.dec.deg, half_px=half_px_for(7.0))
        bright = bright_gaia(c)
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
                ns, no, ovf = grid_sat_fraction(f, [x for x in dc if x.obs_id in ids])
                gfrac = np.where(no > 0, ns / np.maximum(no, 1), 0.0)
                byfrac = (gfrac > 0.15) & (ns >= 2)
                inf_m, fin_m = near & np.isposinf(f.image), near & np.isfinite(f.image)
                viol_inf = inf_m & ~(byfrac | (f.weight == 0))
                viol_fin = fin_m & byfrac
                allinf = np.isposinf(f.image)
                farmask = np.hypot(xx - tx, yy - ty) * f.grid.pix_arcsec > 30
                if bright is not None:
                    bx, by = f.grid.wcs.world_to_pixel(bright)
                    for x_, y_ in zip(np.atleast_1d(bx), np.atleast_1d(by)):
                        farmask &= np.hypot(xx - x_, yy - y_) * f.grid.pix_arcsec > 30
                attach = np.hypot(xx - tx, yy - ty) * f.grid.pix_arcsec <= 12
                if bright is not None:
                    for x_, y_ in zip(np.atleast_1d(bx), np.atleast_1d(by)):
                        attach |= np.hypot(xx - x_, yy - y_) * f.grid.pix_arcsec <= 12
                eight = np.ones((3, 3), bool)
                grouped = binary_dilation(allinf, iterations=1, structure=eight) if allinf.any() else allinf
                lab, nreg = label(grouped, structure=eight)
                lab = np.where(allinf, lab, 0)
                unattached = [k for k in range(1, nreg + 1) if (lab == k).any() and not attach[lab == k].any()]
                slab, sn = label(allinf, structure=eight)  # strict: no gap tolerance (information only)
                strict = [k for k in range(1, sn + 1) if not attach[slab == k].any()]
                nosamp = allinf & (f.weight == 0)
                covered_all = np.isfinite(f.flag_frac)
                near_inf = (distance_transform_edt(~allinf) <= 2) if allinf.any() else np.zeros_like(allinf)
                nan_rim = covered_all & np.isnan(f.image) & near_inf
                dist = distance_transform_edt(~ovf) * f.grid.pix_arcsec if ovf.any() else np.full(ovf.shape, np.inf)
                rows.append({"target": key, "band": f.band, "epoch": f.epoch, "date": f.epoch_label,
                             "grid_sat_frac_at_target": round(float(gfrac[yi, xi]), 3),
                             "inf_px_by_fraction": int((inf_m & byfrac).sum()),
                             "inf_px_no_sample": int((inf_m & ~byfrac & (f.weight == 0)).sum()),
                             "inf_far_from_target_and_bright_stars": int((allinf & farmask).sum()),
                             "inf_regions_unattached": len(unattached),
                             "nan_next_to_inf": int(nan_rim.sum()),
                             "inf_px_unattached": int(np.isin(lab, unattached).sum()) if unattached else 0,
                             "inf_px_unattached_strict": int(np.isin(slab, strict).sum()) if strict else 0,
                             "no_sample_inf_max_dist_arcsec": round(float(dist[nosamp].max()), 1) if nosamp.any() else None,
                             "inf_min_grid_sat_frac": round(float(gfrac[inf_m].min()), 3) if inf_m.any() else None,
                             "finite_max_grid_sat_frac": round(float(gfrac[fin_m].max()), 3) if fin_m.any() else None,
                             "rule_violations": int(viol_inf.sum() + viol_fin.sum()),
                             "sat_fraction_at_target": round(sf, 3), "nan_covered": int(np.isnan(f.image[covered]).sum()),
                             "inf_near": int(np.isposinf(f.image[near]).sum()), "covered_px": int(covered.sum()),
                             "core": "inf" if np.isposinf(core) else ("nan" if np.isnan(core) else round(core, 4)),
                             "peak_offset_px": round(peak_off, 2) if peak_off is not None else None})
    from spx import code_hash
    write_json(OUT_NAME, {"spx_code_hash": code_hash(), "frames": rows})
    return rows


def test_saturated_cores():
    rows = run()
    print("target      band ep  sat%  NaN(cov) +inf  core        peak_off")
    for r in rows:
        print(f"{r['target']:11s} {r['band']}  {r['epoch']}  {100 * r['sat_fraction_at_target']:4.0f}  {r['nan_covered']:7d} {r['inf_near']:5d}  "
              f"{str(r['core']):10s}  {r['peak_offset_px']}")
    print("grid-level rule (inside 36\"): rows with violations:",
          [(r["target"], r["band"], r["epoch"], r["rule_violations"]) for r in rows if r["rule_violations"]])
    bad = [r for r in rows if r["nan_covered"] > 0]
    assert not bad, f"NaN inside covered area: {[(r['target'], r['band'], r['epoch'], r['nan_covered']) for r in bad]}"
    print("strict (no gap tolerance, information only):",
          [(r["target"], r["band"], r["epoch"], r["inf_px_unattached_strict"]) for r in rows if r["inf_px_unattached_strict"]])
    print("+inf regions not attached to the target or a G<12 star:",
          [(r["target"], r["band"], r["epoch"], r["inf_px_unattached"]) for r in rows if r["inf_regions_unattached"]])
    print("+inf pixels beyond 30 arcsec (attached to the target / bright star):",
          [(r["target"], r["band"], r["epoch"], r["inf_far_from_target_and_bright_stars"]) for r in rows if r["inf_far_from_target_and_bright_stars"]])
    md = [r["no_sample_inf_max_dist_arcsec"] for r in rows if r["no_sample_inf_max_dist_arcsec"] is not None]
    print("no-sample +inf: largest distance to a persistent overflow core:", max(md) if md else None, "arcsec")
    assert not any(r["rule_violations"] for r in rows)
    print("NaN within 2 px of +inf (whole frame):", [(r["target"], r["band"], r["epoch"], r["nan_next_to_inf"]) for r in rows if r["nan_next_to_inf"]])
    assert not any(r["inf_regions_unattached"] for r in rows)
    assert not any(r["nan_next_to_inf"] for r in rows)
    for r in rows:
        if r["target"] in ("barnard", "r-dra") and r["sat_fraction_at_target"] > 0.15:
            assert r["core"] == "inf", r
        if r["target"] == "rw-aur" and r["sat_fraction_at_target"] <= 0.15:
            assert r["core"] not in ("inf", "nan"), r
        if r["core"] not in ("inf", "nan") and r["target"] in ("barnard", "rw-aur", "r-dra"):
            assert r["peak_offset_px"] is not None and r["peak_offset_px"] <= 2.0, r


if __name__ == "__main__":
    test_saturated_cores()
