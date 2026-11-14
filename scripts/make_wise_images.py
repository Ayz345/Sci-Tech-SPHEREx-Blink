"""WISE images for the gallery targets on each field's own grid: web/data/<key>/wise_W1.f32, wise_W2.f32, wise.json.

    python scripts/make_wise_images.py              # every target in web/data/index.json
    python scripts/make_wise_images.py wise-0855

Source: AllWISE Atlas images (W1 3.4 um, W2 4.6 um; 1.375 arcsec pixels, 4095 x 4095 per tile) from IRSA's open
S3 bucket nasa-irsa-wise (wise/allwise/images/p3am_cdd/<rr>/<rrrr>/<coadd_id>/<coadd_id>-w<b>-int-3.fits). The
header and only the rows the field needs are read with HTTP range requests, as for SPHEREx (no IRSA web service,
which answered 502 on 14 Nov 2026). Each sub-point is read from the nearest tile that contains it (fields on a tile edge use two).

Resampling: each 3 arcsec grid pixel is split into 6 x 6 sub-points (0.5 arcsec apart, finer than the 1.375 arcsec
Atlas pixel); each sub-point takes the Atlas pixel it falls in (tile WCS), and the grid pixel is their mean. That is
an area average of the surface brightness: unit DN per Atlas pixel (MAGZP 20.5 in the header), not summed.
NaN where the Atlas pixel is NaN or outside the tile. Static files only (PLAN.md 17 F5); they may be committed.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import warnings
from pathlib import Path

import numpy as np
import requests
from astropy.io import fits
from astropy.time import Time
from astropy.wcs import WCS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BUCKET = "https://nasa-irsa-wise.s3.amazonaws.com/"
PREFIX = "wise/allwise/images/p3am_cdd/"
SUB = 6
BANDS = [("W1", 1, 3.4), ("W2", 2, 4.6)]   # wave_um: WISE band names (W1 3.4 um, W2 4.6 um)
NOTE = ("AllWISE Atlas image (IRSA, open S3 copy) resampled to this field's 3 arcsec grid: each grid pixel is the "
        "mean of 6 x 6 sub-points, each taking the Atlas pixel (1.375 arcsec) it falls in, i.e. an area average of "
        "surface brightness in DN per Atlas pixel (MAGZP 20.5). The Atlas co-adds WISE exposures from 2010 "
        "(epoch below is the tile's header MIDOBS; DATEOBS1/DATEOBS2 give the range); a moving source is where it "
        "was then, not where SPHEREx saw it.")
SESSION = requests.Session()


def _get(url, rng=None):
    for i in range(6):
        try:
            r = SESSION.get(url, headers={"Range": rng} if rng else {}, timeout=(15, 90))
            if r.status_code in (200, 206):
                return r.content
            if r.status_code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"HTTP {r.status_code} for {url}")
        except requests.RequestException:
            pass
        import time
        time.sleep(0.5 * 2 ** i)
    raise RuntimeError(f"no answer for {url}")


def _ls(prefix: str) -> list[str]:
    txt = _get(BUCKET + f"?list-type=2&prefix={prefix}&delimiter=/&max-keys=1000").decode()
    return re.findall(r"<Prefix>([^<]*)</Prefix>", txt)[1:]


def grid_wcs(g: dict) -> WCS:
    w = WCS(naxis=2)
    w.wcs.ctype, w.wcs.crval, w.wcs.crpix, w.wcs.cdelt = g["ctype"], g["crval"], g["crpix"], g["cdelt"]
    return w


def _sep(ra1, dec1, ra2, dec2):
    r = math.radians
    c = math.sin(r(dec1)) * math.sin(r(dec2)) + math.cos(r(dec1)) * math.cos(r(dec2)) * math.cos(r(ra1 - ra2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def tiles_near(ra: float, dec: float, max_sep: float = 1.3) -> list[tuple[float, str]]:
    """(name separation, coadd prefix) of tiles named within max_sep deg, nearest first."""
    span = max_sep / max(0.05, math.cos(math.radians(dec)))
    out = []
    lo, hi = int(math.floor((ra - span) * 10)), int(math.ceil((ra + span) * 10))
    for rr in range(lo, hi + 1):
        rr %= 3600
        for d in _ls(f"{PREFIX}{rr // 100:02d}/{rr:04d}/"):
            cid = d.rstrip("/").split("/")[-1]
            m = re.match(r"(\d{4})([pm])(\d{3})_ac51$", cid)
            if not m:
                continue
            tra, tdec = int(m.group(1)) / 10, (1 if m.group(2) == "p" else -1) * int(m.group(3)) / 10
            s = _sep(ra, dec, tra, tdec)
            if s <= max_sep + 0.2:
                out.append((s, d))
    return sorted(out)


def read_header(url: str):
    buf = b""
    while True:
        buf += _get(url, f"bytes={len(buf)}-{len(buf) + 28799}")
        i = buf.find(b"END" + b" " * 77)
        while i >= 0 and i % 80:
            i = buf.find(b"END" + b" " * 77, i + 1)
        if i >= 0:
            hlen = (i // 2880 + 1) * 2880
            return fits.Header.fromstring(buf[:i + 80].decode("ascii")), hlen


def sub_points(g: dict):
    """World coordinates of SUB x SUB sub-points per grid pixel: arrays (ny, nx, SUB*SUB)."""
    ny, nx = g["shape"]
    off = (np.arange(SUB) + 0.5) / SUB - 0.5
    yy, xx = np.mgrid[0:ny, 0:nx]
    dx, dy = np.meshgrid(off, off)
    px = xx[..., None] + dx.ravel()[None, None, :]
    py = yy[..., None] + dy.ravel()[None, None, :]
    ra, dec = grid_wcs(g).pixel_to_world_values(px, py)
    return ra, dec


def _inside(h, x, y):
    return (x >= -0.5) & (y >= -0.5) & (x <= h["NAXIS1"] - 0.5) & (y <= h["NAXIS2"] - 0.5) & np.isfinite(x)


def resample(key: str, data: Path) -> dict:
    """Each sub-point is read from the nearest tile that contains it (a field on a tile edge uses several)."""
    m = json.loads((data / key / "manifest.json").read_text(encoding="utf-8"))
    g = m["grid"]
    cra, cdec = g["crval"]
    sra, sdec = sub_points(g)
    owner = np.full(sra.shape, -1, int)
    tiles = []
    for s, d in tiles_near(cra, cdec):
        if (owner >= 0).all():
            break
        cid = d.rstrip("/").split("/")[-1]
        h, hlen = read_header(BUCKET + d + f"{cid}-w1-int-3.fits")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tw = WCS(h)
        x, y = tw.world_to_pixel_values(sra, sdec)
        take = (owner < 0) & _inside(h, x, y)
        if not take.any():
            continue
        owner[take] = len(tiles)
        tiles.append({"cid": cid, "dir": d, "h": h, "x": x, "y": y})
    if (owner < 0).any():
        raise RuntimeError(f"{key}: {int((owner < 0).sum())} sub-points outside every AllWISE tile tried")
    out = {"key": key, "coadd_ids": [t["cid"] for t in tiles], "bands": []}
    from server import pipeline as P
    for bid, b, wave in BANDS:
        vals = np.full(sra.shape, np.nan)
        for k, t in enumerate(tiles):
            sel = owner == k
            url = BUCKET + t["dir"] + f"{t['cid']}-w{b}-int-3.fits"
            h, hlen = read_header(url)
            if h["NAXIS1"] != t["h"]["NAXIS1"] or h.get("CRVAL1") != t["h"].get("CRVAL1"):
                raise RuntimeError(f"{key}: W{b} tile geometry differs from W1 in {t['cid']}")
            ix, iy = np.rint(t["x"][sel]).astype(int), np.rint(t["y"][sel]).astype(int)
            y0, y1 = int(iy.min()), int(iy.max())
            bpp = abs(int(h["BITPIX"])) // 8
            nx = int(h["NAXIS1"])
            raw = _get(url, f"bytes={hlen + y0 * nx * bpp}-{hlen + (y1 + 1) * nx * bpp - 1}")
            dt = {-32: ">f4", -64: ">f8", 32: ">i4", 16: ">i2"}[int(h["BITPIX"])]
            rows = np.frombuffer(raw, dtype=dt).reshape(y1 - y0 + 1, nx).astype(np.float64)
            if h.get("BSCALE") is not None or h.get("BZERO") is not None:
                rows = rows * float(h.get("BSCALE", 1.0)) + float(h.get("BZERO", 0.0))
            vals[sel] = rows[iy - y0, ix]
            t.setdefault("bunit", h.get("BUNIT", "DN"))
            t.setdefault("magzp", {})[bid] = h.get("MAGZP")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            img = np.nanmean(vals, axis=-1).astype(np.float32)
        fin = img[np.isfinite(img)]
        med = float(np.median(fin))
        mad = float(np.median(np.abs(fin - med))) * 1.4826
        scale = {"vmin": round(med - 2 * mad, 4), "vmax": round(float(np.percentile(fin, 99.7)), 4),
                 "soft": round(max(mad, 1e-6), 4)}
        P.write_f32(data / key / f"wise_{bid}.f32", img)
        zps = sorted({t["magzp"][bid] for t in tiles})
        out["bands"].append({"id": bid, "file": f"wise_{bid}.f32", "wave_um": wave, "scale": scale,
                             "unit": f"{tiles[0]['bunit']} per Atlas pixel (1.375 arcsec), area mean",
                             "magzp": zps[0] if len(zps) == 1 else zps,
                             "nan_fraction": round(float(np.mean(~np.isfinite(img))), 4)})
    # epoch: the tile holding the field centre; every tile's MIDOBS is listed
    ny, nx = g["shape"]
    centre_tile = tiles[int(owner[ny // 2, nx // 2, 0])]
    out["tiles"] = []
    for t in tiles:
        out["tiles"].append({"coadd_id": t["cid"], "midobs": t["h"].get("MIDOBS"), "dateobs1": t["h"].get("DATEOBS1"),
                             "dateobs2": t["h"].get("DATEOBS2"),
                             "grid_fraction": round(float(np.mean(owner == tiles.index(t))), 4)})
    out["coadd_id"] = centre_tile["cid"]
    mid = centre_tile["h"].get("MIDOBS")
    if mid:
        tt = Time(mid, format="isot", scale="utc")
        out["epoch_mjd"], out["date"] = round(float(tt.mjd), 4), mid[:10]
        out["epoch_source"] = (f"header MIDOBS = {mid} of tile {centre_tile['cid']} (field centre); DATEOBS1 = "
                               f"{centre_tile['h'].get('DATEOBS1')}, DATEOBS2 = {centre_tile['h'].get('DATEOBS2')}")
        if len({t["h"].get("MIDOBS") for t in tiles}) > 1:
            out["epoch_source"] += "; other tiles of this field have other MIDOBS (see tiles)"
    else:
        out["epoch_mjd"], out["date"] = None, None
        out["epoch_source"] = f"no MIDOBS keyword in the header of tile {centre_tile['cid']}"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("keys", nargs="*")
    ap.add_argument("--data", default=str(ROOT / "web" / "data"))
    a = ap.parse_args(argv)
    data = Path(a.data)
    keys = a.keys or [t["key"] for t in json.loads((data / "index.json").read_text(encoding="utf-8"))["targets"]]
    from server import pipeline as P
    for key in keys:
        r = resample(key, data)
        doc = {"schema": 1, "key": key, "survey": "AllWISE Atlas", "source": "AllWISE Atlas (IRSA)",
               "source_url": f"{BUCKET}{PREFIX}", "coadd_id": r["coadd_id"], "tiles": r["tiles"],
               "epoch_mjd": r["epoch_mjd"],
               "date": r["date"], "epoch_source": r["epoch_source"], "bands": r["bands"], "note": NOTE}
        P.write_json(data / key / "wise.json", doc)
        print(key, r["coadd_id"], r["date"], [(b["id"], b["nan_fraction"], b["scale"]) for b in r["bands"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
