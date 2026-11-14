"""Per-exposure photometry (spectrum / light curve) and point-source measurements on co-added frames.

spectrum_points(cutouts, coord)  -> {"meta": {...}, "points": [{wavelength_um, bandwidth_um, flux, err,
                                     mjd, epoch, band, flagged, flags}, ...]}   (data contract section 5; flags: v1.2 F3)
Measurement helpers used by tests/test_coadd_wise0855.py:
  matched_filter, snr_map, centroid, fit_psf_pair, fit_motion, null_test
"""
from __future__ import annotations

import warnings

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.time import Time
from astropy.stats import sigma_clipped_stats
from astropy.wcs.utils import proj_plane_pixel_area
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.optimize import least_squares

from spx.coadd import (BAD_MASK, HARD_MASK, assign_epochs, saturated_pixels, split_epochs, target_at,
                       usable_pixels)
from spx.coadd import FLAG_BITS

_OVF = 1 << FLAG_BITS["OVERFLOW"]

FLUX_UNIT = "mJy"
# aperture 'flagged' rule (v1.2, 14 Nov; tests/test_coadd_flagged_rule.py -> handoff/results/flagged_rule_v2.json):
# below 15% masked aperture weight with a clean core, star fluxes agree with unflagged ones (ratio 1.00-1.04)
FLAG_MASKED_FRAC = 0.15
FLAG_CORE_PX = 1.0
# measured by tests/test_coadd_flagged_rule.py (empty sky, unflagged aperture points, 14 Nov)
ERR_EMPTY_SKY_SCATTER = 1.40
ERR_EMPTY_SKY_SCATTER_PSF = 1.32  # same test, fixed-position PSF photometry
ERR_NOTE = (f"Empty-sky test: the scatter of flux/err is {ERR_EMPTY_SKY_SCATTER:.2f}, so the listed errors are too "
            "small by about that factor.")
ERR_NOTE_PSF = (f"Empty-sky test (PSF photometry): the scatter of flux/err is {ERR_EMPTY_SKY_SCATTER_PSF:.2f}, so the "
                "listed errors are too small by about that factor.")

# --------------------------------------------------------------------------- FLAGS names at a point (F3)

FLAG_LEGEND_SOURCE = ("SPHEREx Explanatory Supplement v2.1 (2026-11-10, SSDC-DP-017), Table 16 'Image pixel flags used "
                      "in spectral images at Level 2': one or two sentences of each Description, verbatim")
#: {FLAGS name: meaning}. Every value is checked against the Table 16 text by tests/test_photometry_flags.py.
FLAG_LEGEND = {
    "TRANSIENT": "Transient (such as cosmic ray hit) detected during SUR.",
    "OVERFLOW": "Overflow threshold reached during SUR. This threshold is set at approximately half the level of "
                "full-well saturation.",
    "SUR_ERROR": "In the absence of other flags, it indicates a data handling error in the SUR algorithm onboard. "
                 "When combined with the TRANSIENT or OVERFLOW flags, signals early transient (first half of exposure) "
                 "or early overflow (first 5 seconds) conditions.",
    "NONFUNC": "Static, initially pre-flight assessment that a pixel is permanently unusable/dead.",
    "DICHROIC": "This flag indicates pixels with low expected throughput that are not usefully calibratable or of "
                "value for photometry.",
    "MISSING_DATA": "Pixels for which the onboard data was lost before ingest by the SSDC, e.g., as corrupted packages "
                    "on the downlink that were not retransmitted successfully, resulting that there is no data "
                    "available for this pixel in this image.",
    "HOT": "Hot pixel - noisy or otherwise affected by spurious signal.",
    "COLD": "Pixel that appear unresponsive to light or otherwise has an anomalously low signal in a particular "
            "exposure; permanently dead pixels are flagged as NONFUNC.",
    "FULLSAMPLE": "Pixel for which the full sample history is available, because of the rotating 8×32 pixel region "
                  "for full readout. Not usable for science, purely informational.",
    "PHANMISS": "Applied for pixels that can not be corrected due to missing phantom pixels.",
    "NONLINEAR": "Pixel for which a reliable nonlinearity correction could not be determined.",
    "PERSIST": "Pixel found to have been affected by persistent charge above the Level 2 threshold for this.",
    "OUTLIER": "Pixel flagged by \"Detect Outlier Pixels\" module (Sect. 3.2.10).",
    "CROSSTALK": "Pixel found to have been affected by \"ordinary\" crosstalk (Sect. 3.2.9).",
    "SOURCE": "Pixel mapped to a known source.",
    "GHOST": "Pixel found to have been affected by an optical ghost from a bright source inside the exposure frame "
             "(Sect. 3.2.9).",
    "GHOST_FPA": "Pixel found to have been affected by an optical ghost from a bright source seen on another detector "
                 "in the FPA. This flag is currently unused (subsumed into GHOST EXT) and will be removed in Year 2 "
                 "processing.",
    "GHOST_EXT": "Pixel found to have been affected by an optical ghost from a bright source outside the field of "
                 "view.",
    "STREAK": "Pixel found to have been affected by a streak detected in the image, presumably from an object in "
              "Earth orbit (Sect.3.2.10).",
    "BLOOM": "Pixel found to have been affected by source blooming (Sect. 3.2.1).",
    "SNOWBALL": "Pixel found to have been affected by snowball events in which very large quantities of electrons are "
                "released (Sect. 3.2.1).",
    "HALO": "Pixel found to have been affected by the halo of a transient. Caution is recommended when using this "
            "pixel in science analysis (Sect. 3.2.1).",
    "SATELLITE_HALO": "Pixel found to have been affected by the halo of a satellite streak. This pixel is not "
                      "recommended for use in science analysis (Sect. 3.2.1).",
}
UNKNOWN_FLAG_MEANING = "bit not listed in Table 16 of the Explanatory Supplement v2.1"
_BIT_NAMES = {b: n for n, b in FLAG_BITS.items()}


