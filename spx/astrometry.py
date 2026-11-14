"""Single-exposure astrometry of a moving object for an MPC (ADES) report.

A report needs one observation per exposure (mid-exposure time + position), not a co-add: blink frames span days and
fast Solar System objects are clipped out of them. measure_track() predicts the object's position in every exposure
from the user's marks (linear motion on the sky), fits a PSF centre there on the native L2 cutout (the object free,
known Gaia neighbours within 25" fixed), and returns per-exposure positions with honest errors.

Error per coordinate = fit error (Jacobian, scaled by sqrt(reduced chi2) when > 1) (+) frame WCS error, in quadrature.
WCS_ERR_ARCSEC is measured, not assumed: see tests/test_astrometry.py (Gaia stars in the same cutouts).

Frames and facts used (SPHEREx Explanatory Supplement v2.1 and file headers):
  - time: MJD-AVG / DATE-AVG, mid-exposure, UTC.
  - astrometric reference: Gaia DR3, proper-motion corrected to the exposure epoch (Supplement sect. 3.2.8);
    FINAST = 0 is a good solution.
  - spacecraft position X_SC/Y_SC/Z_SC [km], XYZ_SC_SYSTEM = GEOCENTER. The axes are not named in the Supplement;
    from the headers they are ICRF/J2000 equatorial (GCRS): treating them as GCRS reproduces the header ground-track
    longitude to 0.06 deg, treating them as Earth-fixed misses by ~95 deg, as true-of-date by the 0.35 deg of
    precession since J2000 (tests/test_astrometry.py).
"""
from __future__ import annotations

import warnings

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from scipy.optimize import least_squares
import astropy.units as _u

u_arcsec = _u.arcsec

from spx.coadd import BAD_MASK, FLAG_BITS, saturated_pixels, usable_pixels
from spx.photometry import _move, _pixel_gauss

PIX_ARCSEC = 6.15
WCS_ERR_ARCSEC = 0.23  # per-axis floor measured on 3540 Gaia star fits (tests/test_astrometry.py)
# Bits a MOVING object legitimately raises on its own core: the pipeline's outlier detection flags a source that
# is not on the static sky (OUTLIER), and the onboard ramp fit can flag its passage (TRANSIENT). Only these.
MOVER_ALLOWED_BITS = (1 << FLAG_BITS["OUTLIER"]) | (1 << FLAG_BITS["TRANSIENT"])


def _local_scale(wcs, x, y):
    """d(ra*cos dec, dec)/d(x, y) in arcsec/px at (x, y) (2x2 Jacobian)."""
    c0 = wcs.pixel_to_world(x, y)
    cx = wcs.pixel_to_world(x + 0.5, y)
    cy = wcs.pixel_to_world(x, y + 0.5)
    cosd = np.cos(np.radians(c0.dec.deg))
    J = np.array([[(cx.ra.deg - c0.ra.deg) * cosd, (cy.ra.deg - c0.ra.deg) * cosd],
                  [cx.dec.deg - c0.dec.deg, cy.dec.deg - c0.dec.deg]]) * 3600 / 0.5
    return c0, J


