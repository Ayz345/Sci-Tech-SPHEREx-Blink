"""Before/after measurement table: the v0 prototype co-add (handoff/code/spherex_explore.py) vs spx.coadd.

Run:  python tests/test_coadd_metrics.py
Writes handoff/results/coadd_metrics_v2.csv and handoff/results/spherex_coadd_v2.png (same asinh scale per row).

Metrics, identical for both methods, on the same cutouts:
  n_used        exposures in the co-add
  bg_rms        robust std of the half-split difference (A-B)/2 (alternate exposures), i.e. pure noise
                without sky structure, at ~6" sampling (new 3" frames are 2x2 binned), MJy/sr
  target_snr    matched-filter peak within 6" of the target / filtered half-split noise
                (NaN with target_saturated=True when the target core is saturated: +inf in the frame)
  fwhm_arcsec   median Gaussian FWHM of the brightest isolated stars
  black_px      isolated holes per 10^4 px: pixels more than 5 x bg_rms(v0) below the median of their 3x3
                neighbours. One absolute threshold for both methods (the v0 noise of that epoch), so a
                cleaner frame is not judged by a stricter cut; unmasked dead/cold pixels make holes.
  flagged_in    mean fraction of input cutout pixels with a bad FLAGS bit (what v0 averaged in)
  flag_frac     median fraction of exposures masked/clipped per output pixel (new only)
v0 replica: first 14 exposures of the epoch whose DETECTOR em_min is in the window, global median,
NaN->0, bilinear, plain mean, 6.15"/px grid centred on the target (as in coadd_figure).
"""
from __future__ import annotations

import csv
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

import numpy as np
from astropy.stats import sigma_clipped_stats
from astropy.wcs import WCS
from scipy.ndimage import map_coordinates

from spx import access
from spx.coadd import (BAD_MASK, asinh_stretch, build_frames, common_scale, epoch_table, half_px_for, split_epochs,
                       target_at)
from spx.photometry import matched_filter, measure_fwhm
from spx.targets import TARGETS, target_coord

OUT = ROOT / "handoff" / "results"
FOV = 7.0
EM_MIN = {1: 0.75, 2: 1.10, 3: 1.63, 4: 2.40, 5: 3.81, 6: 4.41}  # SIA2 em_min per detector (v0 filter)
CASES = [  # (target, v0 window, new LVF windows)
    ("barnard", (1.0, 2.0), [(1.0, 2.0)]),
    ("wise-0855", (3.8, 5.0), [(3.8, 5.0), (4.3, 5.0)]),
]


def v0_coadd(cuts, center, lo, hi, per_epoch=14, start=0, step=1):
    pix = 6.15 / 3600
    n = int(FOV / 60 / pix)
    w = WCS(naxis=2)
    w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
    w.wcs.crval = [center.ra.deg, center.dec.deg]
    w.wcs.crpix = [n / 2 + 0.5] * 2
    w.wcs.cdelt = [-pix, pix]
    yy, xx = np.mgrid[0:n, 0:n]
    sky = w.pixel_to_world(xx, yy)
    idx = [c for c in sorted(cuts, key=lambda c: c.mjd) if lo <= EM_MIN[c.detector] <= hi][:per_epoch][start::step]
    acc, cnt = np.zeros((n, n)), np.zeros((n, n))
    for c in idx:
        d = c.image.astype(float)
        d -= np.nanmedian(d)
        px, py = c.wcs.world_to_pixel(sky)
        v = map_coordinates(np.nan_to_num(d), [py, px], order=1, cval=np.nan)
        ok = np.isfinite(v) & (px > 0) & (py > 0) & (px < d.shape[1] - 1) & (py < d.shape[0] - 1)
        acc[ok] += v[ok]
        cnt[ok] += 1
    return np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan), len(idx), w