def flag_names(flags, region) -> list[str]:
    """Names of every FLAGS bit set in any pixel of `region` (bool mask), informational bits included, in bit
    order; a bit without a Table 16 name is reported as "BIT<n>". [] when no bit is set."""
    if flags is None:
        return []
    v = int(np.bitwise_or.reduce(np.asarray(flags, np.int64)[np.asarray(region, bool)].ravel(), initial=0))
    return [_BIT_NAMES.get(b, f"BIT{b}") for b in range(63) if (v >> b) & 1]


def _flag_order(name):
    if name in FLAG_BITS:
        return FLAG_BITS[name]
    return int(name[3:]) if name.startswith("BIT") and name[3:].isdigit() else 99


def flag_legend(points) -> dict:
    """{name: meaning} for the names that occur in `points` (Table 16; unknown bits get UNKNOWN_FLAG_MEANING)."""
    names = sorted({n for p in points for n in p.get("flags", [])}, key=_flag_order)
    return {n: FLAG_LEGEND.get(n, UNKNOWN_FLAG_MEANING) for n in names}


FLAG_REGION_TEXT = ("flags = names of all FLAGS bits set in any pixel of the measurement region, informational bits "
                    "(SOURCE, FULLSAMPLE) included: aperture photometry = every pixel the aperture touches; PSF "
                    "photometry = pixels within 1.5 px of the target (the region that decides 'flagged'). Cutout pixels "
                    "beyond the detector edge carry MISSING_DATA (set by spx.access)")


# --------------------------------------------------------------------------- per-exposure photometry

def _aperture_weights(shape, x, y, r, oversample=5):
    """Fractional pixel coverage of a circle of radius r (px) centred at (x, y) (0-based)."""
    ny, nx = shape
    x0, x1 = max(0, int(np.floor(x - r)) - 1), min(nx, int(np.ceil(x + r)) + 2)
    y0, y1 = max(0, int(np.floor(y - r)) - 1), min(ny, int(np.ceil(y + r)) + 2)
    w = np.zeros(shape)
    if x1 <= x0 or y1 <= y0:
        return w
    s = (np.arange(oversample) + 0.5) / oversample - 0.5
    yy, xx = np.mgrid[y0:y1, x0:x1]
    sub = ((xx[..., None, None] + s[None, None, None, :] - x) ** 2 +
           (yy[..., None, None] + s[None, None, :, None] - y) ** 2) <= r * r
    w[y0:y1, x0:x1] = sub.mean(axis=(2, 3))
    return w


def aperture_flux(cut, x, y, r_ap=2.0, r_in=4.0, r_out=7.0):
    """Background-subtracted aperture flux on one cutout at pixel (x, y).

    Returns (flux_mJy, err_mJy, flagged, n_bad_in_aperture, saturated, overflow, masked_fraction) or None when
    the aperture is off the cutout. Masked pixels (not coadd.usable_pixels: any non-informational FLAGS bit
    except recovered overflow; or no data) are LEFT OUT and the flux and its error are scaled by the PSF-weighted
    aperture fraction (Gaussian with the exposure's PSF_FWHM), except truly saturated pixels, which are kept (a saturated core gives a lower limit, as before).
    masked_fraction = masked share of the aperture weight. flagged (v1.2, 14 Nov) = masked_fraction > FLAG_MASKED_FRAC,
    or a masked pixel within FLAG_CORE_PX of the centre, or a lower limit. saturated = early overflow or
    NONLINEAR in the aperture (flux is a lower limit); overflow = recovered-overflow pixels in the aperture
    (valid; recovered/clean median 1.04 over 10 pairs, range 1.00-1.51).
    """
    img = np.asarray(cut.image, float)
    flags = np.asarray(cut.flags) if cut.flags is not None else np.zeros(img.shape, np.int32)
    var = np.asarray(cut.variance, float) if getattr(cut, "variance", None) is not None else None
    ny, nx = img.shape
    if not (r_ap <= x <= nx - 1 - r_ap and r_ap <= y <= ny - 1 - r_ap):
        return None
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    good_px = usable_pixels(flags)
    satpx = saturated_pixels(flags)
    usable = np.isfinite(img) & ((flags & HARD_MASK) == 0) & (good_px | satpx)
    clean = np.isfinite(img) & ((flags & BAD_MASK) == 0)
    ann = clean & (rr >= r_in) & (rr <= r_out)
    if ann.sum() < 10:
        return None
    _, bg, bgsig = sigma_clipped_stats(img[ann], sigma=3.0, maxiters=5, stdfunc="mad_std")
    w = _aperture_weights(img.shape, x, y, r_ap)
    wu = np.where(usable, w, 0.0)
    lower_limit = wu.sum() < 0.5 * w.sum()
    if lower_limit and not (satpx & (w > 0)).any() and np.isfinite(img[w > 0]).all():
        return None  # mostly dead/missing pixels, nothing to say
    # saturated core with missing (NaN) pixels: sum what is there, no area scaling -> a lower limit
    # missing pixels: scale by the PSF-weighted aperture fraction (Gaussian, the exposure's PSF_FWHM), not by area:
    # masked pixels sit mostly in the faint wings, and an area scaling over-corrects a point source
    fw = getattr(cut, "psf_fwhm_arcsec", float("nan"))
    sg = ((fw if np.isfinite(fw) else 7.5) / 6.15) / 2.3548
    P = np.exp(-rr ** 2 / (2 * sg * sg))
    scale = 1.0 if lower_limit else float(np.sum(w * P) / np.sum(wu * P))
    s = np.nansum(wu * (img - bg)) * scale
    if var is not None:
        v = np.nansum(wu ** 2 * np.where(np.isfinite(var), var, 0.0))
    else:
        v = (wu ** 2).sum() * bgsig ** 2
    v += (wu.sum() ** 2) * bgsig ** 2 / ann.sum()  # background-estimate error
    v *= scale ** 2
    omega_sr = proj_plane_pixel_area(cut.wcs) * (np.pi / 180) ** 2
    to_mjy = 1e6 * omega_sr * 1e3  # MJy/sr * sr -> Jy -> mJy
    inap = w > 0
    masked = ~(good_px & np.isfinite(img))
    n_bad = int((inap & masked).sum())
    mfrac = float(np.sum(w[masked]) / w.sum())
    sat = bool((inap & satpx).any()) or bool(lower_limit)
    ovf = bool((inap & ((flags & _OVF) != 0) & good_px).any())
    core = bool((inap & masked & (rr <= FLAG_CORE_PX)).any())
    flagged = mfrac > FLAG_MASKED_FRAC or core or bool(lower_limit)
    return float(s * to_mjy), float(np.sqrt(v) * to_mjy), flagged, n_bad, sat, ovf, round(mfrac, 4)