def fit_position(cut, guess: SkyCoord, neighbors: SkyCoord | None = None, fwhm_arcsec: float | None = None,
                 box_px: int = 5, max_shift_px: float = 2.0, allow_bits: int = 0):
    """Free-position PSF fit of one point source near `guess` on a native cutout.

    Model: pixel-integrated Gaussian (FWHM = PSF_FWHM of the exposure unless given) for the object at a free position,
    plus fixed-position Gaussians for `neighbors` (already at this epoch) inside the box, plus a constant. Usable
    pixels only (clean or recovered overflow). Returns a dict (ra, dec, sig_ra_arcsec, sig_dec_arcsec, flux_mjy,
    snr, snr_noise, chi2r, flagged, saturated, shift_px, core_bits) or None when the box is off the cutout or the fit
    fails. sig_* are fit errors only (no WCS term). snr: amplitude / its error inflated by sqrt(chi2r) (includes the
    Gaussian-model misfit); snr_noise: amplitude / its error propagated from VARIANCE alone (noise, not misfit). allow_bits: extra FLAGS bits treated as usable (MOVER_ALLOWED_BITS for a
    moving object); flagged then means a pixel near the core that is unusable even with them.
    """
    img = np.asarray(cut.image, float)
    flags = np.asarray(cut.flags) if cut.flags is not None else np.zeros(img.shape, np.int32)
    ny, nx = img.shape
    gx, gy = [float(v) for v in cut.wcs.world_to_pixel(guess)]
    if not (box_px + 1 <= gx <= nx - box_px - 2 and box_px + 1 <= gy <= ny - box_px - 2):
        return None
    fw = fwhm_arcsec or getattr(cut, "psf_fwhm_arcsec", float("nan"))
    fw = (fw if np.isfinite(fw) else 7.5) / PIX_ARCSEC
    yy, xx = np.mgrid[0:ny, 0:nx]
    box = (np.abs(xx - gx) <= box_px) & (np.abs(yy - gy) <= box_px)
    usable = usable_pixels(flags) | (usable_pixels(flags & ~allow_bits) if allow_bits else False)
    ok = np.isfinite(img) & usable
    use = box & ok
    rr = np.hypot(xx - gx, yy - gy)
    if use[rr <= 1.5].sum() < 0.5 * (rr <= 1.5).sum() or use.sum() < 30:
        return None
    ring = ok & ((flags & BAD_MASK) == 0) & (rr >= box_px + 2) & (rr <= box_px + 6)
    if ring.sum() < 15:
        return None
    _, bg0, bgsig = sigma_clipped_stats(img[ring], sigma=3.0, maxiters=5, stdfunc="mad_std")
    var = getattr(cut, "variance", None)
    v = np.asarray(var, float)[use] if var is not None else np.full(use.sum(), bgsig ** 2)
    v = np.where(np.isfinite(v) & (v > 0), v, bgsig ** 2)
    w = 1 / np.sqrt(v)
    Z = img[use]
    nxy = []
    if neighbors is not None and len(neighbors):
        px, py = cut.wcs.world_to_pixel(neighbors)
        for a, b in zip(np.atleast_1d(px), np.atleast_1d(py)):
            if abs(a - gx) <= box_px + 3 and abs(b - gy) <= box_px + 3 and np.hypot(a - gx, b - gy) > 0.3:
                nxy.append((float(a), float(b)))
    shape = img.shape

    def model(p):
        m = p[2] * _pixel_gauss(shape, p[0], p[1], fw)[use] + p[3]
        for k, (a, b) in enumerate(nxy):
            m = m + p[4 + k] * _pixel_gauss(shape, a, b, fw)[use]
        return m

    a0 = max(float(np.nansum((img - bg0)[box & ok & (rr <= 2)])), 1e-6)
    p0 = [gx, gy, a0, bg0] + [a0 * 0.1] * len(nxy)
    lo = [gx - max_shift_px, gy - max_shift_px, -np.inf, -np.inf] + [-np.inf] * len(nxy)
    hi = [gx + max_shift_px, gy + max_shift_px, np.inf, np.inf] + [np.inf] * len(nxy)
    try:
        r = least_squares(lambda p: (model(p) - Z) * w, p0, bounds=(lo, hi))
    except Exception:
        return None
    dof = max(1, Z.size - len(p0))
    chi2r = float(np.sum(r.fun ** 2) / dof)
    try:
        cov0 = np.linalg.inv(r.jac.T @ r.jac)  # VARIANCE-weighted residuals: noise-only covariance
    except np.linalg.LinAlgError:
        return None
    cov = cov0 * max(1.0, chi2r)
    ea0 = float(np.sqrt(abs(cov0[2, 2])))
    ex, ey, ea = np.sqrt(np.abs(np.diag(cov)))[:3]
    c, J = _local_scale(cut.wcs, r.x[0], r.x[1])
    cxy = cov[:2, :2]
    csky = J @ cxy @ J.T
    from astropy.wcs.utils import proj_plane_pixel_area
    to_mjy = 1e6 * proj_plane_pixel_area(cut.wcs) * (np.pi / 180) ** 2 * 1e3
    near = rr <= 2.0
    core_bits = sorted({n for n, b in FLAG_BITS.items() if n not in ("SOURCE", "FULLSAMPLE")
                        and ((flags[near] & (1 << b)) != 0).any()})
    return {"ra": float(c.ra.deg), "dec": float(c.dec.deg),
            "sig_ra_arcsec": float(np.sqrt(csky[0, 0])), "sig_dec_arcsec": float(np.sqrt(csky[1, 1])),
            "flux_mjy": float(r.x[2] * to_mjy), "snr": float(r.x[2] / ea) if ea > 0 else 0.0,
            "snr_noise": float(r.x[2] / ea0) if ea0 > 0 else 0.0, "chi2r": chi2r,
            "flagged": bool((~usable)[near].any() | ~np.isfinite(img[near]).all()), "core_bits": core_bits,
            "saturated": bool(saturated_pixels(flags)[near].any()),
            "shift_px": float(np.hypot(r.x[0] - gx, r.x[1] - gy)), "n_neighbors": len(nxy)}


