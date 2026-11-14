"""Reproduce the SPHEREx detection of WISE 0855 and its proper motion, with a null test.

Run:  python tests/test_coadd_wise0855.py        (or: python -m pytest tests/test_coadd_wise0855.py -s)
Needs network on the first run (IRSA SIA2 + public S3); later runs read cache/.
Result: wise0855_detection_v2.json, and prints the table.
Default run writes to cache/test_results/; `--write` updates the tracked file (tests/resultio.py).

Method (one line each):
  frames   spx.coadd.build_frames, band W = exposures whose LVF wavelength at the target is 4.3-5.0 um,
           3" grid, epochs = split_epochs over ALL exposures of the field (max_epoch_days=60).
  PSF      Gaussian, FWHM measured on the brightest isolated stars of the same W frames (measure_fwhm).
  S/N      matched filter (convolution with that PSF); noise = robust std of the matched-filtered
           half-split difference (A-B)/2, A/B = alternate exposures of the epoch in time order, so the
           noise estimate contains no sky signal; S/N = max of the S/N map within 6" of the prediction.
  position PSF fit of the mover + a second PSF at the FIXED Gaia DR3 position of the blended star
           5756956644387831296 (G=16.3) + constant; 1-sigma errors from the fit Jacobian with the
           half-split per-pixel noise. Then a weighted linear fit of position vs time.
  astrom   reference: the same PSF fit on Gaia DR3 stars (G < 15, propagated to the epoch) in the same frames;
           their median offset is the frame's systematic astrometric error.
  pm err   formal errors are scaled by sqrt(chi2/dof) when chi2/dof > 1 (dof = n_epochs - 2).
  fluxes   linear two-PSF fit with BOTH positions fixed (mover = its fitted position, star = Gaia) in band W
           and in D2 (1.1-1.66 um); Gaussian total flux in mJy. Compared with the WISE W2 flux ratio of the
           same two sources (AllWISE W2 13.704 for WISE 0855 at its 2010 position; CatWISE2020 W2 15.202 /
           AllWISE 15.051 for the star). In D2 the mover must be consistent with zero.
  null     the same S/N measurement (max within 6") at 3000 random positions per epoch whose disc avoids
           any emission above 3 sigma in the matched-filtered frame; plus the S/N AT 3000 random empty
           positions, whose robust width w0 (1.4826 MAD) calibrates the scale:
           snr_calibrated = snr / w0. Quote that, or "x times the highest of 3000 empty positions".
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

from spx import access
from spx.coadd import build_frames, epoch_table, half_px_for, split_epochs, target_at
from spx.photometry import (fit_motion, fit_psf_pair, gauss_flux_mjy, matched_filter, measure_fwhm, null_test,
                            psf_amplitudes, snr_map, source_mask)
from spx.targets import TARGETS, target_coord

KEY = "wise-0855"
BAND_UM = (4.3, 5.0)
FOV = 7.0
PIX = 3.0
SEARCH_ARCSEC = 6.0
BLEND_STAR = SkyCoord(133.75818250493919 * u.deg, -7.241155906974705 * u.deg,
                      pm_ra_cosdec=-6.188 * u.mas / u.yr, pm_dec=1.931 * u.mas / u.yr, obstime=Time(2016.0, format="jyear"))
LIT_PM = (-8.118, 0.680)  # Luhman & Esplin 2016, arcsec/yr
W2_ZP_JY = 171.787  # WISE W2 Vega zero point
W2_MAG = {"wise0855": (13.704, 0.033, "AllWISE J085510.74-071442.5"),
          "star_catwise": (15.202, 0.032, "CatWISE2020 J085501.95-071428.2"),
          "star_allwise": (15.051, 0.075, "AllWISE J085501.95-071428.1")}
from resultio import write_json

OUT_NAME = "wise0855_detection_v2.json"


def gaia_stars(center, radius_deg, gmax=15.0):
    import pyvo
    t = pyvo.dal.TAPService("https://gea.esac.esa.int/tap-server/tap").run_sync(
        "select ra, dec, pmra, pmdec, phot_g_mean_mag from gaiadr3.gaia_source where "
        f"1=CONTAINS(POINT('ICRS',ra,dec), CIRCLE('ICRS',{center.ra.deg},{center.dec.deg},{radius_deg})) "
        f"and phot_g_mean_mag < {gmax} and pmra is not null").to_table()
    return SkyCoord(np.array(t["ra"]) * u.deg, np.array(t["dec"]) * u.deg, pm_ra_cosdec=np.array(t["pmra"]) * u.mas / u.yr,
                    pm_dec=np.array(t["pmdec"]) * u.mas / u.yr, obstime=Time(2016.0, format="jyear"))


def astrometric_offsets(f, stars, fwhm_px, min_sep_arcsec=20.0):
    """Median (dRA*cos dec, dDec) in arcsec of fitted minus Gaia positions for isolated stars in frame f."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        now = stars.apply_space_motion(new_obstime=Time(f.mjd, format="mjd"))
    x, y = f.grid.wcs.world_to_pixel(now)
    ny, nx = f.image.shape
    d = []
    for i in range(len(x)):
        if not (8 <= x[i] < nx - 8 and 8 <= y[i] < ny - 8):
            continue
        sep = np.hypot(x - x[i], y - y[i]) * PIX
        if np.sort(sep)[1] < min_sep_arcsec if len(sep) > 1 else False:
            continue
        r = fit_psf_pair(f.image, (float(x[i]), float(y[i])), None, fwhm_px=fwhm_px, box_px=5)
        if not (abs(r["x"] - x[i]) < 3 and abs(r["y"] - y[i]) < 3) or r["amp"] <= 0:
            continue
        c = f.grid.wcs.pixel_to_world(r["x"], r["y"])
        d.append(((c.ra.deg - now[i].ra.deg) * np.cos(np.radians(c.dec.deg)) * 3600, (c.dec.deg - now[i].dec.deg) * 3600))
    d = np.array(d)
    if not len(d):
        return None
    return {"n": len(d), "dra": float(np.median(d[:, 0])), "ddec": float(np.median(d[:, 1])),
            "scatter": float(1.4826 * np.median(np.abs(d - np.median(d, axis=0))))}