def _pixel_gauss(shape, x, y, fwhm_px):
    """Unit-flux Gaussian integrated over each pixel (correct for the undersampled native SPHEREx PSF)."""
    from scipy.special import erf
    s = fwhm_px / 2.3548 * np.sqrt(2)
    ex = 0.5 * (erf((np.arange(shape[1]) + 0.5 - x) / s) - erf((np.arange(shape[1]) - 0.5 - x) / s))
    ey = 0.5 * (erf((np.arange(shape[0]) + 0.5 - y) / s) - erf((np.arange(shape[0]) - 0.5 - y) / s))
    return np.outer(ey, ex)


def psf_flux(cut, xys, fwhm_px, box_px=6, r_bg=(8.0, 12.0)):
    """Fixed-position PSF photometry on one native cutout: fluxes (mJy) of sources at xys (first = target).

    Pixel-integrated Gaussian PSF, local background from a clean annulus around the target, BAD_MASK
    pixels excluded (recovered overflow kept). Returns (flux_mJy array, err_mJy array, flagged, saturated,
    overflow) or None when the target core cannot be fitted (e.g. saturated): callers fall back to the aperture.
    """
    img = np.asarray(cut.image, float)
    flags = np.asarray(cut.flags) if cut.flags is not None else np.zeros(img.shape, np.int32)
    x0, y0 = xys[0]
    ny, nx = img.shape
    if not (box_px <= x0 <= nx - 1 - box_px and box_px <= y0 <= ny - 1 - box_px):
        return None
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x0, yy - y0)
    clean = np.isfinite(img) & ((flags & BAD_MASK) == 0)
    ann = clean & (rr >= r_bg[0]) & (rr <= r_bg[1])
    if ann.sum() < 10:
        return None
    _, bg, bgsig = sigma_clipped_stats(img[ann], sigma=3.0, maxiters=5, stdfunc="mad_std")
    box = (np.abs(xx - x0) <= box_px) & (np.abs(yy - y0) <= box_px)
    use = box & np.isfinite(img) & usable_pixels(flags)
    near = rr <= 1.5
    if use[near].sum() < 0.5 * near.sum():
        return None
    A = np.column_stack([_pixel_gauss(img.shape, x, y, fwhm_px)[use] for x, y in xys])
    Z = img[use] - bg
    var = np.asarray(cut.variance, float)[use] if getattr(cut, "variance", None) is not None else np.full(Z.size, bgsig ** 2)
    var = np.where(np.isfinite(var) & (var > 0), var, bgsig ** 2)
    w = 1 / var
    cov = np.linalg.inv(A.T @ (A * w[:, None]))
    p = cov @ (A.T @ (w * Z))
    omega_sr = proj_plane_pixel_area(cut.wcs) * (np.pi / 180) ** 2
    to_mjy = 1e6 * omega_sr * 1e3
    flagged = bool((~usable_pixels(flags))[near].any())
    sat = bool(saturated_pixels(flags)[rr <= 2.0].any())
    ovf = bool((((flags & _OVF) != 0) & usable_pixels(flags))[rr <= 2.0].any())
    return p * to_mjy, np.sqrt(np.diag(cov)) * to_mjy, flagged, sat, ovf


def _saturated_radius(cut, x, y, search_px=3.0):
    """Radius (px) of the target's own contiguous saturated region (early overflow / NONLINEAR / NaN core)."""
    from scipy.ndimage import binary_dilation, label
    img = np.asarray(cut.image, float)
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    rr = np.hypot(xx - x, yy - y)
    sat = saturated_pixels(cut.flags) | (~np.isfinite(img) & (rr < 25))
    lab, _ = label(binary_dilation(sat, iterations=1))
    ids = np.unique(lab[(rr <= search_px) & (lab > 0)])
    own = np.isin(lab, ids) & sat if len(ids) else np.zeros_like(sat)
    return float(rr[own].max()) if own.any() else 0.0


def wing_flux(cut, x, y, r_in, r_out, bg_gap=4.0, bg_width=6.0):
    """Mean surface brightness (MJy/sr) of usable pixels in the annulus r_in..r_out around (x, y), minus the local
    background from r_out+bg_gap .. r_out+bg_gap+bg_width. RELATIVE brightness of a saturated star from its PSF
    wings (not a total flux). Returns (value, err) or None (annulus off the cutout or < 60 % usable)."""
    img = np.asarray(cut.image, float)
    flags = np.asarray(cut.flags)
    ny, nx = img.shape
    rb = r_out + bg_gap + bg_width
    if not (rb <= x <= nx - 1 - rb and rb <= y <= ny - 1 - rb):
        return None
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    ok = np.isfinite(img) & usable_pixels(flags)
    ann = (rr >= r_in) & (rr < r_out)
    a = ann & ok
    if a.sum() < 0.6 * ann.sum():
        return None
    bgm = ok & ((flags & BAD_MASK) == 0) & (rr >= r_out + bg_gap) & (rr < rb)
    if bgm.sum() < 20:
        return None
    _, bg, bgsig = sigma_clipped_stats(img[bgm], sigma=3.0, maxiters=5, stdfunc="mad_std")
    val = float(np.mean(img[a]) - bg)
    var = getattr(cut, "variance", None)
    if var is not None:
        v = np.asarray(var, float)[a]
        v = np.where(np.isfinite(v) & (v > 0), v, bgsig ** 2)
        err = float(np.sqrt(v.sum()) / a.sum())
    else:
        err = float(bgsig / np.sqrt(a.sum()))
    err = float(np.hypot(err, bgsig / np.sqrt(bgm.sum())))
    return val, err


