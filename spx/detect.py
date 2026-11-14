"""Automatic candidate finder on produced frames (v1.2 F2). Classical detector, no machine learning.

load_field(path) -> field dict          reads {key}/ (manifest.json, {band}.json, {epoch}_{band}.f32 + _weight,
                                         _flagfrac, gaia.json); the gallery (web/data/<key>/) and the server
                                         (cache/fields/<v>/<key>/) use the same layout. No download.
find_candidates(field, bands=None, params=None) -> {method, params, bands_searched, candidates[], rejected{},
                                                     validation[]}   (PLAN.md section 17, F2)

Method, per band (low-coverage epochs are left out):
  1. Difference images. Template = mean of the epoch frames weighted by 1/noise^2; R_e = frame_e - template, so
     everything constant on the sky cancels. Pixels that are +inf (saturated) in any epoch, grown by sat_grow_px,
     are masked, as are pixels without data in any epoch.
  2. Noise. Per pixel: s_e^2 * (median weight / weight) (weight = number of exposures) plus a systematic term
     (sys_frac * template)^2 for residuals of bright stars (PSF differences between epochs). s_e and sys_frac are
     measured on the frames themselves (robust scatter of R_e on faint pixels, and of R_e / template on bright
     star peaks).
  3. Matched filter. Z_e = optimal Gaussian-PSF amplitude S/N of R_e at every pixel (inverse-variance weighted;
     FWHM measured on the template), rescaled so its robust scatter is 1.
  4. Motion search (shift-and-stack). For every trial velocity on a grid (total motion over the data span up to
     max_shift_px, step shift_step_px) the Z_e maps are shifted back along the motion and summed; the largest sum
     per pixel and its velocity seed a candidate. Peaks of max_e |Z_e| seed candidates at a fixed position.
  5. Local fits on the difference images, both models with the template subtraction built in:
       mover    F * [PSF(x - p_e) - sum_k w_k PSF(x - p_k)],  p_e = p0 + v (t_e - t_ref)   (5 parameters)
       variable (a_e - sum_k w_k a_k) * PSF(x - p)                                         (n + 1 parameters)
     S/N = sqrt(chi2_null - chi2_model). mover if F > 0, motion >= min_shift_px over the span, S/N >= mover_snr
     and its AIC is lower; otherwise variable if S/N >= variable_snr.
  6. Gaia DR3 (moved to every epoch with its proper motion): a mover on a Gaia star whose fitted motion matches
     the star's proper motion is kept and labelled known (near_gaia filled). Movers on Gaia stars whose motion
     does NOT match, and whose motion residual is common to >= align_min_stars such stars, are image alignment
     residuals: dropped and counted.
Then candidates of the same kind found in several bands at the same place (and, for movers, motion) are merged.
One parameter set for every field: nothing is tuned to a target.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from astropy.wcs import WCS
from scipy.ndimage import binary_dilation, gaussian_filter, map_coordinates, maximum_filter
from scipy.optimize import least_squares

METHOD = ("difference of each epoch from the inverse-variance mean of all epochs (static sky cancels); Gaussian "
          "matched filter with a noise model from the weight maps plus a bright-star systematic; shift-and-stack "
          "over trial velocities using all epochs at once; local fits of a moving-source model and a fixed "
          "variable-source model (template subtraction built into both); Gaia DR3 proper motions label known "
          "movers; a motion common to bright stars is an alignment residual and is dropped; one threshold set "
          "for every field. Not a detection claim: candidates are for a person to inspect.")

DEFAULT_PARAMS = {
    "max_shift_px": 8.0,         # largest total motion over the data span that is searched (grid px)
    "shift_step_px": 0.5,        # velocity grid step, as total motion over the span
    "min_shift_px": 1.0,         # a mover moves at least this much over the span (grid px)
    "min_epochs_mover": 3,       # linear motion needs >= 3 epochs to be tested; 2-epoch bands: variables only
    "seed_snr": 4.5,             # stack or single-epoch peak that starts a local fit
    "mover_snr": 6.0,            # local-fit S/N for a mover
    "variable_snr": 6.0,         # local-fit S/N for a variable
    "sat_grow_px": 7,            # mask around +inf (saturated) pixels: 21" = spx.coadd.MANTLE_ARCSEC (bright wings)
    "edge_px": 5,                # no seeds this close to the frame edge
    "fit_half_px": 5,            # local fit stamp margin around the positions
    "known_match_px": 2.0,       # Gaia star within this of the candidate (at t_ref)
    "known_rate_tol_px": 1.5,    # |v - v_gaia| * span below this: motion explained by Gaia proper motion
    "align_min_stars": 3,        # a common motion residual on at least this many Gaia stars = alignment residual
    "align_tol_px": 0.75,        # ... within this (total over the span) of their median
    "extended_min_frac": 0.67,   # extended-change test only where the change exceeds this fraction of the template
                                 # amplitude (a modest change of a template point source is that source: the
                                 # Gaussian PSF under-fits bright stars' wings, so "broader" would win there)
    "halo_px": 20.0,             # 60": candidates this close to a saturated core, or to a candidate whose change is
    "halo_frac": 0.05,           # ... more than 1/halo_frac times larger (same band), are that source's halo
    "mover_residual_px": 6.0,    # a variable this close to a mover's position in any epoch is its residual
    "merge_px": 2.0,             # same candidate in two bands if this close (at t_ref)
    "merge_rate_tol_px": 1.5,    # ... and motions within this (total over the span)
}

# First lines of every result's validation: what a reader should know (numbers from tests/test_coadd_detect.py,
# handoff/results/detect_validation_v2.json: injections at S/N 12 per pass, 5 px = 15" total motion).
USER_LINES = [
    "Finds objects that move at least about 10 arcsec (about the width of a star image) over the time covered; "
    "slower motion is mostly missed.",
    "In a sparse field an object at S/N 12 per pass moving 15 arcsec was recovered in 18 of 18 injections; "
    "in a crowded field in 8 of 18.",
    "Grade A = seen in at least two independent bands, or a known Gaia star; grade B = one band only. A change "
    "seen in one band only is usually not confirmed in the neighbouring band (it is not explained by the "
    "wavelengths sampled in each pass, which differ by less than 0.1 um); treat B with caution.",
]

MJD_J2000 = 51544.5


# --------------------------------------------------------------------------- loading

def load_field(path) -> dict:
    """{"manifest", "gaia", "bands": {id: {"json", "frames": {epoch_id: {"image", "weight", "flagfrac"}}}}, "path"}.
    Missing frames and bands without status "ready" are skipped."""
    path = Path(path)
    man = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    gp = path / "gaia.json"
    gaia = json.loads(gp.read_text(encoding="utf-8")) if gp.exists() else None
    shape = tuple(int(v) for v in man["grid"]["shape"])
    bands = {}
    for b in man.get("bands", []):
        jf = path / f"{b['id']}.json"
        if b.get("status", "ready") != "ready" or not jf.exists():
            continue
        bj = json.loads(jf.read_text(encoding="utf-8"))
        frames = {}
        for e in bj.get("epochs", []):
            f = path / f"{e['id']}_{b['id']}.f32"
            if not f.exists():
                continue
            fr = {"image": np.fromfile(f, "<f4").reshape(shape)}
            for kind in ("weight", "flagfrac"):
                g = path / f"{e['id']}_{b['id']}_{kind}.f32"
                fr[kind] = np.fromfile(g, "<f4").reshape(shape) if g.exists() else None
            frames[e["id"]] = fr
        bands[b["id"]] = {"json": bj, "frames": frames}
    return {"manifest": man, "gaia": gaia, "bands": bands, "path": str(path)}


def grid_wcs(grid: dict) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype = list(grid["ctype"])
    w.wcs.crval = list(grid["crval"])
    w.wcs.crpix = list(grid["crpix"])
    w.wcs.cdelt = list(grid["cdelt"])
    return w


def gaia_xy(gaia, wcs: WCS, mjd: float):
    """Pixel positions (x, y arrays) of the Gaia rows at mjd (linear proper motion from ref_epoch)."""
    if not gaia:
        return np.zeros(0), np.zeros(0)
    ra = np.array([g["ra"] for g in gaia], float)
    dec = np.array([g["dec"] for g in gaia], float)
    pmra = np.array([g.get("pmra") or 0.0 for g in gaia], float)
    pmdec = np.array([g.get("pmdec") or 0.0 for g in gaia], float)
    ref = np.array([MJD_J2000 + (float(g.get("ref_epoch") or 2016.0) - 2000.0) * 365.25 for g in gaia])
    dt = (mjd - ref) / 365.25
    dec_t = dec + pmdec * dt / 3.6e6
    ra_t = ra + pmra * dt / 3.6e6 / np.cos(np.radians(dec))
    x, y = wcs.wcs_world2pix(ra_t, dec_t, 0)
    return np.asarray(x), np.asarray(y)


# --------------------------------------------------------------------------- per-band search

def _mad(a):
    a = a[np.isfinite(a)]
    return float(1.4826 * np.median(np.abs(a - np.median(a)))) if a.size else float("nan")


def _take(a, d, axis):
    """out[p] = a[p + d] along axis, linear interpolation (a is zero-padded, so the wrap of np.roll is harmless)."""
    i = int(math.floor(d)); f = d - i
    return (1 - f) * np.roll(a, -i, axis) + f * np.roll(a, -i - 1, axis)


def _psf(xx, yy, x, y, s):
    return np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * s * s))


def _fwhm_px(img, good):
    from spx.photometry import measure_fwhm
    try:
        f, _ = measure_fwhm(np.where(good, img, np.nan))
    except Exception:
        f = float("nan")
    return float(f) if np.isfinite(f) and 1.2 < f < 6 else 2.3


class _Band:
    """Difference images, noise model and Z maps of one band."""

    def __init__(self, bid, bdata, p):
        self.bid = bid
        man_eps = bdata["json"]["epochs"]
        self.low = [e["id"] for e in man_eps if e.get("quality") == "low" and e["id"] in bdata["frames"]]
        eps = sorted([e for e in man_eps if e["id"] in bdata["frames"] and e.get("quality") != "low"],
                      key=lambda e: e["mjd_mid"])
        self.eps = eps
        self.n = len(eps)
        if self.n < 2:
            return
        self.mjd = np.array([e["mjd_mid"] for e in eps], float)
        t = self.mjd / 365.25
        self.t_ref_mjd = float(np.mean(self.mjd))
        self.dt = t - t.mean()
        self.span = float(t.max() - t.min())
        I = np.stack([bdata["frames"][e["id"]]["image"].astype(float) for e in eps])
        W = np.stack([(bdata["frames"][e["id"]]["weight"] if bdata["frames"][e["id"]]["weight"] is not None
                       else np.ones(I.shape[1:])).astype(float) for e in eps])
        ny, nx = I.shape[1:]
        sat = binary_dilation(np.isposinf(I).any(0), iterations=int(p["sat_grow_px"]))
        good = np.isfinite(I).all(0) & (W > 0).all(0) & ~sat
        self.sat_px = int(np.isposinf(I).any(0).sum())
        satcore = np.isposinf(I).any(0)
        if satcore.any():  # distance (px) to the nearest saturated pixel
            from scipy.ndimage import distance_transform_edt
            self.sat_dist = distance_transform_edt(~satcore)
        else:
            self.sat_dist = None
        self.good = good
        I0 = np.where(good, I, 0.0)
        noise = np.array([float(e.get("noise") or 1.0) for e in eps])
        om = 1.0 / noise ** 2
        self.om = om / om.sum()
        tmpl = np.tensordot(self.om, I0, axes=1)
        R = np.where(good, I0 - tmpl, 0.0)
        wmed = np.array([np.median(W[k][good]) if good.any() else 1.0 for k in range(self.n)])
        wrel = np.where(good, W / wmed[:, None, None], 1.0)
        tpos = np.clip(tmpl, 0, None)
        # white noise per epoch: robust scatter of R_e*sqrt(w_rel) on faint pixels
        faint = good & (tpos < 3 * _mad(tmpl[good]) + np.median(tmpl[good]))
        s = np.array([_mad((R[k] * np.sqrt(wrel[k]))[faint]) for k in range(self.n)])
        # systematic fraction: robust scatter of R_e / template at bright local maxima of the template
        self.fwhm = _fwhm_px(tmpl, good)
        sg = self.fwhm / 2.3548
        self.sigma = sg
        peaks = good & (tmpl == maximum_filter(tmpl, size=5)) & (tmpl > 30 * np.median(s))
        ratios = [R[k][peaks] / tmpl[peaks] for k in range(self.n)]
        rr = np.concatenate(ratios) if peaks.any() else np.zeros(0)
        self.sys_frac = float(_mad(rr)) if rr.size >= 10 else 0.0
        # the same on PSF amplitudes (coherent over the PSF): matched-filter amplitude of R_e / that of the template
        tamp = 2.0 * gaussian_filter(np.where(good, tmpl, 0.0), sg, mode="constant")
        ra = [2.0 * gaussian_filter(R[k], sg, mode="constant")[peaks] / tamp[peaks] for k in range(self.n)]
        ra = np.concatenate(ra) if peaks.any() else np.zeros(0)
        self.sys_amp_frac = float(_mad(ra)) if ra.size >= 10 else 0.0
        self.n_sys_stars = int(peaks.sum())
        var = s[:, None, None] ** 2 / wrel + (self.sys_frac * tpos[None]) ** 2
        ivar = np.where(good[None], 1.0 / var, 0.0)
        self.R, self.var, self.tmpl, self.s = R, var, tmpl, s
        self.tamp = tamp  # PSF amplitude of the template
        # optimal amplitude S/N: num = conv(R/var, P), den = conv(1/var, P^2)
        cP = 2 * np.pi * sg * sg
        cP2 = np.pi * sg * sg
        Z = np.zeros_like(R)
        for k in range(self.n):
            num = cP * gaussian_filter(R[k] * ivar[k], sg, mode="constant")
            den = cP2 * gaussian_filter(ivar[k], sg / math.sqrt(2), mode="constant")
            Z[k] = np.where(den > 0, num / np.sqrt(np.maximum(den, 1e-30)), 0.0)
        e = int(p["edge_px"])
        inner = np.zeros_like(good)
        inner[e:ny - e, e:nx - e] = True
        self.inner = inner & good
        self.zscale = np.array([_mad(Z[k][self.inner]) for k in range(self.n)])
        self.Z = Z / self.zscale[:, None, None]
        # same empirical scale for the local fits (pixel noise is correlated: native 6.15" pixels on a 3" grid)
        self.var = self.var * self.zscale[:, None, None] ** 2
        self.shape = (ny, nx)

    # ----------------------------------------------------------------- seeds
    def seeds(self, p):
        ny, nx = self.shape
        seeds = []
        if self.n < int(p["min_epochs_mover"]):
            self.stack = None
            return self._epoch_seeds(p, seeds)
        step = p["shift_step_px"] / self.span
        vmax = p["max_shift_px"] / self.span
        grid = np.arange(-vmax, vmax + 1e-9, step)
        best = np.full((ny, nx), -np.inf)
        bvx = np.zeros((ny, nx)); bvy = np.zeros((ny, nx))
        pad = int(math.ceil(vmax * np.max(np.abs(self.dt)))) + 2
        Zp = [np.pad(self.Z[k], pad) for k in range(self.n)]  # Z = 0 outside the data: such epochs only lower the sum
        X = [[_take(Zp[k], vx * self.dt[k], 1) for vx in grid] for k in range(self.n)]  # out(p) = Z(p + v dt)
        for jx, vx in enumerate(grid):
            for vy in grid:
                if math.hypot(vx, vy) * self.span < p["min_shift_px"]:
                    continue
                T = sum(_take(X[k][jx], vy * self.dt[k], 0) for k in range(self.n))[pad:pad + ny, pad:pad + nx]
                T = np.where(self.inner, T / math.sqrt(self.n), -np.inf)
                upd = T > best
                best[upd] = T[upd]; bvx[upd] = vx; bvy[upd] = vy
        self.stack = best
        pk = (best == maximum_filter(best, size=5)) & np.isfinite(best) & (best >= p["seed_snr"])
        for y, x in zip(*np.nonzero(pk)):
            seeds.append({"x": float(x), "y": float(y), "vx": float(bvx[y, x]), "vy": float(bvy[y, x]),
                          "seed": "stack", "seed_snr": float(best[y, x])})
        return self._epoch_seeds(p, seeds)

    def _epoch_seeds(self, p, seeds):
        V = np.where(self.inner, np.abs(self.Z).max(0), 0.0)
        pk = (V == maximum_filter(V, size=5)) & (V >= p["seed_snr"]) & self.inner
        for y, x in zip(*np.nonzero(pk)):
            seeds.append({"x": float(x), "y": float(y), "vx": 0.0, "vy": 0.0, "seed": "epoch", "seed_snr": float(V[y, x])})
        return seeds

    # ----------------------------------------------------------------- local fits
    def fit(self, sd, p):
        ny, nx = self.shape
        h = int(p["fit_half_px"])
        xs = [sd["x"] + sd["vx"] * d for d in self.dt]; ys = [sd["y"] + sd["vy"] * d for d in self.dt]
        x0, x1 = max(0, int(min(xs)) - h), min(nx, int(max(xs)) + h + 2)
        y0, y1 = max(0, int(min(ys)) - h), min(ny, int(max(ys)) + h + 2)
        yy, xx = np.mgrid[y0:y1, x0:x1]
        m = self.good[y0:y1, x0:x1]
        if m.sum() < 20:
            return None
        X, Y = xx[m].astype(float), yy[m].astype(float)
        D = [self.R[k][y0:y1, x0:x1][m] for k in range(self.n)]
        S = [np.sqrt(self.var[k][y0:y1, x0:x1][m]) for k in range(self.n)]
        sg, om, dt, n = self.sigma, self.om, self.dt, self.n
        chi0 = float(sum(np.sum((D[k] / S[k]) ** 2) for k in range(n)))
        vlim = 1.2 * p["max_shift_px"] / self.span

        def res_m(q):
            P = [_psf(X, Y, q[0] + q[2] * dt[k], q[1] + q[3] * dt[k], sg) for k in range(n)]
            Pm = sum(om[k] * P[k] for k in range(n))
            return np.concatenate([(D[k] - q[4] * (P[k] - Pm)) / S[k] for k in range(n)])

        def bvals(q):
            b = list(q[2:])
            b.append(-sum(om[k] * b[k] for k in range(n - 1)) / om[n - 1])
            return b

        def res_v(q, s_=sg):
            P = _psf(X, Y, q[0], q[1], s_)
            b = bvals(q)
            return np.concatenate([(D[k] - b[k] * P) / S[k] for k in range(n)])

        out = {"chi0": chi0}
        try:
            amp0 = max(float(np.max(D[int(np.argmax([d.max() for d in D]))])), 1e-6)
            q0 = [sd["x"], sd["y"], sd["vx"], sd["vy"], amp0]
            lo = [sd["x"] - 2, sd["y"] - 2, -vlim, -vlim, -np.inf]
            hi = [sd["x"] + 2, sd["y"] + 2, vlim, vlim, np.inf]
            rm = least_squares(res_m, q0, bounds=(lo, hi))
            out["mover"] = {"q": rm.x, "chi2": float(np.sum(rm.fun ** 2)), "k": 5}
        except Exception:
            out["mover"] = None
        try:
            Pc = _psf(X, Y, sd["x"], sd["y"], sg)
            b0 = [float(np.sum(D[k] * Pc) / np.sum(Pc * Pc)) for k in range(n - 1)]
            rv = least_squares(res_v, [sd["x"], sd["y"]] + b0,
                               bounds=([sd["x"] - 2, sd["y"] - 2] + [-np.inf] * (n - 1),
                                       [sd["x"] + 2, sd["y"] + 2] + [np.inf] * (n - 1)))
            out["variable"] = {"q": rv.x, "b": bvals(rv.x), "chi2": float(np.sum(rv.fun ** 2)), "k": n + 1}
            rb = least_squares(lambda q: res_v(q, 2 * sg), rv.x)  # same change, twice as wide
            out["variable"]["chi2_broad"] = float(np.sum(rb.fun ** 2))
        except Exception:
            out["variable"] = None
        return out

    def amp_at(self, x, y):
        return max(0.0, float(map_coordinates(self.tamp, [[y], [x]], order=1, mode="nearest")[0]))

    def z_at(self, k, x, y):
        return float(map_coordinates(self.Z[k], [[y], [x]], order=1, mode="constant", cval=0.0)[0])


def _classify(B: _Band, sd, fit, p):
    """(kind or None, info dict, reject reason or None)."""
    chi0 = fit["chi0"]
    mv, vr = fit.get("mover"), fit.get("variable")
    snr_m = math.sqrt(max(chi0 - mv["chi2"], 0.0)) if mv else 0.0
    snr_v = math.sqrt(max(chi0 - vr["chi2"], 0.0)) if vr else 0.0
    aic_m = mv["chi2"] + 2 * mv["k"] if mv else np.inf
    aic_v = vr["chi2"] + 2 * vr["k"] if vr else np.inf
    if mv is not None:
        x, y, vx, vy, F = mv["q"]
        shift = math.hypot(vx, vy) * B.span
        snr_m = _with_sys(abs(F), snr_m, B.sys_amp_frac * B.amp_at(x, y))
        if B.n >= p["min_epochs_mover"] and F > 0 and shift >= p["min_shift_px"] and snr_m >= p["mover_snr"] \
                and aic_m < aic_v and max(abs(vx), abs(vy)) * B.span > p["max_shift_px"] - p["shift_step_px"]:
            return None, None, "rate_at_search_limit"
        if B.n >= p["min_epochs_mover"] and F > 0 and shift >= p["min_shift_px"] and snr_m >= p["mover_snr"] and aic_m < aic_v:
            return "mover", {"x": x, "y": y, "vx": vx, "vy": vy, "F": F, "snr": snr_m, "snr_variable": snr_v,
                             "shift_px": shift}, None
    if vr is not None:
        x, y = vr["q"][:2]
        snr_v = _with_sys(max(abs(b) for b in vr["b"]), snr_v, B.sys_amp_frac * B.amp_at(x, y))
    if vr is not None and snr_v >= p["variable_snr"]:
        bmax = max(abs(b) for b in vr["b"])
        if vr.get("chi2_broad", np.inf) < vr["chi2"] and bmax > p["extended_min_frac"] * B.amp_at(x, y):
            return None, None, "extended_change"
        return "variable", {"x": x, "y": y, "vx": 0.0, "vy": 0.0, "b": vr["b"], "snr": snr_v, "snr_mover": snr_m,
                            "shift_px": 0.0}, None
    if mv is not None and snr_m >= p["mover_snr"] and aic_m < aic_v and mv["q"][4] > 0:
        return None, None, "motion_below_min_shift"
    return None, None, "below_threshold"


def _with_sys(amp, snr, sys_amp):
    """S/N with a coherent systematic: a bright star's PSF-scale residual (sys_frac x its template amplitude)
    added in quadrature to the statistical error amp/snr."""
    if snr <= 0 or amp <= 0:
        return 0.0
    return float(amp / math.sqrt((amp / snr) ** 2 + max(sys_amp, 0.0) ** 2))


def _search_band(bid, bdata, wcs, gaia, p, rejected, validation):
    B = _Band(bid, bdata, p)
    if B.n < 2:
        validation.append(f"{bid}: fewer than 2 usable epochs (low-coverage epochs {B.low} left out); not searched")
        return [], None
    seeds = B.seeds(p)
    cands = []
    for sd in seeds:
        if B.sat_dist is not None and B.sat_dist[int(round(sd["y"])), int(round(sd["x"]))] <= p["halo_px"]:
            rejected["saturated_halo"] = rejected.get("saturated_halo", 0) + 1  # not fitted
            continue
        f = B.fit(sd, p)
        if f is None:
            rejected["fit_failed"] = rejected.get("fit_failed", 0) + 1
            continue
        kind, info, why = _classify(B, sd, f, p)
        if kind is None:
            rejected[why] = rejected.get(why, 0) + 1
            continue
        info["kind"] = kind
        cands.append(info)
    # duplicates (several seeds converging on one source): keep the highest S/N per kind
    cands.sort(key=lambda c: -c["snr"])
    kept = []
    for c in cands:
        if any(k["kind"] == c["kind"] and math.hypot(k["x"] - c["x"], k["y"] - c["y"]) < 1.5 for k in kept):
            continue
        if c["kind"] == "variable" and any(k["kind"] == "mover" and _near_track(B, k, c["x"], c["y"], 1.5) for k in kept):
            continue  # the fixed-position echo of a mover
        kept.append(c)
    # halos: near a saturated core, or a much weaker change near a much stronger candidate (same band)
    for c in kept:
        c["amp"] = abs(c["F"]) if c["kind"] == "mover" else max(abs(b) for b in c["b"])
    out_k = []
    for c in sorted(kept, key=lambda c: -c["amp"]):
        if B.sat_dist is not None and B.sat_dist[int(round(c["y"])), int(round(c["x"]))] <= p["halo_px"]:
            rejected["saturated_halo"] = rejected.get("saturated_halo", 0) + 1
            continue
        if any(math.hypot(k["x"] - c["x"], k["y"] - c["y"]) <= p["halo_px"] and c["amp"] < p["halo_frac"] * k["amp"]
               for k in out_k):
            rejected["halo_of_stronger_candidate"] = rejected.get("halo_of_stronger_candidate", 0) + 1
            continue
        out_k.append(c)
    kept = sorted(out_k, key=lambda c: -c["snr"])
    # Gaia association, known movers, alignment residuals
    gx, gy = gaia_xy(gaia, wcs, B.t_ref_mjd)
    gx1, gy1 = gaia_xy(gaia, wcs, B.t_ref_mjd + 365.25)
    gvx, gvy = gx1 - gx, gy1 - gy   # px / yr
    mism = []
    for c in kept:
        c["near_gaia"] = None
        c["known"] = False
        if len(gx):
            d = np.hypot(gx - c["x"], gy - c["y"])
            j = int(np.argmin(d))
            if d[j] <= p["known_match_px"]:
                g = gaia[j]
                c["near_gaia"] = {"source_id": str(g.get("source_id")) if g.get("source_id") is not None else None,
                                  "sep_arcsec": round(float(d[j]) * abs(wcs.wcs.cdelt[1]) * 3600, 2),
                                  "g_mag": round(float(g["g_mag"]), 2) if g.get("g_mag") is not None else None}
                if c["kind"] == "mover":
                    dv = (c["vx"] - gvx[j], c["vy"] - gvy[j])
                    if math.hypot(*dv) * B.span <= p["known_rate_tol_px"]:
                        c["known"] = True
                    else:
                        c["dv"] = dv
                        mism.append(c)
    drop = set()
    if len(mism) >= p["align_min_stars"]:
        mx = float(np.median([c["dv"][0] for c in mism])); my = float(np.median([c["dv"][1] for c in mism]))
        grp = [c for c in mism if math.hypot(c["dv"][0] - mx, c["dv"][1] - my) * B.span <= p["align_tol_px"]]
        if len(grp) >= p["align_min_stars"]:
            drop = {id(c) for c in grp}
            rejected["alignment_residual"] = rejected.get("alignment_residual", 0) + len(grp)
            validation.append(f"{bid}: {len(grp)} movers on Gaia stars share one apparent motion "
                              f"({mx * B.span:+.2f}, {my * B.span:+.2f}) px over the span: alignment residual, dropped")
    out = []
    for c in kept:
        if id(c) in drop:
            continue
        c["band"] = bid
        c["epochs"] = [{"epoch": e["id"], "mjd": float(B.mjd[k]),
                        "x": c["x"] + c["vx"] * B.dt[k], "y": c["y"] + c["vy"] * B.dt[k],
                        "snr": B.z_at(k, c["x"] + c["vx"] * B.dt[k], c["y"] + c["vy"] * B.dt[k])}
                       for k, e in enumerate(B.eps)]
        c["t_ref_mjd"] = B.t_ref_mjd
        c["span_yr"] = B.span
        out.append(c)
    validation.append(f"{bid}: epochs {[e['id'] for e in B.eps]}" + (f" (low-coverage {B.low} left out)" if B.low else "")
                      + f", span {B.span:.2f} yr, FWHM {B.fwhm:.2f} px, noise rescale "
                      f"{', '.join(f'{z:.2f}' for z in B.zscale)}, bright-star systematic {B.sys_frac:.3f} (pixel) / {B.sys_amp_frac:.3f} (PSF amplitude) "
                      f"({B.n_sys_stars} peaks), saturated px {B.sat_px}, masked {int((~B.good).sum())} px, "
                      f"{len(seeds)} seeds, {sum(c['kind'] == 'mover' for c in out)} movers, "
                      f"{sum(c['kind'] == 'variable' for c in out)} variables")
    return out, B


def _near_track(B, m, x, y, r):
    return any(math.hypot(m["x"] + m["vx"] * d - x, m["y"] + m["vy"] * d - y) < r for d in B.dt)


# --------------------------------------------------------------------------- field level

def n_independent_bands(bands, waves) -> int:
    """Number of independent bands: bands whose wavelength ranges overlap (e.g. W 4.3-5.0 um with D5 and D6) share
    exposures and count once."""
    groups = []
    for b in dict.fromkeys(bands):
        w = waves.get(b)
        hit = [g for g in groups if w is not None and any(waves.get(o) is not None and w[0] < waves[o][1] and waves[o][0] < w[1]
                                                        for o in g)]
        merged = {b}.union(*hit) if hit else {b}
        groups = [g for g in groups if g not in hit] + [merged]
    return len(groups)


def find_candidates(field: dict, bands: list[str] | None = None, params: dict | None = None) -> dict:
    p = dict(DEFAULT_PARAMS)
    if params:
        p.update(params)
    man = field["manifest"]
    wcs = grid_wcs(man["grid"])
    pix_arcsec = abs(float(man["grid"]["cdelt"][1])) * 3600
    if bands is None:
        bands = [b for b in field["bands"] if not b.startswith("w")]
    rejected, validation, per_band = {}, [], []
    searched = []
    for bid in bands:
        if bid not in field["bands"]:
            continue
        c, B = _search_band(bid, field["bands"][bid], wcs, field.get("gaia"), p, rejected, validation)
        if B is not None and B.n >= 2:
            searched.append(bid)
        per_band.extend(c)
    # a weaker mover sharing an epoch position with a stronger one (any band) is its residual
    movers = sorted([c for c in per_band if c["kind"] == "mover"], key=lambda c: -c["snr"])
    strong = []
    for c in movers:
        pos = {e["epoch"]: (e["x"], e["y"]) for e in c["epochs"]}
        if any(math.hypot(pos[e["epoch"]][0] - e["x"], pos[e["epoch"]][1] - e["y"]) <= p["merge_px"] * 1.5
               and math.hypot(h["vx"] - c["vx"], h["vy"] - c["vy"]) * max(h["span_yr"], c["span_yr"]) > p["merge_rate_tol_px"]
               for h in strong for e in h["epochs"] if e["epoch"] in pos):
            rejected["mover_residual"] = rejected.get("mover_residual", 0) + 1
            c["_drop"] = True
            continue
        strong.append(c)
    per_band = [c for c in per_band if not c.get("_drop")]
    # a variable on any band's mover track is that mover's residual
    track = [(e["x"], e["y"]) for c in per_band if c["kind"] == "mover" for e in c["epochs"]]
    keep = []
    for c in per_band:
        if c["kind"] == "variable" and any(math.hypot(c["x"] - x, c["y"] - y) <= p["mover_residual_px"] for x, y in track):
            rejected["mover_residual"] = rejected.get("mover_residual", 0) + 1
            continue
        keep.append(c)
    per_band = keep
    # merge the same candidate across bands (compare at the field's reference time)
    t0 = float(np.median([c["t_ref_mjd"] for c in per_band])) if per_band else 0.0
    for c in per_band:
        d = (t0 - c["t_ref_mjd"]) / 365.25
        c["x0"], c["y0"] = c["x"] + c["vx"] * d, c["y"] + c["vy"] * d
    per_band.sort(key=lambda c: -c["snr"])
    groups = []
    for c in per_band:
        for g in groups:
            h = g[0]
            span = max(h["span_yr"], c["span_yr"])
            if (h["kind"] == c["kind"] and math.hypot(h["x0"] - c["x0"], h["y0"] - c["y0"]) <= p["merge_px"]
                    and math.hypot(h["vx"] - c["vx"], h["vy"] - c["vy"]) * span <= p["merge_rate_tol_px"]):
                g.append(c)
                break
        else:
            groups.append([c])
    cands = []
    cd1, cd2 = float(man["grid"]["cdelt"][0]) * 3600, float(man["grid"]["cdelt"][1]) * 3600
    waves = {b["id"]: b.get("wave_um") for b in man.get("bands", [])}
    for i, g in enumerate(groups):
        h = g[0]
        pos = []
        for e in h["epochs"]:
            ra, dec = wcs.wcs_pix2world([e["x"]], [e["y"]], 0)
            pos.append({"epoch": e["epoch"], "ra": round(float(ra[0]), 6), "dec": round(float(dec[0]), 6),
                        "snr": round(e["snr"], 1)})
        notes = [f"{c['band']}: S/N {c['snr']:.1f}" for c in g]
        if h["kind"] == "mover":
            notes.append(f"motion {h['shift_px'] * pix_arcsec:.1f} arcsec over {h['span_yr']:.2f} yr")
            if h["known"]:
                notes.append("known: motion matches the Gaia DR3 proper motion of the star at this position")
            elif h["near_gaia"]:
                notes.append("on a Gaia DR3 star whose proper motion does not explain the motion")
        if h["kind"] == "variable" and h["near_gaia"]:
            notes.append("at a Gaia DR3 star")
        nb = n_independent_bands([c["band"] for c in g], waves)
        cands.append({
            "id": i + 1, "kind": h["kind"], "known": bool(h.get("known")), "snr": round(h["snr"], 1),
            "n_bands": nb, "grade": "A" if (nb >= 2 or h.get("known")) else "B",
            "bands": [c["band"] for c in g], "positions": pos,
            "rate_arcsec_per_year": ([round(h["vx"] * cd1, 2), round(h["vy"] * cd2, 2)] if h["kind"] == "mover" else None),
            "near_gaia": h["near_gaia"], "notes": notes})
    cands.sort(key=lambda c: (c["grade"], -c["snr"]))
    for i, c in enumerate(cands):
        c["id"] = i + 1
    return {"method": METHOD, "params": p, "bands_searched": searched, "candidates": cands,
            "rejected": rejected, "validation": USER_LINES + validation}


if __name__ == "__main__":
    import sys
    import time
    for arg in sys.argv[1:]:
        t = time.time()
        res = find_candidates(load_field(arg))
        print(f"== {arg}: {len(res['candidates'])} candidates in {time.time() - t:.1f} s; rejected {res['rejected']}")
        for line in res["validation"]:
            print("  ", line)
        for c in res["candidates"]:
            print(f"   #{c['id']} {c['grade']} n{c['n_bands']} {c['kind']}{' (known)' if c['known'] else ''} S/N {c['snr']} bands {c['bands']} "
                  f"rate {c['rate_arcsec_per_year']} gaia {c['near_gaia']} first {c['positions'][0]}")