def spec_summary(cuts, tgt, ep, center):
    """Per-exposure spectrum at the target, aperture vs PSF-deblended against Gaia neighbours (G < 19)."""
    from spx.photometry import spectrum_points
    nb = gaia_stars(center, 0.07, gmax=19.0)
    out = {}
    for label, kw in (("aperture", {}), ("psf_deblended", {"neighbors": nb})):
        P = [p for p in spectrum_points(cuts, tgt, epochs=ep, **kw)["points"] if not p["flagged"]]
        rows = []
        for lo, hi in ((0.75, 1.1), (1.1, 1.65), (1.65, 2.4), (2.4, 3.8), (3.8, 4.3), (4.3, 5.0)):
            q = [p for p in P if lo <= p["wavelength_um"] < hi]
            f = np.array([p["flux"] for p in q]); w = 1 / np.array([p["err"] for p in q]) ** 2
            m = float(np.sum(w * f) / w.sum())
            rows.append({"um": [lo, hi], "n": len(q), "flux_mjy": round(m, 4), "err": round(float(1 / np.sqrt(w.sum())), 4),
                         "chi2_dof": round(float(np.sum(w * (f - m) ** 2) / max(1, len(q) - 1)), 2),
                         "neighbor_flux_mjy": round(float(np.median([p["neighbor_flux"] for p in q])), 4) if "neighbor_flux" in q[0] else None})
        out[label] = rows
    return out