def wing_radii(cuts, coord, pct=90.0, pad=1.5, width=3.0, r_min=2.0):
    """Per detector {band: (r_in, r_out)}: just outside the target's saturated core in (almost) every exposure."""
    rs = {}
    for c in cuts:
        x, y = c.wcs.world_to_pixel(target_at(coord, c.mjd))
        rs.setdefault(f"D{c.detector}", []).append(_saturated_radius(c, float(x), float(y)))
    return {b: (max(r_min, float(np.percentile(v, pct)) + pad), max(r_min, float(np.percentile(v, pct)) + pad) + width)
            for b, v in rs.items()}


def spectrum_points(cutouts, coord: SkyCoord, epochs=None, r_ap: float = 2.0, r_in: float = 4.0,
                    r_out: float = 7.0, max_epoch_days: float | None = 60.0, neighbors: SkyCoord | None = None,
                    blend_radius_arcsec: float = 25.0, neighbor_info: list | None = None, wings: bool = False):
    """One photometric point per exposure at `coord` (propagated per exposure if it has a proper motion).

    epochs: the manifest epoch_table (preferred, so ids match the frames); otherwise split_epochs over
    the cutouts. Returns {"meta": {...}, "points": [...]}, points sorted by wavelength.
    neighbors: known sources (e.g. Gaia DR3 with pm/obstime). If any comes within blend_radius_arcsec of the
    target in any exposure, EVERY point is measured by fixed-position PSF photometry that fits the target and
    those neighbours together (deblended); otherwise by aperture photometry. meta["photometry"] says which.
    wings: (default False, not yet approved for publication) add wing_flux / wing_err (MJy/sr, RELATIVE brightness
    from the PSF wings in a fixed per-band annulus just
    outside the saturated core; meta["wing"] gives the radii). Use it for saturated stars, compare like with like
    (binned_series) - it is not a total flux.
    neighbor_info: optional list of dicts aligned with `neighbors` (e.g. gaia.json rows); their source_id and
    g_mag are copied into meta["deblended_neighbors"] = [{source_id, g_mag, ra, dec}] (missing keys -> null).
    """
    cuts = [c for c in cutouts if c is not None]
    mjd = np.array([c.mjd for c in cuts])
    ep = assign_epochs(mjd, epochs) if epochs is not None else split_epochs(mjd, max_epoch_days=max_epoch_days)
    blend = _blended(neighbors, coord, mjd, blend_radius_arcsec) if neighbors is not None and len(cuts) else []
    wr = wing_radii(cuts, coord) if wings else {}
    pts = []
    for c, e in zip(cuts, ep):
        pos = target_at(coord, c.mjd)
        x, y = c.wcs.world_to_pixel(pos)
        if blend:
            nb = _move(neighbors[blend], c.mjd)
            nxs, nys = c.wcs.world_to_pixel(nb)
            xys = [(float(x), float(y))] + [(float(a_), float(b_)) for a_, b_ in zip(np.atleast_1d(nxs), np.atleast_1d(nys))]
            fw = getattr(c, "psf_fwhm_arcsec", float("nan"))
            fw = (fw if np.isfinite(fw) else 7.5) / 6.15
            r = psf_flux(c, xys, fw)
            if r is not None:
                flux, err, flagged, sat, ovf = float(r[0][0]), float(r[1][0]), r[2], r[3], r[4]
                yy, xx = np.mgrid[0:c.image.shape[0], 0:c.image.shape[1]]
                region = np.hypot(xx - float(x), yy - float(y)) <= 1.5
                mfrac = round(float(np.mean(~(usable_pixels(c.flags) & np.isfinite(c.image))[region])), 4)
                nbf = float(np.sum(r[0][1:]))
        else:
            r = None
        if r is None:  # no neighbours, or the core cannot be PSF-fitted (saturated): aperture photometry
            nbf = None
            r = aperture_flux(c, float(x), float(y), r_ap, r_in, r_out)
            if r is None:
                continue
            flux, err, flagged, _, sat, ovf, mfrac = r
            region = _aperture_weights(c.image.shape, float(x), float(y), r_ap) > 0
        pts.append({"wavelength_um": round(float(c.wavelength_um), 5), "bandwidth_um": round(float(c.bandwidth_um), 5),
                    "flux": round(flux, 6), "err": round(err, 6), "mjd": round(float(c.mjd), 5),
                    "epoch": int(e), "band": f"D{c.detector}", "flagged": bool(flagged), "saturated": bool(sat),
                    "overflow": bool(ovf), "flags": flag_names(c.flags, region), "masked_fraction": mfrac,
                    "obs_id": c.obs_id})
        if wings and f"D{c.detector}" in wr:
            wf = wing_flux(c, float(x), float(y), *wr[f"D{c.detector}"])
            if wf is not None:
                pts[-1]["wing_flux"], pts[-1]["wing_err"] = round(wf[0], 6), round(wf[1], 6)
        if nbf is not None:
            pts[-1]["neighbor_flux"] = round(nbf, 6)
        elif blend:
            pts[-1]["photometry"] = "aperture"  # PSF fit impossible for this exposure (saturated core)
    pts.sort(key=lambda p: (p["wavelength_um"], p["mjd"]))
    if blend:
        meta = {"flux_unit": FLUX_UNIT, "photometry": "psf_deblended", "aperture_radius_px": None, "err_note": ERR_NOTE_PSF, "wing": ({"label": "relative brightness from PSF wings", "unit": "MJy/sr",
                         "annulus_px": {k: [round(v[0], 2), round(v[1], 2)] for k, v in sorted(wr.items())},
                         "method": "mean of usable pixels (clean or recovered overflow) in a fixed per-band annulus "
                                   "just outside the target's saturated core in 90% of exposures, minus a local "
                                   "background 4-10 px further out; validated against aperture flux on RW Aur D1 "
                                   "(tests/test_coadd_wings.py): the wing/aperture ratio changes between epochs by "
                                   "x1.23 (median), x1.51 (90th percentile) on unsaturated control stars, so only "
                                   "changes larger than x1.5 are significant. Not a total flux: compare epochs only at similar "
                                   "wavelength (binned_series)"} if wr else None),
                "deblended_neighbors": [{"source_id": _info(neighbor_info, i, "source_id"),
                                         "g_mag": _info(neighbor_info, i, "g_mag"),
                                         "ra": round(float(neighbors[i].ra.deg), 6), "dec": round(float(neighbors[i].dec.deg), 6)}
                                        for i in blend],
                "method": "per-exposure fixed-position PSF photometry (pixel-integrated Gaussian, FWHM = PSF_FWHM of "
                          "the exposure) fitting the target together with the known neighbours within "
                          f"{blend_radius_arcsec:g} arcsec, so their light is removed; total flux, no aperture "
                          "correction; flagged = non-informational FLAGS bit within 1.5 px (masked_fraction = masked share of "
                          "those pixels); saturated = "
                          "early overflow (OVERFLOW+SUR_ERROR) or NONLINEAR within 2 px (flux = lower limit; such "
                          "exposures fall back to aperture photometry, point.photometry = 'aperture'); overflow = "
                          "recovered overflow pixels (valid; recovered/clean median 1.04, 10 pairs, range 1.00-1.51); neighbor_flux = fitted flux of the removed "
                          "neighbours. Systematic: the Gaussian PSF leaves ~5-15% of a neighbour's flux as a "
                          "residual on the target (measured on WISE 0855 / G=16.3 star at 3-11 arcsec), so treat "
                          "|flux| < 0.15 x neighbor_flux as consistent with zero",
                "n_points": len(pts), "n_flagged": int(sum(p["flagged"] for p in pts)),
                "n_saturated": int(sum(p["saturated"] for p in pts)), "n_overflow": int(sum(p["overflow"] for p in pts)),
                "flag_legend": flag_legend(pts), "flag_legend_source": FLAG_LEGEND_SOURCE, "flags_region": FLAG_REGION_TEXT}
        return {"meta": meta, "points": pts}
    meta = {"flux_unit": FLUX_UNIT, "photometry": "aperture", "aperture_radius_px": r_ap, "wing": ({"label": "relative brightness from PSF wings", "unit": "MJy/sr",
                         "annulus_px": {k: [round(v[0], 2), round(v[1], 2)] for k, v in sorted(wr.items())},
                         "method": "mean of usable pixels (clean or recovered overflow) in a fixed per-band annulus "
                                   "just outside the target's saturated core in 90% of exposures, minus a local "
                                   "background 4-10 px further out; validated against aperture flux on RW Aur D1 "
                                   "(tests/test_coadd_wings.py): the wing/aperture ratio changes between epochs by "
                                   "x1.23 (median), x1.51 (90th percentile) on unsaturated control stars, so only "
                                   "changes larger than x1.5 are significant. Not a total flux: compare epochs only at similar "
                                   "wavelength (binned_series)"} if wr else None),
            "aperture_radius_arcsec": round(r_ap * 6.15, 2), "err_note": ERR_NOTE,
            "background_annulus_px": [r_in, r_out], "aperture_corrected": False,
            "method": "per-exposure circular aperture on the L2 image (MJy/sr x pixel solid angle), "
                      "sigma-clipped annulus background; err from VARIANCE (or annulus scatter) + background error; "
                      "masked pixels (any non-informational FLAGS bit except recovered overflow, or no data) are left "
                      "out and the flux is scaled by the PSF-weighted aperture fraction (truly saturated pixels are "
                      "kept: lower limit); flagged = more than 15% of the aperture weight masked, or a masked pixel "
                      "within 1 px of the centre, or a lower limit (masked_fraction per point; on isolated stars, "
                      "points below 15% with a clean core agree with unmasked ones to a median ratio 0.99-1.00); "
                      "where OUTLIER pixels were left out, empty-sky fluxes sit 0.3-0.5 sigma low (the outliers "
                      "removed are the positive ones); "
                      "saturated = early overflow (OVERFLOW+SUR_ERROR) or NONLINEAR in the aperture: the flux is a "
                      "lower limit; overflow = recovered overflow pixels (OVERFLOW without SUR_ERROR, slope from the "
                      "reads before the threshold; recovered/clean flux on RW Aur D1, same epoch and wavelength: median 1.04 over 10 pairs, eight within 1.00-1.20, two at 1.30 and 1.51 near 0.85 um), usable; "
                      "keep flagged points out of light curves",
            "n_points": len(pts), "n_flagged": int(sum(p["flagged"] for p in pts)),
            "n_saturated": int(sum(p["saturated"] for p in pts)), "n_overflow": int(sum(p["overflow"] for p in pts)),
                "flag_legend": flag_legend(pts), "flag_legend_source": FLAG_LEGEND_SOURCE, "flags_region": FLAG_REGION_TEXT}
    return {"meta": meta, "points": pts}