# --------------------------------------------------------------------------- track measurement

# Motion during one exposure above which a fit is not trusted. Validated on (433) Eros up to 4.9" per exposure
# (scatter does not grow with trail length); beyond one native pixel it is untested, so such exposures are rejected.
TRAIL_LIMIT_ARCSEC = 6.15
VISIT_GAP_DAYS = 30.0      # same rule as spx.coadd.split_epochs: marks within one survey epoch -> "within_visit"


def track_model(marks):
    """Linear motion on the sky from >= 2 marks [{ra, dec, mjd}]. Returns (position(mjd) -> SkyCoord, info)."""
    if len(marks) < 2:
        raise ValueError("need at least two marks")
    m = sorted(marks, key=lambda k: k["mjd"])
    t = np.array([k["mjd"] for k in m], float)
    c = SkyCoord([k["ra"] for k in m], [k["dec"] for k in m], unit="deg")
    ref = c[0]
    off = ref.spherical_offsets_to(c)
    x, y = off[0].to_value("arcsec"), off[1].to_value("arcsec")
    t0 = float(np.mean(t))
    A = np.vstack([t - t0, np.ones_like(t)]).T
    px, *_ = np.linalg.lstsq(A, x, rcond=None)
    py, *_ = np.linalg.lstsq(A, y, rcond=None)
    span = float(t.max() - t.min())
    regime = "within_visit" if span <= VISIT_GAP_DAYS else "between_epochs"
    if regime == "within_visit":
        pad = max(span, 1.0)
        window = (float(t.min() - pad), float(t.max() + pad))
    else:
        window = (-np.inf, np.inf)

    def position(mjd):
        dx, dy = px[0] * (mjd - t0) + px[1], py[0] * (mjd - t0) + py[1]
        return ref.spherical_offsets_by(dx * u_arcsec, dy * u_arcsec)

    info = {"regime": regime, "span_days": span, "rate_arcsec_per_day": (float(px[0]), float(py[0])),
            "speed_arcsec_per_s": float(np.hypot(px[0], py[0]) / 86400), "window_mjd": window, "n_marks": len(m)}
    return position, info


