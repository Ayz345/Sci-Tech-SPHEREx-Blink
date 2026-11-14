"""Figures and blink GIFs from spx.coadd frames (for reports and the README; the web UI stretches raw frames).

python -m spx.display                       all five targets -> handoff/results/five_targets_v2.png + *_blink_v2.gif
python -m spx.display --targets wise-0855   one target
"""
from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np

from spx.coadd import build_frames, common_scale, difference, half_px_for, stretch, target_at

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "handoff" / "results"

# display band per target: where the object is bright and the field is clean
DISPLAY_BAND = {"barnard": "D2", "eps-indi-b": "D2", "wise-0855": "W", "rw-aur": "D3", "r-dra": "D2"}


def frames_for(key, fov_arcmin=7.0, band=None, verbose=False):
    """Fetch (cached) and co-add one target; returns (frames of the display band, target dict)."""
    from spx import access
    from spx.targets import TARGETS, target_coord
    t = TARGETS[key]
    tgt = target_coord(key)
    center = target_at(tgt, 61000.0)
    refs = access.find_images(center.ra.deg, center.dec.deg)
    if not refs:
        return [], t
    _, ep = access.epochs_for(refs)  # the contract's single epoch table (same as the manifest)
    cuts, _ = access.fetch_cutouts(refs, center.ra.deg, center.dec.deg, half_px=half_px_for(fov_arcmin))
    band = band or t.get("default_band") or DISPLAY_BAND.get(key, "D2")
    kw = dict(fov_arcmin=fov_arcmin, epochs=ep, center=center, verbose=verbose)
    extra = {b["id"]: b for b in t.get("extra_bands", [])}
    if band in extra:
        frames = build_frames(cuts, tgt, band_um=tuple(extra[band]["wave_um"]), band_id=band, **kw)
    else:
        frames = [f for f in build_frames([c for c in cuts if f"D{c.detector}" == band], tgt, **kw) if f.band == band]
    return frames, t


def panel(ax, img8, f, title=None, ring_arcsec=15.0):
    ax.imshow(img8, origin="lower", cmap="gray", vmin=0, vmax=255, interpolation="nearest")
    r = ring_arcsec / f.grid.pix_arcsec
    ax.add_patch(__import__("matplotlib.patches", fromlist=["Circle"]).Circle(f.target_xy, r, fill=False, ec="red", lw=1.0))
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title(title or f"{f.epoch_label}  N={f.n_used}", fontsize=8)


def five_target_figure(results, path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    ncol = max(len(fr) for fr, _ in results.values()) + 1
    fig, axs = plt.subplots(len(results), ncol, figsize=(2.3 * ncol, 2.55 * len(results)), squeeze=False)
    for row, (key, (frames, t)) in enumerate(results.items()):
        for a in axs[row]:
            a.axis("off")
        if not frames:
            axs[row, 0].set_title(f"{t['name']}: no data", fontsize=8)
            continue
        u8 = stretch(frames, common_scale(frames))
        diffs = difference(frames)
        for k, (f, im) in enumerate(zip(frames, u8)):
            axs[row, k].axis("on")
            panel(axs[row, k], im, f, f"{t['name'] if k == 0 else ''}\n{f.epoch_label}  {f.band}  N={f.n_used}")
        # last column: newest minus the others (movers -> black/white pair, variables -> residual)
        d = diffs[-1]
        lim = 5 * np.nanmedian([f.noise for f in frames])
        a = axs[row, len(frames)]
        a.axis("on")
        a.imshow(np.nan_to_num(d), origin="lower", cmap="gray", vmin=-lim, vmax=lim, interpolation="nearest")
        a.set_xticks([]); a.set_yticks([])
        a.set_title(f"difference\n{frames[-1].epoch_label} − others", fontsize=8)
    fig.suptitle("SPHEREx per-epoch co-adds (spx.coadd v1): fixed sky grid 3\"/px, north up, east left, 7'; "
                 "common asinh scale per row; red ring = target at that epoch", fontsize=9)
    fig.tight_layout(rect=(0, 0, 1, 0.965))
    fig.savefig(path, dpi=110)
    plt.close(fig)


def blink_gif(frames, path, scale_px=3, ms=700):
    """Animated GIF of the epochs (nearest-neighbour upscale, cosmetic) with a date label."""
    from PIL import Image, ImageDraw
    u8 = stretch(frames, common_scale(frames))
    imgs = []
    for f, im in zip(frames, u8):
        a = np.flipud(im)  # GIF rows go top-down; frames are south-first
        pil = Image.fromarray(a).resize((a.shape[1] * scale_px, a.shape[0] * scale_px), Image.NEAREST).convert("RGB")
        dr = ImageDraw.Draw(pil)
        x, y = f.target_xy
        r = 15 / f.grid.pix_arcsec * scale_px
        cx, cy = (x + 0.5) * scale_px, (a.shape[0] - 0.5 - y) * scale_px
        dr.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 60, 60))
        dr.text((6, 6), f"{f.epoch_label}  {f.band}  N={f.n_used}", fill=(255, 255, 0))
        imgs.append(pil)
    if imgs:
        imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=ms, loop=0)


def main(argv=None):
    warnings.filterwarnings("ignore")
    from spx.targets import TARGETS
    p = argparse.ArgumentParser()
    p.add_argument("--targets", nargs="*", default=list(TARGETS))
    p.add_argument("--fov", type=float, default=7.0)
    a = p.parse_args(argv)
    results = {}
    for key in a.targets:
        frames, t = frames_for(key, a.fov, verbose=True)
        results[key] = (frames, t)
        print(key, [(f.epoch_label, f.band, f.n_used) for f in frames], flush=True)
        if frames:
            blink_gif(frames, OUT / f"{key}_blink_v2.gif")
    five_target_figure(results, OUT / "five_targets_v2.png")
    print("written", OUT / "five_targets_v2.png")


if __name__ == "__main__":
    sys.exit(main())