def _move(c: SkyCoord, mjd: float) -> SkyCoord:
    """Positions at mjd (proper motion applied when c has pm + obstime)."""
    try:
        has_pm = c.obstime is not None and c.pm_ra_cosdec is not None
    except (AttributeError, TypeError):
        has_pm = False
    if not has_pm:
        return SkyCoord(c.ra, c.dec)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = c.apply_space_motion(new_obstime=Time(float(mjd), format="mjd"))
    return SkyCoord(m.ra, m.dec)


def _info(info, i, key):
    if not info or i >= len(info) or info[i] is None:
        return None
    v = info[i].get(key)
    if v is None:
        return None
    return str(v) if key == "source_id" else round(float(v), 3)


def _blended(neighbors: SkyCoord, coord: SkyCoord, mjd, radius_arcsec: float) -> list:
    """Indices of neighbours that come within radius of the target at the first or last exposure."""
    out = set()
    for m in (float(np.min(mjd)), float(np.max(mjd))):
        sep = _move(neighbors, m).separation(target_at(coord, m)).arcsec
        out |= set(int(i) for i in np.nonzero((sep < radius_arcsec) & (sep > 0.5))[0])
    return sorted(out)


def binned_series(points, kind: str = "flux", nbins: int = 8):
    """Like-with-like series: per band, the band's wavelength range in nbins equal bins; per (band, bin, epoch) the
    median of the usable points (kind="flux": not flagged; kind="wing": points with wing_flux) and its error
    (1.2533 x the scatter / sqrt(n), or the point error when n = 1). Returns a list of dicts
    {kind, band, wave_um: [lo, hi], epoch, mjd, value, err, n}, sorted by band, bin, epoch."""
    key, ekey = ("flux", "err") if kind == "flux" else ("wing_flux", "wing_err")
    out = []
    for band in sorted({p["band"] for p in points}):
        q = [p for p in points if p["band"] == band and key in p and (kind != "flux" or not p["flagged"])]
        if not q:
            continue
        wl = np.array([p["wavelength_um"] for p in points if p["band"] == band])
        edges = np.linspace(wl.min(), wl.max() + 1e-9, nbins + 1)
        for i in range(nbins):
            for e in sorted({p["epoch"] for p in q}):
                g = [p for p in q if p["epoch"] == e and edges[i] <= p["wavelength_um"] < edges[i + 1]]
                if not g:
                    continue
                v = np.array([p[key] for p in g])
                err = float(1.2533 * np.std(v) / np.sqrt(len(v))) if len(v) > 2 else float(np.max([p[ekey] for p in g]))
                out.append({"kind": kind, "band": band, "wave_um": [round(float(edges[i]), 4), round(float(edges[i + 1]), 4)],
                            "epoch": int(e), "mjd": round(float(np.median([p["mjd"] for p in g])), 3),
                            "value": round(float(np.median(v)), 6), "err": round(err, 6), "n": len(g)})
    return out