def _calibrated_snr(cut, x, y, flux_mjy, fwhm_px, n=24, r_min=5.0, seed=0):
    """S/N against the scatter of the same fixed-position PSF flux measured at n random positions of the cutout
    (robust width), i.e. the per-exposure equivalent of the WISE 0855 null test. None if too few positions."""
    from spx.photometry import psf_flux
    rng = np.random.default_rng(seed)
    ny, nx = cut.image.shape
    vals, tries = [], 0
    while len(vals) < n and tries < 6 * n:
        tries += 1
        qx, qy = rng.uniform(8, nx - 9), rng.uniform(8, ny - 9)
        if np.hypot(qx - x, qy - y) < r_min:
            continue
        r = psf_flux(cut, [(qx, qy)], fwhm_px)
        if r is not None:
            vals.append(float(r[0][0]))
    if len(vals) < n // 2:
        return None
    w = 1.4826 * np.median(np.abs(np.array(vals) - np.median(vals)))
    return float(flux_mjy / w) if w > 0 else None


BLEND_FWHM = 0.6  # a Gaia neighbour closer than this many PSF FWHM to the object: one exposure cannot separate them


def measure_track(cutouts, marks, neighbors: SkyCoord | None = None, snr_min: float = 5.0,
                  max_offset_arcsec: float = 8.0, blend_radius_arcsec: float = 25.0, calibrate_snr: bool = True):
    """Per-exposure positions of the object marked by the user (see module docstring).

    marks: >= 2 dicts {ra, dec, mjd} of the same object at different times. neighbors: Gaia sources (pm + obstime)
    fitted as fixed PSFs within blend_radius_arcsec. Returns (rows, info); one row per cutout in the model's time
    window, accepted or not, with the reason. Row fields: obs_id, detector, band, wavelength_um, obs_time (DATE-AVG,
    UTC mid-exposure), mjd_mid, exp_s, ra, dec, rms_ra, rms_dec (arcsec; RA as RA*cos(dec)), snr_fit,
    snr_calibrated, snr_variance, snr_image, flux_mjy, flagged, saturated, core_bits, finast,
    offset_from_model_arcsec, trail_arcsec, trailed, sc_pos_km, sc_vel_kms, regime, accepted, reason.

    S/N fields (measured on a (433) Eros pass, 52 exposures: snr_fit 15-66, snr_variance 57-150, snr_calibrated
    126-613):
      snr_fit         amplitude / fit error inflated by sqrt(chi2r): includes the Gaussian-PSF misfit, which grows with
                      brightness; a fit-quality number, not the image noise.
      snr_calibrated  flux / scatter of the same PSF flux at empty positions: background noise only, so it overstates
                      a bright source whose own photon noise dominates (~4x the background term on Eros).
      snr_variance    amplitude / error propagated from the VARIANCE layer: background plus source photon noise.
      snr_image       min(snr_variance, snr_calibrated): the S/N of the source in the image (ADES logSNR). On Eros the
                      empty-position scatter is ~1.3x the VARIANCE prediction, so for a background-limited source the
                      calibrated value is expected to be the lower one.
    Acceptance stays min(snr_fit, snr_calibrated) >= snr_min (conservative: a poor fit is not accepted).
    """
    position, info = track_model(marks)
    lo, hi = info["window_mjd"]
    rows = []
    for c in sorted([c for c in cutouts if c is not None], key=lambda c: c.mjd):
        if not (lo <= c.mjd <= hi):
            continue
        pred = position(c.mjd)
        nb = None
        if neighbors is not None and len(neighbors):
            nb = _move(neighbors, c.mjd)
            nb = nb[nb.separation(pred).arcsec < blend_radius_arcsec]
        exp_s = float(getattr(c, "exptime_s", None) or 113.58)
        trail = info["speed_arcsec_per_s"] * exp_s
        row = {"obs_id": c.obs_id, "detector": int(c.detector), "band": f"D{c.detector}",
               "wavelength_um": round(float(c.wavelength_um), 5), "obs_time": getattr(c, "date_avg", None),
               "mjd_mid": float(c.mjd), "exp_s": exp_s, "finast": getattr(c, "finast", None),
               "sc_pos_km": getattr(c, "sc_pos_km", None), "sc_vel_kms": getattr(c, "sc_vel_kms", None),
               "regime": info["regime"], "trail_arcsec": round(trail, 3),
               "trailed": bool(TRAIL_LIMIT_ARCSEC is not None and trail > TRAIL_LIMIT_ARCSEC),
               "pred_ra": float(pred.ra.deg), "pred_dec": float(pred.dec.deg)}
        f = fit_position(c, pred, nb, allow_bits=MOVER_ALLOWED_BITS)
        if f is None:
            row.update({"ra": None, "dec": None, "accepted": False,
                        "reason": "no fit (position off the cutout or core pixels unusable)"})
            rows.append(row)
            continue
        fit_c = SkyCoord(f["ra"], f["dec"], unit="deg")
        rms_ra = float(np.hypot(f["sig_ra_arcsec"], WCS_ERR_ARCSEC))
        rms_dec = float(np.hypot(f["sig_dec_arcsec"], WCS_ERR_ARCSEC))
        fw_arcsec = getattr(c, "psf_fwhm_arcsec", float("nan"))
        fw_arcsec = fw_arcsec if np.isfinite(fw_arcsec) else 7.5
        degenerate = not (np.isfinite(rms_ra) and np.isfinite(rms_dec) and f["snr"] > 0)
        blend = None
        if nb is not None and len(nb):
            dmin = float(min(nb.separation(fit_c).arcsec.min(), nb.separation(pred).arcsec.min()))
            if dmin < BLEND_FWHM * fw_arcsec:
                blend = dmin
        snr_cal = None
        if calibrate_snr and not degenerate and blend is None:
            x, y = [float(v) for v in c.wcs.world_to_pixel(fit_c)]
            snr_cal = _calibrated_snr(c, x, y, f["flux_mjy"], fw_arcsec / PIX_ARCSEC)
        off = float(fit_c.separation(pred).arcsec)
        row.update({"ra": f["ra"], "dec": f["dec"], "rms_ra": round(rms_ra, 3), "rms_dec": round(rms_dec, 3),
                    "snr_fit": round(f["snr"], 2), "snr_calibrated": None if snr_cal is None else round(snr_cal, 2),
                    "snr_variance": round(f["snr_noise"], 2),
                    "snr_image": None if snr_cal is None else round(min(f["snr_noise"], snr_cal), 2),
                    "flux_mjy": round(f["flux_mjy"], 5), "flagged": f["flagged"], "saturated": f["saturated"],
                    "core_bits": f["core_bits"], "offset_from_model_arcsec": round(off, 2)})
        reasons = []
        if degenerate:
            reasons.append("degenerate fit (no finite error)")
        if blend is not None:
            reasons.append(f"blended: Gaia source {blend:.1f}\" away (< {BLEND_FWHM:g} x FWHM {fw_arcsec:.1f}\")")
        snr_use = min(f["snr"], snr_cal) if snr_cal is not None else f["snr"]
        if not degenerate and snr_use < snr_min:
            reasons.append(f"S/N {snr_use:.1f} < {snr_min:g}")
        if f["flagged"]:
            reasons.append("flagged pixels in the core: " + ",".join(f["core_bits"]))
        if f["saturated"]:
            reasons.append("saturated")
        if row["finast"] not in (0, None):
            reasons.append(f"FINAST {row['finast']} (frame astrometry not good)")
        if off > max_offset_arcsec:
            reasons.append(f"{off:.1f}\" from the motion model (> {max_offset_arcsec:g}\")")
        if row["trailed"]:
            reasons.append(f"trailed: {trail:.1f}\" motion during the exposure (> {TRAIL_LIMIT_ARCSEC:g}\")")
        row["accepted"] = not reasons
        row["reason"] = "; ".join(reasons) if reasons else "ok"
        rows.append(row)
    return rows, info