def run():
    tgt = target_coord(KEY)
    center = target_at(tgt, 61000.0)
    refs = access.find_images(center.ra.deg, center.dec.deg)
    ep = epoch_table([r.t_min for r in refs], split_epochs([r.t_min for r in refs], max_epoch_days=60))
    cuts, nskip = access.fetch_cutouts(refs, center.ra.deg, center.dec.deg, half_px=half_px_for(FOV))
    kw = dict(fov_arcmin=FOV, pix_arcsec=PIX, epochs=ep, center=center, band_um=BAND_UM, band_id="W")
    frames = build_frames(cuts, tgt, **kw)

    # half-split: alternate exposures (time order) of each epoch inside the band
    inband = [c for c in cuts if BAND_UM[0] <= c.wavelength_um <= BAND_UM[1]]
    halves = ([], [])
    for e in ep:
        sel = sorted([c for c in inband if e["mjd_min"] - 0.5 <= c.mjd <= e["mjd_max"] + 0.5], key=lambda c: c.mjd)
        for j, c in enumerate(sel):
            halves[j % 2].append(c)
    fa = {f.epoch: f for f in build_frames(halves[0], tgt, min_exposures=2, **kw)}
    fb = {f.epoch: f for f in build_frames(halves[1], tgt, min_exposures=2, **kw)}

    fwhm_px, fwhms = measure_fwhm(np.nanmean([f.image for f in frames], axis=0))
    stars = gaia_stars(center, FOV / 60 / 2 * 1.3)
    d2 = {f.epoch: f for f in build_frames([c for c in cuts if c.detector == 2], tgt, fov_arcmin=FOV, pix_arcsec=PIX,
                                           epochs=ep, center=center)}
    fwhm_d2 = measure_fwhm(np.nanmean([np.where(np.isfinite(f.image), f.image, np.nan) for f in d2.values()], axis=0))[0]
    rows, nulls = [], {}
    for f in frames:
        noise_img = (fa[f.epoch].image - fb[f.epoch].image) / 2 if f.epoch in fa and f.epoch in fb else None
        if noise_img is not None:  # saturated (+inf) pixels in a half carry no noise information
            noise_img = np.where(np.isfinite(noise_img), noise_img, np.nan)
        snr, sig = snr_map(f.image, fwhm_px, noise_img)
        tx, ty = f.target_xy
        yy, xx = np.mgrid[0:snr.shape[0], 0:snr.shape[1]]
        near = np.hypot(xx - tx, yy - ty) <= SEARCH_ARCSEC / PIX
        j = np.nanargmax(np.where(near, snr, -np.inf))
        peak_snr, px, py = float(snr.flat[j]), float(xx.flat[j]), float(yy.flat[j])
        pix_noise = float(np.nanstd(noise_img)) if noise_img is not None else None
        sx, sy = f.grid.wcs.world_to_pixel(target_at(BLEND_STAR, f.mjd))
        fit = fit_psf_pair(f.image, (px, py), (float(sx), float(sy)), fwhm_px=fwhm_px, noise=pix_noise)
        pos = f.grid.wcs.pixel_to_world(fit["x"], fit["y"])
        pair = [(fit["x"], fit["y"]), (float(sx), float(sy))]
        a, ae, _ = psf_amplitudes(f.image, pair, fwhm_px, noise=pix_noise)
        fl, fle = gauss_flux_mjy(a, fwhm_px, PIX), gauss_flux_mjy(ae, fwhm_px, PIX)
        g = d2.get(f.epoch)
        if g is not None:
            b, be, _ = psf_amplitudes(np.where(np.isfinite(g.image), g.image, np.nan), pair, fwhm_d2)
            fd, fde = gauss_flux_mjy(b, fwhm_d2, PIX), gauss_flux_mjy(be, fwhm_d2, PIX)
        pred = SkyCoord(*f.target_radec, unit="deg")
        mask = source_mask(matched_filter(f.image, fwhm_px))
        nl = null_test(snr, mask, n=3000, search_px=SEARCH_ARCSEC / PIX, seed=f.epoch)
        nulls[f.epoch] = nl
        pt = null_test(snr, mask, n=3000, search_px=0, seed=100 + f.epoch)
        w0 = float(1.4826 * np.median(np.abs(pt - np.median(pt))))
        ast = astrometric_offsets(f, stars, fwhm_px)
        rows.append({
            "epoch": f.epoch, "date": f.epoch_label, "mjd": round(f.mjd, 4), "n_exposures": f.n_used,
            "snr": round(peak_snr, 2), "snr_calibrated": round(peak_snr / w0, 2), "null_point_width": round(w0, 3),
            "snr_over_null_max": round(peak_snr / float(nl.max()), 2), "filtered_noise_mjy_sr": sig,
            "ra": round(pos.ra.deg, 6), "dec": round(pos.dec.deg, 6),
            "err_ra_arcsec": round(fit["sx"] * PIX, 3), "err_dec_arcsec": round(fit["sy"] * PIX, 3),
            "offset_from_prediction_arcsec": round(pos.separation(pred).arcsec, 2),
            "pred_ra": round(pred.ra.deg, 6), "pred_dec": round(pred.dec.deg, 6),
            "sep_to_blend_star_arcsec": round(pos.separation(target_at(BLEND_STAR, f.mjd)).arcsec, 2),
            "flux_W_mover_mjy": round(float(fl[0]), 4), "flux_W_mover_err": round(float(fle[0]), 4),
            "flux_W_star_mjy": round(float(fl[1]), 4), "flux_W_star_err": round(float(fle[1]), 4),
            "flux_D2_mover_mjy": round(float(fd[0]), 4) if g is not None else None,
            "flux_D2_mover_err": round(float(fde[0]), 4) if g is not None else None,
            "flux_D2_star_mjy": round(float(fd[1]), 4) if g is not None else None,
            "flux_D2_star_err": round(float(fde[1]), 4) if g is not None else None,
            "gaia_ref": {k: (round(v, 3) if isinstance(v, float) else v) for k, v in ast.items()} if ast else None,
            "null_n": int(nl.size), "null_median": round(float(np.median(nl)), 2),
            "null_p99_9": round(float(np.percentile(nl, 99.9)), 2), "null_max": round(float(nl.max()), 2),
            "null_frac_ge_5": float(np.mean(nl >= 5)), "null_frac_ge_detection": float(np.mean(nl >= peak_snr)),
        })
    mot = fit_motion([r["mjd"] for r in rows], [r["ra"] for r in rows], [r["dec"] for r in rows],
                     [r["err_ra_arcsec"] for r in rows], [r["err_dec_arcsec"] for r in rows])
    w2 = {k: W2_ZP_JY * 10 ** (-0.4 * m) * 1e3 for k, (m, _, _) in W2_MAG.items()}  # mJy
    fm = np.array([r["flux_W_mover_mjy"] for r in rows]); fme = np.array([r["flux_W_mover_err"] for r in rows])
    fs = np.array([r["flux_W_star_mjy"] for r in rows]); fse = np.array([r["flux_W_star_err"] for r in rows])
    wm, ws = 1 / fme ** 2, 1 / fse ** 2
    flux_check = {
        "W2_catalog": {k: {"mag": m, "err": e, "source": src, "flux_mjy": round(w2[k], 4)} for k, (m, e, src) in W2_MAG.items()},
        "W2_ratio_mover_over_star": [round(w2["wise0855"] / w2["star_catwise"], 2), round(w2["wise0855"] / w2["star_allwise"], 2)],
        "W_mover_mean_mjy": round(float(np.sum(wm * fm) / wm.sum()), 4), "W_mover_chi2": round(float(np.sum(wm * (fm - np.sum(wm * fm) / wm.sum()) ** 2)), 2),
        "W_star_mean_mjy": round(float(np.sum(ws * fs) / ws.sum()), 4), "W_star_chi2": round(float(np.sum(ws * (fs - np.sum(ws * fs) / ws.sum()) ** 2)), 2),
        "fwhm_W_arcsec": round(fwhm_px * PIX, 2), "fwhm_D2_arcsec": round(fwhm_d2 * PIX, 2),
    }
    flux_check["W_ratio_mover_over_star"] = round(flux_check["W_mover_mean_mjy"] / flux_check["W_star_mean_mjy"], 2)
    dof = max(1, len(rows) - 2)
    for k in ("pmra", "pmdec"):
        mot[k + "_err_scaled"] = mot[k + "_err"] * max(1.0, np.sqrt(mot[k + "_chi2"] / dof))
    res = {"target": KEY, "band_um": BAND_UM, "grid_arcsec": PIX, "psf_fwhm_arcsec": round(fwhm_px * PIX, 2),
           "psf_fwhm_stars_arcsec": [round(v * PIX, 2) for v in fwhms], "n_cutouts": len(cuts), "n_skipped": nskip,
           "epochs": rows, "motion_arcsec_per_yr": {k: round(v, 3) for k, v in mot.items()},
           "literature_pm_arcsec_per_yr": LIT_PM, "flux_check": flux_check,
           "spectrum_deblended": spec_summary(cuts, tgt, ep, center),
           "method": __doc__.split("Method (one line each):")[1].strip()}
    write_json(OUT_NAME, res, ensure_ascii=False)
    return res