VARIABILITY_METHOD = (
    "variability(binned_series(points)): per band, the band's wavelength range in 8 equal bins; per bin the epoch "
    "medians of the usable (not flagged) points; max/min over epochs (bins with any value <= 3 err skipped); median "
    "over bins. Significance: control stars measured the SAME way in the SAME cutouts (select_controls): Gaia DR3 "
    "stars 30\" to 4.5' from the target, G within +-1.5 mag of the target (G 10-15 when the target is saturated or "
    "brighter than G 11.5), with no Gaia neighbour within 20\" brighter than G_control + 2.5 (neighbour light < 10%), "
    "up to 20 stars. A band is variable when at least 5 controls have a ratio in that band and the target's ratio "
    "exceeds both 1.15 and the largest control ratio; with fewer than 5 controls the band is reported as undetermined.")


def select_controls(gaia_rows, target_radec, target_g=None, n_max=20, radius_arcmin=4.5, saturated=False):
    """Control stars for variability() as described in VARIABILITY_METHOD. gaia_rows: dicts with ra, dec, g_mag
    (gaia.json rows, or any source list covering the cutouts). Returns the selected rows, brightest first."""
    rows = [r for r in gaia_rows if r.get("g_mag") is not None]
    if not rows:
        return []
    ra = np.array([r["ra"] for r in rows]); dec = np.array([r["dec"] for r in rows]); g = np.array([r["g_mag"] for r in rows])
    allc = SkyCoord(ra, dec, unit="deg")
    t = SkyCoord(*target_radec, unit="deg")
    if target_g is None or saturated or target_g < 11.5:
        lo, hi = 10.0, 15.0
    else:
        lo, hi = target_g - 1.5, target_g + 1.5
    out = []
    for i in np.argsort(g):
        if not (lo <= g[i] <= hi):
            continue
        sep_t = allc[i].separation(t).arcsec
        if sep_t < 30 or sep_t > radius_arcmin * 60:
            continue
        sep = allc.separation(allc[i]).arcsec
        if np.any((sep > 0.5) & (sep < 20) & (g < g[i] + 2.5)):
            continue
        out.append(rows[i])
        if len(out) >= n_max:
            break
    return out


def is_variable(target_ratio, control_ratios, min_controls: int = 5, floor: float = 1.15):
    """True / False per VARIABILITY_METHOD, or None (undetermined) with fewer than min_controls controls."""
    c = [r for r in control_ratios if r is not None and np.isfinite(r)]
    if len(c) < min_controls or target_ratio is None:
        return None
    return bool(target_ratio > floor and target_ratio > max(c))


def variability(series, min_epochs: int = 2):
    """{band: dict(ratio, n_bins)}: per wavelength bin with >= min_epochs epochs, max/min of the epoch values
    (bins with any value <= 3 err are skipped), then the median over bins. 1.0 = constant."""
    out = {}
    for band in sorted({r["band"] for r in series}):
        ratios = []
        for wv in sorted({tuple(r["wave_um"]) for r in series if r["band"] == band}):
            g = [r for r in series if r["band"] == band and tuple(r["wave_um"]) == wv]
            if len(g) < min_epochs or any(r["value"] <= 3 * r["err"] for r in g):
                continue
            v = [r["value"] for r in g]
            ratios.append(max(v) / min(v))
        if ratios:
            out[band] = {"ratio": round(float(np.median(ratios)), 3), "n_bins": len(ratios)}
    return out