def bin2(img):
    ny, nx = (img.shape[0] // 2) * 2, (img.shape[1] // 2) * 2
    return np.nanmean(img[:ny, :nx].reshape(ny // 2, 2, nx // 2, 2), axis=(1, 3))


def rms(img):
    f = img[np.isfinite(img)]
    return float(sigma_clipped_stats(f, sigma=4, stdfunc="mad_std")[2])


def black(img, s):
    from scipy.ndimage import generic_filter
    nb = generic_filter(np.where(np.isfinite(img), img, np.nan), lambda v: np.nanmedian(np.delete(v, 4)), size=3,
                        mode="constant", cval=np.nan)
    ok = np.isfinite(img) & np.isfinite(nb)
    return 1e4 * float(np.mean((img - nb)[ok] < -5 * s))


def saturated_at(img, xy, pix_arcsec):
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    return bool(np.isposinf(img[np.hypot(xx - xy[0], yy - xy[1]) <= 6.0 / pix_arcsec]).any())


def snr_at(img, noise_img, xy, pix_arcsec, fwhm_arcsec):
    if saturated_at(img, xy, pix_arcsec):
        return float("nan")
    fw = fwhm_arcsec / pix_arcsec
    f, fn = matched_filter(img, fw), matched_filter(noise_img, fw)
    sig = rms(fn)
    _, med, _ = sigma_clipped_stats(f[np.isfinite(f)], sigma=3, stdfunc="mad_std")
    yy, xx = np.mgrid[0:img.shape[0], 0:img.shape[1]]
    near = np.hypot(xx - xy[0], yy - xy[1]) <= 6.0 / pix_arcsec
    return float(np.nanmax(np.where(near, f - med, -np.inf)) / sig)


def run():
    rows, panels = [], []
    for key, v0win, wins in CASES:
        tgt = target_coord(key)
        center = target_at(tgt, 61000.0)
        refs = access.find_images(center.ra.deg, center.dec.deg)
        ep = epoch_table([r.t_min for r in refs], split_epochs([r.t_min for r in refs], max_epoch_days=60))
        cuts, _ = access.fetch_cutouts(refs, center.ra.deg, center.dec.deg, half_px=half_px_for(FOV))
        byep = {e["id"]: [c for c in cuts if e["mjd_min"] - 0.5 <= c.mjd <= e["mjd_max"] + 0.5] for e in ep}
        new_by_win = {}
        for win in wins:
            kw = dict(fov_arcmin=FOV, epochs=ep, center=center, band_um=win)
            full = build_frames(cuts, tgt, **kw)
            halves = ([], [])
            for e in ep:
                inb = sorted([c for c in byep[e["id"]] if win[0] <= c.wavelength_um <= win[1]], key=lambda c: c.mjd)
                for j, c in enumerate(inb):
                    halves[j % 2].append(c)
            ha = {f.epoch: f for f in build_frames(halves[0], tgt, min_exposures=2, **kw)}
            hb = {f.epoch: f for f in build_frames(halves[1], tgt, min_exposures=2, **kw)}
            new_by_win[win] = (full, ha, hb)
        fw_ref = measure_fwhm(np.nanmean([f.image for f in new_by_win[wins[0]][0]], axis=0))[0] * 3.0
        for e in ep:
            ec = byep[e["id"]]
            tc = target_at(tgt, e["mjd_mid"])
            # ---- v0
            img, n0, w0 = v0_coadd(ec, tc, *v0win)
            s_v0 = None
            if n0 >= 4:
                a, _, _ = v0_coadd(ec, tc, *v0win, start=0, step=2)
                b, _, _ = v0_coadd(ec, tc, *v0win, start=1, step=2)
                noise = (a - b) / 2
                s = s_v0 = rms(noise)
                xy = [float(v) for v in w0.world_to_pixel(tc)]
                sel = [c for c in sorted(ec, key=lambda c: c.mjd) if v0win[0] <= EM_MIN[c.detector] <= v0win[1]][:14]
                rows.append({"target": key, "epoch": e["id"], "date": e["date"], "method": "v0", "window_um": f"{v0win[0]}-{v0win[1]} (detector em_min)",
                             "n_used": n0, "bg_rms": s, "target_snr": snr_at(img, noise, xy, 6.15, fw_ref),
                             "fwhm_arcsec": measure_fwhm(img, guess_px=fw_ref / 6.15)[0] * 6.15,
                             "target_saturated": False, "black_px_per_1e4": black(img, s),
                             "flagged_in": float(np.mean([np.mean((c.flags & BAD_MASK) != 0) for c in sel])), "flag_frac": 0.0})
                panels.append((key, e["id"], "v0", img, xy, 6.15, n0, e["date"]))
            # ---- new
            for win in wins:
                full, ha, hb = new_by_win[win]
                f = next((f for f in full if f.epoch == e["id"]), None)
                if f is None:
                    continue
                noise = (ha[f.epoch].image - hb[f.epoch].image) / 2 if f.epoch in ha and f.epoch in hb else None
                s = rms(bin2(noise)) if noise is not None else float("nan")
                sel = [c for c in ec if c.obs_id in set(f.obs_ids)]
                rows.append({"target": key, "epoch": e["id"], "date": e["date"], "method": "spx.coadd", "window_um": f"{win[0]}-{win[1]} (LVF at target)",
                             "n_used": f.n_used, "bg_rms": s,
                             "target_snr": snr_at(f.image, noise, f.target_xy, 3.0, fw_ref) if noise is not None else float("nan"),
                             "fwhm_arcsec": measure_fwhm(f.image, guess_px=fw_ref / 3.0)[0] * 3.0,
                             "target_saturated": saturated_at(f.image, f.target_xy, 3.0),
                             "black_px_per_1e4": black(bin2(np.where(np.isfinite(f.image), f.image, np.nan)), s_v0 or s),
                             "flagged_in": float(np.mean([np.mean((c.flags & BAD_MASK) != 0) for c in sel])),
                             "flag_frac": float(np.nanmedian(f.flag_frac))})
                if win == wins[-1]:
                    panels.append((key, e["id"], f"spx.coadd {win[0]}-{win[1]}um", f.image, f.target_xy, 3.0, f.n_used, f.epoch_label, f))
    with open(OUT / "coadd_metrics_v2.csv", "w", newline="", encoding="utf-8") as fh:
        wr = csv.DictWriter(fh, fieldnames=list(rows[0]))
        wr.writeheader()
        for r in rows:
            wr.writerow({k: (round(v, 5) if isinstance(v, float) else v) for k, v in r.items()})
    figure(panels)
    return rows


def figure(panels):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    keys = list(dict.fromkeys((p[0], p[2].split()[0]) for p in panels))
    ncol = max(sum(1 for p in panels if (p[0], p[2].split()[0]) == k) for k in keys)
    fig, axs = plt.subplots(len(keys), ncol, figsize=(3.6 * ncol, 3.8 * len(keys)), squeeze=False)
    for r, k in enumerate(keys):
        ps = [p for p in panels if (p[0], p[2].split()[0]) == k]
        newf = [p[8] for p in panels if p[0] == k[0] and len(p) > 8]
        sc = common_scale(newf)[newf[0].key]  # same scale for v0 and new rows of a target
        for c in range(ncol):
            axs[r, c].axis("off")
        for c, p in enumerate(ps):
            a = axs[r, c]
            a.axis("on"); a.set_xticks([]); a.set_yticks([])
            a.imshow(asinh_stretch(p[3], *sc), origin="lower", cmap="gray", vmin=0, vmax=1, interpolation="nearest")
            a.add_patch(matplotlib.patches.Circle(p[4], 15 / p[5], fill=False, ec="red", lw=1))
            a.set_title(f"{p[0]} {p[2]}\n{p[7]}  N={p[6]}  {p[5]}\"/px", fontsize=8)
    fig.suptitle("Before (v0 prototype) / after (spx.coadd), same cutouts, same asinh scale per target", fontsize=10)
    fig.tight_layout(rect=(0, 0, 1, 0.975))
    fig.savefig(OUT / "spherex_coadd_v2.png", dpi=100)
    plt.close(fig)


def test_metrics():
    rows = run()
    print(f"{'target':10s} ep {'method':9s} {'window':26s} {'N':>3s} {'bg_rms':>8s} {'S/N':>6s} {'FWHM':>5s} {'black':>6s} {'flagIn':>6s} {'flagFr':>6s} sat")
    for r in rows:
        print(f"{r['target']:10s} {r['epoch']:2d} {r['method']:9s} {r['window_um']:26s} {r['n_used']:3d} {r['bg_rms']:8.5f} "
              f"{r['target_snr']:6.1f} {r['fwhm_arcsec']:5.1f} {r['black_px_per_1e4']:6.1f} {r['flagged_in']:6.3f} {r['flag_frac']:6.3f} {'Y' if r['target_saturated'] else ''}")
    for key in ("barnard", "wise-0855"):
        for e in {r["epoch"] for r in rows if r["target"] == key}:
            v0 = [r for r in rows if r["target"] == key and r["epoch"] == e and r["method"] == "v0"]
            nw = [r for r in rows if r["target"] == key and r["epoch"] == e and r["method"] == "spx.coadd"]
            if v0 and nw:
                assert nw[0]["black_px_per_1e4"] <= v0[0]["black_px_per_1e4"]


if __name__ == "__main__":
    test_metrics()
