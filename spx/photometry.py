"""Per-exposure photometry (spectrum / light curve) and point-source measurements on co-added frames.

spectrum_points(cutouts, coord)  -> {"meta": {...}, "points": [{wavelength_um, bandwidth_um, flux, err,
                                     mjd, epoch, band, flagged}, ...]}   (data contract section 5)
Measurement helpers used by tests/test_coadd_wise0855.py:
  matched_filter, snr_map, centroid, fit_psf_pair, fit_motion, null_test
"""
from __future__ import annotations

import warnings

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.stats import sigma_clipped_stats
from astropy.wcs.utils import proj_plane_pixel_area
from scipy.ndimage import gaussian_filter, maximum_filter
from scipy.optimize import least_squares

from spx.coadd import BAD_MASK, HARD_MASK, assign_epochs, split_epochs, target_at

FLUX_UNIT = "mJy"


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

    Returns (flux_mJy, err_mJy, flagged, n_bad_in_aperture) or None when the aperture is off the cutout.
    Pixels with HARD_MASK flags (dead, hot, cold, missing...) are excluded and the flux is scaled
    up by the excluded aperture fraction; any BAD_MASK pixel in the aperture sets flagged=True
    (saturated cores of bright stars are kept but flagged).
    """
    img = np.asarray(cut.image, float)
    flags = np.asarray(cut.flags) if cut.flags is not None else np.zeros(img.shape, np.int32)
    var = np.asarray(cut.variance, float) if getattr(cut, "variance", None) is not None else None
    ny, nx = img.shape
    if not (r_ap <= x <= nx - 1 - r_ap and r_ap <= y <= ny - 1 - r_ap):
        return None
    yy, xx = np.mgrid[0:ny, 0:nx]
    rr = np.hypot(xx - x, yy - y)
    usable = np.isfinite(img) & ((flags & HARD_MASK) == 0)
    clean = np.isfinite(img) & ((flags & BAD_MASK) == 0)
    ann = clean & (rr >= r_in) & (rr <= r_out)
    if ann.sum() < 10:
        return None
    _, bg, bgsig = sigma_clipped_stats(img[ann], sigma=3.0, maxiters=5, stdfunc="mad_std")
    w = _aperture_weights(img.shape, x, y, r_ap)
    wu = np.where(usable, w, 0.0)
    if wu.sum() < 0.5 * w.sum():
        return None
    s = np.nansum(wu * (img - bg)) * (w.sum() / wu.sum())
    if var is not None:
        v = np.nansum(wu ** 2 * np.where(np.isfinite(var), var, 0.0))
    else:
        v = (wu ** 2).sum() * bgsig ** 2
    v += (wu.sum() ** 2) * bgsig ** 2 / ann.sum()  # background-estimate error
    omega_sr = proj_plane_pixel_area(cut.wcs) * (np.pi / 180) ** 2
    to_mjy = 1e6 * omega_sr * 1e3  # MJy/sr * sr -> Jy -> mJy
    n_bad = int(((w > 0) & ((flags & BAD_MASK) != 0)).sum())
    return float(s * to_mjy), float(np.sqrt(v) * to_mjy), n_bad > 0, n_bad


def spectrum_points(cutouts, coord: SkyCoord, epochs=None, r_ap: float = 2.0, r_in: float = 4.0,
                    r_out: float = 7.0, max_epoch_days: float | None = 60.0):
    """One photometric point per exposure at `coord` (propagated per exposure if it has a proper motion).

    epochs: the manifest epoch_table (preferred, so ids match the frames); otherwise split_epochs over
    the cutouts. Returns {"meta": {...}, "points": [...]}, points sorted by wavelength.
    """
    cuts = [c for c in cutouts if c is not None]
    mjd = np.array([c.mjd for c in cuts])
    ep = assign_epochs(mjd, epochs) if epochs is not None else split_epochs(mjd, max_epoch_days=max_epoch_days)
    pts = []
    for c, e in zip(cuts, ep):
        pos = target_at(coord, c.mjd)
        x, y = c.wcs.world_to_pixel(pos)
        r = aperture_flux(c, float(x), float(y), r_ap, r_in, r_out)
        if r is None:
            continue
        flux, err, flagged, _ = r
        pts.append({"wavelength_um": round(float(c.wavelength_um), 5), "bandwidth_um": round(float(c.bandwidth_um), 5),
                    "flux": round(flux, 6), "err": round(err, 6), "mjd": round(float(c.mjd), 5),
                    "epoch": int(e), "band": f"D{c.detector}", "flagged": bool(flagged), "obs_id": c.obs_id})
    pts.sort(key=lambda p: (p["wavelength_um"], p["mjd"]))
    meta = {"flux_unit": FLUX_UNIT, "aperture_radius_px": r_ap, "aperture_radius_arcsec": round(r_ap * 6.15, 2),
            "background_annulus_px": [r_in, r_out], "aperture_corrected": False,
            "method": "per-exposure circular aperture on the L2 image (MJy/sr x pixel solid angle), "
                      "sigma-clipped annulus background; flagged = any non-informational FLAGS bit in the aperture "
                      "(e.g. saturated core), err from VARIANCE + background error",
            "n_points": len(pts), "n_flagged": int(sum(p["flagged"] for p in pts))}
    return {"meta": meta, "points": pts}


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