def report(res):
    print(f"PSF FWHM (co-add, {len(res['psf_fwhm_stars_arcsec'])} stars): {res['psf_fwhm_arcsec']}\"")
    print("date        N   S/N  S/Ncal xNullMax  offset  err_ra err_dec  sep_star | null: median p99.9  max  frac>=5 | Gaia ref n dRA dDec scatter")
    for r in res["epochs"]:
        print(f"{r['date']} {r['n_exposures']:3d} {r['snr']:5.1f} {r['snr_calibrated']:5.1f} {r['snr_over_null_max']:5.1f}  {r['offset_from_prediction_arcsec']:5.2f}\"  "
              f"{r['err_ra_arcsec']:5.2f}  {r['err_dec_arcsec']:5.2f}   {r['sep_to_blend_star_arcsec']:5.2f}\" | "
              f"{r['null_median']:5.2f} {r['null_p99_9']:5.2f} {r['null_max']:5.2f} {r['null_frac_ge_5']:.4f} | "
              + (f"{r['gaia_ref']['n']:2d} {r['gaia_ref']['dra']:+.2f} {r['gaia_ref']['ddec']:+.2f} {r['gaia_ref']['scatter']:.2f}" if r['gaia_ref'] else "-"))
    m = res["motion_arcsec_per_yr"]
    print(f"SPHEREx motion: pmra {m['pmra']:+.2f} +- {m['pmra_err_scaled']:.2f}  pmdec {m['pmdec']:+.2f} +- {m['pmdec_err_scaled']:.2f} \"/yr "
          f"(formal +- {m['pmra_err']:.2f}, {m['pmdec_err']:.2f}; "
          f"chi2 {m['pmra_chi2']:.1f}, {m['pmdec_chi2']:.1f}; literature {LIT_PM[0]:+.3f}, {LIT_PM[1]:+.3f})")
    print("fluxes (mJy)   W mover          W star           D2 mover          D2 star")
    for r in res["epochs"]:
        print(f"  {r['date']}  {r['flux_W_mover_mjy']:6.3f}+-{r['flux_W_mover_err']:.3f}  {r['flux_W_star_mjy']:6.3f}+-{r['flux_W_star_err']:.3f}  "
              f"{r['flux_D2_mover_mjy']:7.3f}+-{r['flux_D2_mover_err']:.3f}  {r['flux_D2_star_mjy']:6.3f}+-{r['flux_D2_star_err']:.3f}")
    fc = res["flux_check"]
    print(f"W mean: mover {fc['W_mover_mean_mjy']} (chi2 {fc['W_mover_chi2']}), star {fc['W_star_mean_mjy']} (chi2 {fc['W_star_chi2']}); "
          f"ratio SPHEREx W {fc['W_ratio_mover_over_star']} vs WISE W2 {fc['W2_ratio_mover_over_star']}; W2 fluxes {[(k, v['flux_mjy']) for k, v in fc['W2_catalog'].items()]}")
    for label, rows in res["spectrum_deblended"].items():
        print(f"spectrum {label:14s} " + "  ".join(f"{r['um'][0]}-{r['um'][1]}:{r['flux_mjy']:+.3f}" for r in rows) + " mJy")


