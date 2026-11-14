"""SPHEREx cutouts -> per-epoch, per-band co-added frames on ONE fixed sky grid.

Input: cutout objects from spx.access.fetch_cutouts (image, flags, variance, [zodi], wcs, mjd,
detector, obs_id, wavelength_um = LVF wavelength at the target pixel). Output: Frame objects
(float32 MJy/sr images + coverage/flag maps) that all share one Grid, so a blink between them
shows only real motion and real brightness change.

Quality steps, in order (each one is measured in handoff/results/coadd_metrics_v2.csv):
  1. FLAGS mask: transient/overflow/hot/cold/persistence/outlier/crosstalk/ghost/halo... -> NaN.
     SOURCE (bit 21) and FULLSAMPLE (bit 12) are informational and are NOT masked.
  2. Local background: sigma-clipped median of unflagged, non-SOURCE pixels in each cutout
     (after subtracting ZODI when the cutout carries it).
  3. Per-exposure robust noise sigma -> weight 1/sigma^2.
  4. Mask-aware bilinear resampling onto the common TAN grid (data*valid and valid resampled
     separately), so masked pixels leave holes instead of dragging zeros into neighbours.
  5. Per-pixel clipped, weighted mean over all exposures of the epoch in the band. The clip
     threshold has a term proportional to |median|: exposures sample different wavelengths (LVF),
     so real spectral differences are kept; single-exposure hits are rejected.
  6. Saturated cores (no clean sample; every exposure flagged OVERFLOW/BLOOM/TRANSIENT...) become +inf
     (contract: drawn at the brightest colour, labelled "saturated"); NaN only ever means "no data".
     saturated="fill" puts the soft-masked median there instead. Frame.filled marks those pixels.
  7. Display: one asinh scale shared by all epochs of a band (no blink flicker).

    frames = build_frames(cutouts, target, fov_arcmin=7)      # per detector D1..D6 by default
    scales = common_scale(frames)                              # {band: (vmin, vmax, soft)}
    imgs8  = stretch(frames, scales)                           # uint8, same order as frames
    diffs  = difference(frames)                                # epoch minus the other epochs
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
from astropy.coordinates import SkyCoord
from astropy.stats import sigma_clipped_stats
from astropy.time import Time
from astropy.wcs import WCS
from scipy.ndimage import gaussian_filter, map_coordinates

PIX_ARCSEC = 6.15  # native SPHEREx pixel

# FLAGS planes, from the MSKNnnnn cards of the FLAGS header (DR1, l2b-v27)
FLAG_BITS = {
    "TRANSIENT": 0, "OVERFLOW": 1, "SUR_ERROR": 2, "NONFUNC": 6, "DICHROIC": 7, "MISSING_DATA": 9,
    "HOT": 10, "COLD": 11, "FULLSAMPLE": 12, "PHANMISS": 14, "NONLINEAR": 15, "PERSIST": 17,
    "OUTLIER": 19, "CROSSTALK": 20, "SOURCE": 21, "GHOST": 22, "GHOST_FPA": 23, "GHOST_EXT": 24,
    "STREAK": 25, "BLOOM": 26, "SNOWBALL": 27, "HALO": 28,
}
INFO_ONLY = ("SOURCE", "FULLSAMPLE")
BAD_MASK = sum(1 << b for n, b in FLAG_BITS.items() if n not in INFO_ONLY)
# pixels that are never usable, not even to fill a saturated core
HARD_MASK = sum(1 << FLAG_BITS[n] for n in ("NONFUNC", "MISSING_DATA", "HOT", "COLD", "PHANMISS", "DICHROIC"))
SOURCE_MASK = 1 << FLAG_BITS["SOURCE"]
SATURATION_MASK = sum(1 << FLAG_BITS[n] for n in ("OVERFLOW", "NONLINEAR", "BLOOM"))

DETECTOR_BANDS_UM = {1: (0.75, 1.11), 2: (1.10, 1.66), 3: (1.63, 2.42), 4: (2.42, 3.83),
                     5: (3.80, 4.43), 6: (4.41, 5.01)}  # measured LVF range at the target pixel


# --------------------------------------------------------------------------- grid / target / epochs

@dataclass
class Grid:
    wcs: WCS
    shape: tuple  # (ny, nx)
    center: SkyCoord
    pix_arcsec: float

    def to_manifest(self):
        w = self.wcs.wcs
        return {"ctype": list(w.ctype), "crval": [float(v) for v in w.crval], "crpix": [float(v) for v in w.crpix],
                "cdelt": [float(v) for v in w.cdelt], "shape": list(self.shape)}


def make_grid(center: SkyCoord, fov_arcmin: float = 7.0, pix_arcsec: float = 3.0) -> Grid:
    """North-up, east-left TAN grid centred on `center`."""
    n = int(np.ceil(fov_arcmin * 60 / pix_arcsec))
    n += n % 2  # even -> centre falls between pixels (n/2 - 0.5)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [center.icrs.ra.deg, center.icrs.dec.deg]
    w.wcs.crpix = [(n + 1) / 2] * 2
    w.wcs.cdelt = [-pix_arcsec / 3600, pix_arcsec / 3600]
    return Grid(w, (n, n), center.icrs, pix_arcsec)


def half_px_for(fov_arcmin: float) -> int:
    """Cutout half size (native px) that covers the grid for any detector rotation."""
    return int(np.ceil(fov_arcmin * 60 / 2 / PIX_ARCSEC * np.sqrt(2))) + 4


def target_at(target: SkyCoord, mjd: float) -> SkyCoord:
    """Target position at `mjd`, propagated with its proper motion when it has one."""
    try:
        has_pm = target.obstime is not None and target.pm_ra_cosdec is not None
    except (AttributeError, TypeError):
        has_pm = False
    if not has_pm:
        return SkyCoord(target.icrs.ra, target.icrs.dec, frame="icrs")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # ErfaWarning for missing distance
        c = target.apply_space_motion(new_obstime=Time(mjd, format="mjd"))
    return SkyCoord(c.icrs.ra, c.icrs.dec, frame="icrs")


def split_epochs(mjd, gap_days: float = 30.0, max_epoch_days: float | None = None) -> np.ndarray:
    """Epoch index per exposure (0, 1, ...), in time order. THE single epoch definition.

    A gap longer than gap_days starts a new survey epoch (normal epochs span 1-55 days).
    Near the ecliptic poles (e.g. R Dra) visits are almost continuous and an "epoch" can span
    months; max_epoch_days then cuts each epoch into equal-length chunks no longer than that.
    Call it on ALL exposures of the field (every detector), never on one band's subset.
    """
    mjd = np.asarray(mjd, float)
    if not len(mjd):
        return np.zeros(0, int)
    order = np.argsort(mjd)
    coarse = np.concatenate([[0], np.cumsum(np.diff(mjd[order]) > gap_days)])
    out, nxt = np.empty(len(mjd), int), 0
    for e in range(coarse.max() + 1):
        sel = order[coarse == e]
        t = mjd[sel]
        span = t.max() - t.min()
        nchunk = 1 if not max_epoch_days else max(1, int(np.ceil(span / max_epoch_days)))
        sub = np.minimum(((t - t.min()) / (span / nchunk + 1e-9)).astype(int), nchunk - 1)
        used = np.unique(sub)
        out[sel] = nxt + np.searchsorted(used, sub)
        nxt += len(used)
    return out


def epoch_table(mjd, labels) -> list[dict]:
    """[{id, mjd_min, mjd_max, mjd_mid, date}] for the manifest; pass it back as build_frames(epochs=...)."""
    mjd, labels = np.asarray(mjd, float), np.asarray(labels)
    out = []
    for e in np.unique(labels):
        t = mjd[labels == e]
        out.append({"id": int(e), "mjd_min": float(t.min()), "mjd_max": float(t.max()),
                    "mjd_mid": float(np.median(t)), "date": Time(np.median(t), format="mjd").iso[:10]})
    return out


def assign_epochs(mjd, epochs, tol_days: float = 0.5) -> np.ndarray:
    """Epoch id for each mjd from an epoch_table (or its manifest copy); -1 if none matches."""
    mjd = np.asarray(mjd, float)
    lab = np.full(len(mjd), -1, int)
    for e in epochs:
        lab[(mjd >= e["mjd_min"] - tol_days) & (mjd <= e["mjd_max"] + tol_days)] = e["id"]
    return lab


def select_spread(mjd, n: int) -> np.ndarray:
    """Indices of at most n exposures spread evenly in time (cap for dense fields near the poles)."""
    mjd = np.asarray(mjd, float)
    if n is None or len(mjd) <= n:
        return np.arange(len(mjd))
    order = np.argsort(mjd)
    return np.sort(order[np.unique(np.round(np.linspace(0, len(mjd) - 1, n)).astype(int))])


# --------------------------------------------------------------------------- per exposure

def prepare(cut, min_good: int = 200):
    """Mask, subtract local background. Returns (clean, soft, sigma, satmask) or None.

    clean: all BAD_MASK pixels NaN. soft: only HARD_MASK pixels NaN (used only to fill
    saturated cores when saturated="fill"). satmask: float 1 where a saturation-type flag is set.
    """
    img = np.asarray(cut.image, float)
    zodi = getattr(cut, "zodi", None)
    if zodi is not None:
        img = img - np.asarray(zodi, float)
    flags = np.asarray(cut.flags)
    finite = np.isfinite(img)
    var = getattr(cut, "variance", None)
    if var is not None:
        var = np.asarray(var, float)
        finite &= np.isfinite(var) & (var > 0)
    good = finite & ((flags & BAD_MASK) == 0)
    bg = good & ((flags & SOURCE_MASK) == 0)
    if bg.sum() < min_good:
        bg = good
    if bg.sum() < min_good:
        return None
    _, med, std = sigma_clipped_stats(img[bg], sigma=3.0, maxiters=5, stdfunc="mad_std")
    if not np.isfinite(std) or std <= 0:
        return None
    d = img - med
    soft = np.where(np.isfinite(img) & ((flags & HARD_MASK) == 0), d, np.nan)
    d[~good] = np.nan
    sat = ((flags & SATURATION_MASK) != 0).astype(float)
    return d, soft, float(std), sat


def resample(d: np.ndarray, px: np.ndarray, py: np.ndarray, min_cov: float = 0.4):
    """Mask-aware bilinear resampling at input pixel coords (px, py).

    Returns (values, overlap, ok): overlap = inside the cutout, ok = has a valid value.
    """
    ny, nx = d.shape
    overlap = (px >= 0) & (py >= 0) & (px <= nx - 1) & (py <= ny - 1)
    g = np.isfinite(d)
    num = map_coordinates(np.where(g, d, 0.0), [py, px], order=1, cval=0.0, prefilter=False)
    den = map_coordinates(g.astype(float), [py, px], order=1, cval=0.0, prefilter=False)
    ok = overlap & (den >= min_cov)
    val = np.full(px.shape, np.nan)
    val[ok] = num[ok] / den[ok]
    return val, overlap, ok


# --------------------------------------------------------------------------- combine

def combine(stack: np.ndarray, sigma: np.ndarray, clip: float = 3.5, rel_tol: float = 0.5, iters: int = 2):
    """Per-pixel clipped, inverse-variance weighted mean along axis 0 (NaN = no sample).

    Threshold = clip * max(per-pixel robust scatter, sample noise) + rel_tol * |median|.
    The relative term matters because exposures of one epoch sample DIFFERENT wavelengths (LVF):
    a steep-spectrum source (WISE 0855, Mira water bands) or an undersampled bright core differs
    between exposures for real and must not be clipped. Single-exposure hits (cosmic rays,
    residual bad pixels, asteroids) are far above that and still go.
    Returns (mean, sum of weights, number of kept samples).
    """
    w = (1.0 / sigma ** 2)[:, None, None]
    keep = np.isfinite(stack)
    if stack.shape[0] >= 3:
        for _ in range(iters):
            s = np.where(keep, stack, np.nan)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                med = np.nanmedian(s, axis=0)
                spread = 1.4826 * np.nanmedian(np.abs(s - med), axis=0)
            thr = clip * np.fmax(spread[None], sigma[:, None, None]) + rel_tol * np.abs(med)[None]
            keep = np.isfinite(stack) & (np.abs(stack - med[None]) <= thr)
    ww = np.where(keep, w, 0.0)
    wsum = ww.sum(axis=0)
    mean = np.where(wsum > 0, (np.where(keep, stack, 0.0) * ww).sum(axis=0) / np.maximum(wsum, 1e-30), np.nan)
    return mean, wsum, keep.sum(axis=0)


def nan_smooth(img: np.ndarray, fwhm_px: float) -> np.ndarray:
    """Gaussian smoothing that ignores NaNs (normalized convolution)."""
    if fwhm_px <= 0:
        return img
    s = fwhm_px / 2.3548
    g = np.isfinite(img)
    num = gaussian_filter(np.where(g, img, 0.0), s)
    den = gaussian_filter(g.astype(float), s)
    return np.where(den > 0.3, num / np.maximum(den, 1e-12), np.nan)


# --------------------------------------------------------------------------- frames

@dataclass
class Frame:
    image: np.ndarray          # float32 MJy/sr, background-subtracted; NaN = no coverage
    weight: np.ndarray         # float32 number of exposures that contributed (contract: weight.f32)
    flag_frac: np.ndarray      # float32 0-1, fraction of overlapping exposures masked or clipped
    ivar: np.ndarray           # float32 sum of 1/sigma^2 of accepted samples
    filled: np.ndarray         # bool, saturated core (+inf, or soft-masked median if saturated="fill")
    grid: Grid
    band: str                  # "D1".."D6" or a custom id
    epoch: int
    epoch_label: str           # median date YYYY-MM-DD
    mjd: float                 # median MJD of exposures used
    mjd_range: tuple
    detector: int | None
    band_um: tuple             # (min, max) LVF wavelength of exposures used
    n_used: int                # exposures contributing
    noise: float               # robust sigma of the frame background (MJy/sr)
    target_radec: tuple        # target (ra, dec) at this epoch
    target_xy: tuple           # target (x, y) in grid pixels (0-based)
    obs_ids: list = field(default_factory=list, repr=False)

    @property
    def key(self):
        return self.band


def build_frames(cutouts, target: SkyCoord, fov_arcmin: float = 7.0, pix_arcsec: float = 3.0,
                 band_um=None, band_id: str | None = None, per_detector: bool = True,
                 epochs=None, epoch_gap_days: float = 30.0, max_epoch_days: float | None = None,
                 max_exposures: int | None = None, clip: float = 3.5, rel_tol: float = 0.5,
                 min_exposures: int = 3, saturated: str = "inf", smooth_fwhm_arcsec: float = 0.0,
                 center: SkyCoord | None = None, verbose: bool = False) -> list[Frame]:
    """Co-add cutouts into per-epoch, per-band frames on one fixed sky grid.

    target    SkyCoord; with pm + obstime it is propagated to every epoch (target_xy / target_radec).
    center    grid centre; default = target at the median time of the cutouts. Pass the manifest
              centre so every band of a field shares exactly the same grid.
    epochs    epoch_table() of ALL exposures of the field (manifest). If None, split_epochs is run on
              the cutouts given (before any band filtering).
    band_um   (lo, hi): keep exposures whose LVF wavelength at the target falls inside; the frames get
              band = band_id (default "W<lo>-<hi>") and per_detector is ignored.
    max_exposures  cap per (epoch, band), chosen spread in time (dense fields near the poles).
    saturated "inf" (default, data contract): grid pixels covered by exposures but with no clean sample,
              where at least one exposure has a saturation-type flag (OVERFLOW/NONLINEAR/BLOOM), become +inf.
              "fill": soft-masked median instead. "nan": leave NaN.
    smooth_fwhm_arcsec  optional NaN-aware Gaussian (~7" = point-source matched filter, "detection view").
    """
    cuts = [c for c in cutouts if c is not None]
    if not cuts:
        return []
    mjd_all = np.array([c.mjd for c in cuts])
    ep_all = assign_epochs(mjd_all, epochs) if epochs is not None else split_epochs(mjd_all, epoch_gap_days, max_epoch_days)
    keep = ep_all >= 0
    if band_um is not None:
        lo, hi = band_um
        keep &= np.array([lo <= c.wavelength_um <= hi for c in cuts])
        per_detector = False
        band_id = band_id or f"W{lo:g}-{hi:g}"
    cuts = [c for c, k in zip(cuts, keep) if k]
    mjd, ep = mjd_all[keep], ep_all[keep]
    if not cuts:
        return []
    if center is None:
        center = target_at(target, float(np.median(mjd)))
    grid = make_grid(center, fov_arcmin, pix_arcsec)
    yy, xx = np.mgrid[0:grid.shape[0], 0:grid.shape[1]]
    grid_sky = grid.wcs.pixel_to_world(xx, yy)

    groups = {}
    for i, c in enumerate(cuts):
        b = f"D{c.detector}" if per_detector else (band_id or "all")
        groups.setdefault((b, int(ep[i])), []).append(i)

    frames = []
    for (band, e), idx in sorted(groups.items()):
        if max_exposures:
            idx = [idx[j] for j in select_spread(mjd[idx], max_exposures)]
        vals, softs, sats, ovl, sig, used = [], [], [], [], [], []
        for i in idx:
            p = prepare(cuts[i])
            if p is None:
                continue
            px, py = cuts[i].wcs.world_to_pixel(grid_sky)
            v, o, ok = resample(p[0], px, py)
            if not o.any():
                continue
            vals.append(v); ovl.append(o); sig.append(p[2]); used.append(i)
            if saturated != "nan":
                softs.append(resample(p[1], px, py)[0])
                sats.append(map_coordinates(p[3], [py, px], order=1, cval=0.0, prefilter=False) > 0.25)
        if len(used) < min_exposures:
            continue
        stack = np.stack(vals)
        sig = np.array(sig)
        overlap = np.sum(ovl, axis=0)
        mean, wsum, nkeep = combine(stack, sig, clip=clip, rel_tol=rel_tol)
        filled = np.zeros(grid.shape, bool)
        if saturated != "nan":
            filled = (nkeep == 0) & (overlap > 0) & np.any(sats, axis=0)
            if filled.any():
                if saturated == "fill":
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        soft_med = np.nanmedian(np.stack(softs)[:, filled], axis=0)
                    mean[filled] = np.where(np.isfinite(soft_med), soft_med, np.inf)
                else:
                    mean[filled] = np.inf
        if smooth_fwhm_arcsec > 0:
            mean = np.where(filled, mean, nan_smooth(np.where(filled, np.nan, mean), smooth_fwhm_arcsec / pix_arcsec))
        flag_frac = np.where(overlap > 0, 1.0 - nkeep / np.maximum(overlap, 1), np.nan)
        m = float(np.median(mjd[used]))
        tc = target_at(target, m)
        tx, ty = grid.wcs.world_to_pixel(tc)
        fin = mean[np.isfinite(mean)]
        noise = float(sigma_clipped_stats(fin, sigma=3, stdfunc="mad_std")[2]) if fin.size > 50 else float("nan")
        wl = np.array([cuts[i].wavelength_um for i in used])
        frames.append(Frame(
            image=mean.astype(np.float32), weight=nkeep.astype(np.float32), flag_frac=flag_frac.astype(np.float32),
            ivar=wsum.astype(np.float32), filled=filled, grid=grid, band=band, epoch=e,
            epoch_label=Time(m, format="mjd").iso[:10], mjd=m,
            mjd_range=(float(mjd[used].min()), float(mjd[used].max())),
            detector=cuts[used[0]].detector if band.startswith("D") else None,
            band_um=(float(wl.min()), float(wl.max())), n_used=len(used), noise=noise,
            target_radec=(float(tc.ra.deg), float(tc.dec.deg)), target_xy=(float(tx), float(ty)),
            obs_ids=[cuts[i].obs_id for i in used]))
        if verbose:
            print(f"{band} epoch {e} {frames[-1].epoch_label}: {len(used)} exposures, noise {noise:.4g} MJy/sr, "
                  f"median n/px {np.median(nkeep):.0f}, filled px {int(filled.sum())}")
    return frames


# --------------------------------------------------------------------------- display

def common_scale(frames, lo_sigma: float = 2.0, hi_pct: float = 99.8, soft_sigma: float = 2.0):
    """{band: (vmin, vmax, soft)} shared by all epochs of the band (no blink flicker).

    vmin = -lo_sigma*noise, vmax = high percentile over all epochs (>= 30 noise), soft = asinh knee.
    Browser formula: asinh((x - vmin)/soft) / asinh((vmax - vmin)/soft), clipped to 0..1.
    """
    out = {}
    for key in dict.fromkeys(f.key for f in frames):
        fs = [f for f in frames if f.key == key]
        noise = float(np.nanmedian([f.noise for f in fs]))
        allpix = np.concatenate([f.image[np.isfinite(f.image)] for f in fs])
        vmax = float(max(np.percentile(allpix, hi_pct), 30 * noise))
        out[key] = (-lo_sigma * noise, vmax, soft_sigma * noise)
    return out


def asinh_stretch(img: np.ndarray, vmin: float, vmax: float, soft: float) -> np.ndarray:
    """0..1 float; NaN -> 0, +inf (saturated) -> 1."""
    x = np.nan_to_num(img - vmin, nan=0.0, posinf=np.inf)
    y = np.arcsinh(np.clip(x, 0, None) / soft) / np.arcsinh((vmax - vmin) / soft)
    return np.clip(y, 0, 1)


def stretch(frames, scales=None, smooth_fwhm_arcsec: float = 0.0):
    """uint8 images (same order as frames) using common per-band scales."""
    scales = scales or common_scale(frames)
    out = []
    for f in frames:
        img = nan_smooth(f.image, smooth_fwhm_arcsec / f.grid.pix_arcsec) if smooth_fwhm_arcsec else f.image
        out.append((asinh_stretch(img, *scales[f.key]) * 255 + 0.5).astype(np.uint8))
    return out


def difference(frames):
    """Each frame minus the ivar-weighted mean of the OTHER epochs of the same band.

    Static sky cancels; a mover leaves a positive blob where it is now and negative ones where it
    was; a variable star leaves a single residual. float32 arrays, same order as frames.
    """
    out = []
    for f in frames:
        others = [g for g in frames if g.key == f.key and g.epoch != f.epoch]
        if not others:
            out.append(np.full_like(f.image, np.nan))
            continue
        num = sum(np.where(np.isfinite(g.image), g.image, 0.0) * g.ivar for g in others)
        den = sum(np.where(np.isfinite(g.image), g.ivar, 0) for g in others)
        sat = np.any([~np.isfinite(g.image) & ~np.isnan(g.image) for g in others], axis=0)
        tmpl = np.where((den > 0) & ~sat, num / np.maximum(den, 1e-30), np.nan)
        d = f.image - tmpl
        d[~np.isfinite(f.image)] = np.nan  # saturated or empty: no meaningful difference
        out.append(d.astype(np.float32))
    return out
