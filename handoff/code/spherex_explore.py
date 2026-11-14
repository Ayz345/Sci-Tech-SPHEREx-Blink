"""SPHEREx exploration for NASA Space Apps 2026 "Planet X and SPHEREx".

1. Counts SPHEREx images/epochs per challenge test target (IRSA TAP, ObsCore).
2. Builds per-epoch co-added cutouts (WISE 0855, Barnard's Star) -> spherex_coadd.png

Usage:  python spherex_explore.py            (count + figure)
        python spherex_explore.py --count     (count only, fast)
Needs:  pip install pyvo astropy numpy scipy matplotlib requests
"""
import io
import sys
import warnings

import astropy.units as u
import matplotlib
import numpy as np
import pyvo
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.io.votable import parse
from astropy.time import Time
from astropy.visualization import ZScaleInterval
from astropy.wcs import WCS
from scipy.ndimage import map_coordinates

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

warnings.filterwarnings("ignore")

TAP = "https://irsa.ipac.caltech.edu/TAP"
COLLECTIONS = ("spherex_dr1", "spherex_qr3")  # DR1 = 2025-2026 archive, QR3 = latest weeks
EPOCH_GAP_DAYS = 30  # gaps longer than this split images into 6-month survey epochs

# Challenge test targets (J2000 catalog positions)
TARGETS = {
    "Barnard's Star": "17h57m48.5s +04d41m36s",
    "Epsilon Indi B": "22h04m10.5s -56d46m58s",
    "WISE 0855": "08h55m10.831s -07d14m42.53s",
    "RW Aurigae": "05h07m49.5s +30d24m05s",
    "R Draconis": "16h32m40.2s +66d45m18s",
}


def query_images(tap, coord):
    """All SPHEREx level-2 images whose footprint contains coord."""
    q = (
        "select obs_collection, obs_id, t_min, em_min, em_max, access_url from ivoa.obscore "
        f"where obs_collection in {COLLECTIONS} and dataproduct_type='image' "
        f"and 1=CONTAINS(POINT('ICRS',{coord.ra.deg},{coord.dec.deg}), s_region)"
    )
    return tap.run_sync(q).to_table()


def split_epochs(t_min):
    """Index groups, one per survey epoch, in time order."""
    order = np.argsort(t_min)
    return np.split(order, np.where(np.diff(t_min[order]) > EPOCH_GAP_DAYS)[0] + 1)


def fits_url(datalink):
    """ObsCore access_url is a DataLink; the ID must be URL-encoded, '#this' row is the file."""
    base, ident = datalink.split("?ID=")
    vt = parse(io.BytesIO(requests.get(base, params={"ID": ident}, timeout=120).content))
    table = vt.get_first_table().to_table()  # columns named by ID: col_1 access_url, col_4 semantics
    return [str(row["col_1"]) for row in table if str(row["col_4"]) == "#this"][0]


def count_all(tap):
    tables = {}
    for name, pos in TARGETS.items():
        r = query_images(tap, SkyCoord(pos))
        tables[name] = r
        tm = np.array(r["t_min"], float)
        epochs = [(Time(tm[g[0]], format="mjd").iso[:10], len(g)) for g in split_epochs(tm)] if len(tm) else []
        print(f"{name}: {len(r)} images, {len(epochs)} epochs {epochs}")
    return tables


def coadd_figure(tables, out="spherex_coadd.png", size=0.12, per_epoch=14):
    pix = 6.15 / 3600  # SPHEREx pixel scale
    n = int(size / pix)
    targets = {
        # Barnard moves ~10.4"/yr: propagate to each epoch with Hipparcos proper motion
        "Barnard's Star": (SkyCoord(269.452075 * u.deg, 4.693391 * u.deg,
                                    pm_ra_cosdec=-798.58 * u.mas / u.yr, pm_dec=10328.12 * u.mas / u.yr,
                                    distance=1.83 * u.pc, obstime=Time("J2000")), (1.0e-6, 2.0e-6)),
        # TODO: WISE 0855 position epoch unknown (pm ~ -8.1"/yr RA); not yet motion-corrected
        "WISE 0855": (SkyCoord(TARGETS["WISE 0855"]), (3.8e-6, 5.0e-6)),
    }
    fig, ax = plt.subplots(2, 3, figsize=(12, 8.4))
    for row, (name, (c0, (lo, hi))) in enumerate(targets.items()):
        r = tables[name]
        tm = np.array(r["t_min"], float)
        em = np.array(r["em_min"], float)
        for k, grp in enumerate(split_epochs(tm)[:3]):
            a = ax[row, k]
            a.axis("off")
            idx = [i for i in grp if lo <= em[i] <= hi][:per_epoch]
            tmid = Time(np.median(tm[grp]), format="mjd")
            c = c0.apply_space_motion(new_obstime=tmid) if c0.obstime is not None else c0
            # common TAN grid centred on target; each cutout is resampled onto it
            w = WCS(naxis=2)
            w.wcs.ctype = ["RA---TAN", "DEC--TAN"]
            w.wcs.crval = [c.ra.deg, c.dec.deg]
            w.wcs.crpix = [n / 2 + 0.5] * 2
            w.wcs.cdelt = [-pix, pix]
            yy, xx = np.mgrid[0:n, 0:n]
            sky = w.pixel_to_world(xx, yy)
            acc = np.zeros((n, n))
            cnt = np.zeros((n, n))
            used = 0
            for i in idx:
                try:
                    url = fits_url(str(r["access_url"][i])) + f"?center={c.ra.deg},{c.dec.deg}&size={size * 1.5}"
                    h = fits.open(io.BytesIO(requests.get(url, timeout=180).content))
                    d = h["IMAGE"].data.astype(float)  # TODO: mask with h["FLAGS"]
                    d -= np.nanmedian(d)
                    px, py = WCS(h["IMAGE"].header).celestial.world_to_pixel(sky)
                    v = map_coordinates(np.nan_to_num(d), [py, px], order=1, cval=np.nan)
                    ok = np.isfinite(v) & (px > 0) & (py > 0) & (px < d.shape[1] - 1) & (py < d.shape[0] - 1)
                    acc[ok] += v[ok]
                    cnt[ok] += 1
                    used += 1
                except Exception as e:  # IRSA sometimes returns non-FITS or times out
                    print(name, k, "skip", repr(e)[:80])
            img = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
            vmin, vmax = ZScaleInterval().get_limits(img[np.isfinite(img)])
            a.imshow(img, origin="lower", cmap="gray", vmin=vmin, vmax=vmax * 2)
            a.plot(n / 2 - 0.5, n / 2 - 0.5, "o", mfc="none", mec="r", ms=16)
            a.set_title(f"{name} - {tmid.iso[:10]}\n{used} images co-added, {lo * 1e6:.1f}-{hi * 1e6:.1f} um", fontsize=9)
            print(name, k, used, "images")
    plt.suptitle(f"SPHEREx per-epoch co-add ({size} deg, 6.15\"/px); red ring = target position at epoch")
    plt.tight_layout()
    plt.savefig(out, dpi=110)
    print("saved", out)


if __name__ == "__main__":
    tap = pyvo.dal.TAPService(TAP)
    tables = count_all(tap)
    if "--count" not in sys.argv:
        coadd_figure(tables)