def test_wise0855_detected():
    res = run()
    report(res)
    good = [r for r in res["epochs"] if r["n_exposures"] >= 10]
    assert len(good) >= 2
    for r in good:
        assert r["snr_calibrated"] >= 5, r
        assert r["offset_from_prediction_arcsec"] <= SEARCH_ARCSEC, r
        assert r["null_frac_ge_detection"] < 0.001, r
    fc = res["flux_check"]
    assert 0.5 < fc["W_ratio_mover_over_star"] / fc["W2_ratio_mover_over_star"][0] < 2.0
    for r in res["epochs"]:
        assert abs(r["flux_D2_mover_mjy"]) < 3 * r["flux_D2_mover_err"] + 0.05
    deb = {tuple(r["um"]): r for r in res["spectrum_deblended"]["psf_deblended"]}
    assert deb[(4.3, 5.0)]["flux_mjy"] > 5 * deb[(4.3, 5.0)]["err"]
    for k in ((0.75, 1.1), (1.1, 1.65), (1.65, 2.4)):
        assert abs(deb[k]["flux_mjy"]) < 0.15 * deb[k]["neighbor_flux_mjy"] + 3 * deb[k]["err"]
    m = res["motion_arcsec_per_yr"]
    assert abs(m["pmra"] - LIT_PM[0]) <= 3 * m["pmra_err_scaled"]
    assert abs(m["pmdec"] - LIT_PM[1]) <= 3 * m["pmdec_err_scaled"]


if __name__ == "__main__":
    test_wise0855_detected()