# --------------------------------------------------------------------------- co-add measurements

def matched_filter(img: np.ndarray, fwhm_px: float) -> np.ndarray:
    """Point-source matched filter for a Gaussian PSF: NaN-aware convolution with the PSF, normalised so
    the output at a point source equals its peak-flux estimate."""
    s = fwhm_px / 2.3548
    g = np.isfinite(img)
    num = gaussian_filter(np.where(g, img, 0.0), s)
    den = gaussian_filter(g.astype(float), s)
    k2 = 1.0 / (4 * np.pi * s * s)  # sum of squared normalised Gaussian kernel
    return np.where(den > 0.5, num / np.maximum(den, 1e-12), np.nan) / (k2 * 2 * np.pi * s * s)


def source_mask(fimg: np.ndarray, nsig: float = 3.0, grow_px: int = 5) -> np.ndarray:
    """True where the filtered image has emission above nsig robust sigma (grown), i.e. not empty sky."""
    fin = fimg[np.isfinite(fimg)]
    _, med, sig = sigma_clipped_stats(fin, sigma=3.0, maxiters=10, stdfunc="mad_std")
    m = np.nan_to_num(fimg - med) > nsig * sig
    return maximum_filter(m, size=2 * grow_px + 1) | ~np.isfinite(fimg)


def centroid(img: np.ndarray, x0: float, y0: float, search_px: float = 3.0, fwhm_px: float = 2.5):
    """Gaussian-weighted centroid starting from the matched-filter peak within search_px of (x0, y0)."""
    sm = matched_filter(img, fwhm_px)
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    near = (np.hypot(xx - x0, yy - y0) <= search_px) & np.isfinite(sm)
    j = np.nanargmax(np.where(near, sm, -np.inf))
    cx, cy = float(xx.flat[j]), float(yy.flat[j])
    s = fwhm_px / 2.3548
    for _ in range(10):
        w = np.exp(-0.5 * ((xx - cx) ** 2 + (yy - cy) ** 2) / s ** 2) * np.clip(np.nan_to_num(img), 0, None)
        w[np.hypot(xx - cx, yy - cy) > 2 * fwhm_px] = 0
        if w.sum() <= 0:
            break
        cx, cy = float((w * xx).sum() / w.sum()), float((w * yy).sum() / w.sum())
    return cx, cy


def _gauss(xx, yy, x, y, amp, s):
    return amp * np.exp(-0.5 * ((xx - x) ** 2 + (yy - y) ** 2) / s ** 2)


def fit_psf_pair(img: np.ndarray, guess_xy, star_xy=None, fwhm_px: float = 2.5, box_px: int = 9, noise=None):
    """Least-squares fit of a Gaussian PSF (fixed FWHM) for the mover plus, optionally, a second Gaussian
    at the FIXED position star_xy (a blended background star) and a constant background.

    Returns dict(x, y, amp, sx, sy, star_amp) with 1-sigma errors from the Jacobian scaled by the residual
    noise (or by `noise` per pixel if given).
    """
    gx, gy = guess_xy
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    sel = (np.abs(xx - gx) <= box_px) & (np.abs(yy - gy) <= box_px) & np.isfinite(img)
    X, Y, Z = xx[sel].astype(float), yy[sel].astype(float), img[sel].astype(float)
    s = fwhm_px / 2.3548

    def model(p):
        m = _gauss(X, Y, p[0], p[1], p[2], s) + p[3]
        if star_xy is not None:
            m = m + _gauss(X, Y, star_xy[0], star_xy[1], p[4], s)
        return m

    a0 = float(np.nanmax(Z) - np.nanmedian(Z))
    p0 = [gx, gy, a0, float(np.nanmedian(Z))] + ([a0] if star_xy is not None else [])
    r = least_squares(lambda p: model(p) - Z, p0)
    dof = max(1, Z.size - len(p0))
    sig2 = (noise ** 2) if noise is not None else float(np.sum(r.fun ** 2) / dof)
    try:
        cov = np.linalg.inv(r.jac.T @ r.jac) * sig2
        err = np.sqrt(np.diag(cov))
    except np.linalg.LinAlgError:
        err = np.full(len(p0), np.nan)
    out = {"x": r.x[0], "y": r.x[1], "amp": r.x[2], "sx": err[0], "sy": err[1], "samp": err[2]}
    if star_xy is not None:
        out["star_amp"] = r.x[4]
    return {k: float(v) for k, v in out.items()}


def fit_motion(mjd, ra, dec, sig_ra_arcsec, sig_dec_arcsec):
    """Weighted linear fit of position vs time. Returns pmra*cos(dec), pmdec and their 1-sigma errors (arcsec/yr)."""
    t = (np.asarray(mjd, float) - np.mean(mjd)) / 365.25
    ra, dec = np.asarray(ra, float), np.asarray(dec, float)
    x = (ra - ra.mean()) * np.cos(np.radians(dec.mean())) * 3600
    y = (dec - dec.mean()) * 3600
    out = {}
    for name, v, s in (("pmra", x, np.asarray(sig_ra_arcsec)), ("pmdec", y, np.asarray(sig_dec_arcsec))):
        w = 1 / s ** 2
        A = np.vstack([t, np.ones_like(t)]).T
        cov = np.linalg.inv(A.T @ (A * w[:, None]))
        p = cov @ (A.T @ (w * v))
        chi2 = float(np.sum(w * (v - A @ p) ** 2))
        out[name], out[name + "_err"], out[name + "_chi2"] = float(p[0]), float(np.sqrt(cov[0, 0])), chi2
    return out


def snr_map(img: np.ndarray, fwhm_px: float, noise_img: np.ndarray | None = None):
    """Matched-filter S/N map. Noise = robust std of the filtered noise_img (e.g. half-split difference /2,
    which has no sky signal) if given, else of the filtered image's empty-sky pixels."""
    f = matched_filter(img, fwhm_px)
    if noise_img is not None:
        fn = matched_filter(noise_img, fwhm_px)
        fin = fn[np.isfinite(fn)]
        sig = float(sigma_clipped_stats(fin, sigma=4.0, stdfunc="mad_std")[2])
    else:
        empty = ~source_mask(f)
        sig = float(sigma_clipped_stats(f[empty], sigma=4.0, stdfunc="mad_std")[2])
    _, med, _ = sigma_clipped_stats(f[np.isfinite(f) & ~source_mask(f)], sigma=3.0, stdfunc="mad_std")
    return (f - med) / sig, sig


def null_test(snr: np.ndarray, mask: np.ndarray, n: int = 2000, search_px: float = 2.0, edge_px: int = 10, seed: int = 0):
    """S/N measured the same way as the detection (max within search_px of a position) at n random positions
    whose search disc avoids `mask` (known emission). Returns the array of those maxima.
    search_px=0 gives the S/N at the position itself (its robust width calibrates the S/N scale)."""
    rng = np.random.default_rng(seed)
    ny, nx = snr.shape
    yy, xx = np.mgrid[0:ny, 0:nx]
    disc = int(np.ceil(search_px))
    vals = []
    tries = 0
    while len(vals) < n and tries < 50 * n:
        tries += 1
        x, y = rng.uniform(edge_px, nx - edge_px), rng.uniform(edge_px, ny - edge_px)
        if search_px <= 0:
            i, j = int(round(y)), int(round(x))
            if not mask[i, j] and np.isfinite(snr[i, j]):
                vals.append(float(snr[i, j]))
            continue
        sl = (slice(max(0, int(y) - disc), int(y) + disc + 2), slice(max(0, int(x) - disc), int(x) + disc + 2))
        d = np.hypot(xx[sl] - x, yy[sl] - y) <= search_px
        if mask[sl][d].any() or not np.isfinite(snr[sl][d]).all():
            continue
        vals.append(float(np.max(snr[sl][d])))
    return np.array(vals)


def measure_fwhm(img: np.ndarray, n_stars: int = 12, box_px: int = 6, min_sep_px: int = 10, guess_px: float = 2.5):
    """Median FWHM (px) of Gaussian fits to the brightest isolated, unsaturated-looking stars of a frame.

    Returns (median_fwhm_px, list of individual fwhm_px)."""
    f = matched_filter(img, guess_px)
    fin = f[np.isfinite(f)]
    _, med, sig = sigma_clipped_stats(fin, sigma=3.0, stdfunc="mad_std")
    peaks = (f == maximum_filter(np.nan_to_num(f, nan=-np.inf), size=2 * min_sep_px + 1)) & (f - med > 20 * sig)
    py, px = np.nonzero(peaks)
    order = np.argsort(-f[py, px])
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    out = []
    for j in order[1:]:  # skip the very brightest (often saturated / filled)
        x0, y0 = px[j], py[j]
        if not (box_px <= x0 < img.shape[1] - box_px and box_px <= y0 < img.shape[0] - box_px):
            continue
        sel = (np.abs(xx - x0) <= box_px) & (np.abs(yy - y0) <= box_px) & np.isfinite(img)
        X, Y, Z = xx[sel].astype(float), yy[sel].astype(float), img[sel].astype(float)
        r = least_squares(lambda p: _gauss(X, Y, p[0], p[1], p[2], abs(p[3])) + p[4] - Z,
                          [x0, y0, float(Z.max()), guess_px / 2.3548, float(np.median(Z))])
        fw = abs(r.x[3]) * 2.3548
        if r.success and 1.0 < fw < 4 * guess_px:
            out.append(float(fw))
        if len(out) >= n_stars:
            break
    return (float(np.median(out)) if out else float("nan")), out


def psf_amplitudes(img: np.ndarray, positions, fwhm_px: float, box_px: int = 9, noise: float | None = None):
    """Linear least-squares amplitudes of Gaussian PSFs at FIXED positions (grid px) + a constant background.

    The box is centred on the mean position. Returns (amps, errs, bg): peak values in the image unit; errors
    from the normal-equation covariance with the per-pixel `noise` (or the residual rms if None).
    """
    pos = np.asarray(positions, float)
    cx, cy = pos.mean(axis=0)
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    sel = (np.abs(xx - cx) <= box_px) & (np.abs(yy - cy) <= box_px) & np.isfinite(img)
    X, Y, Z = xx[sel].astype(float), yy[sel].astype(float), img[sel].astype(float)
    s = fwhm_px / 2.3548
    A = np.column_stack([_gauss(X, Y, x, y, 1.0, s) for x, y in pos] + [np.ones_like(X)])
    p, *_ = np.linalg.lstsq(A, Z, rcond=None)
    resid = Z - A @ p
    sig2 = noise ** 2 if noise is not None else float(np.sum(resid ** 2) / max(1, Z.size - A.shape[1]))
    cov = np.linalg.inv(A.T @ A) * sig2
    err = np.sqrt(np.diag(cov))
    return p[:-1], err[:-1], float(p[-1])


def gauss_flux_mjy(amp, fwhm_px: float, pix_arcsec: float):
    """Total flux (mJy) of a Gaussian of peak `amp` (MJy/sr) and FWHM fwhm_px on a grid of pix_arcsec pixels."""
    s = fwhm_px / 2.3548
    omega = (pix_arcsec / 206264.806) ** 2
    return np.asarray(amp) * 2 * np.pi * s * s * omega * 1e9
