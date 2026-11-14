"""Probe 2: plane/artifact TAP speed, IRSA cutout failure rate vs workers, S3 range-read cutouts. Read-only."""
import io
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pyvo
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.wcs import WCS

c = SkyCoord("08h55m10.831s -07d14m42.53s")
tap = pyvo.dal.TAPService("https://irsa.ipac.caltech.edu/TAP")

for tn in ("spherex.plane", "spherex.artifact"):
    cols = tap.run_sync(f"select column_name from TAP_SCHEMA.columns where table_name='{tn}'").to_table()
    print(tn, list(cols["column_name"]))

t = time.time()
r = tap.run_sync(
    "SELECT a.uri, p.time_bounds_lower, p.energy_bandpassname FROM spherex.artifact a "
    "JOIN spherex.plane p ON a.planeid = p.planeid "
    f"WHERE 1 = CONTAINS(POINT('ICRS', {c.ra.deg}, {c.dec.deg}), p.poly) ORDER BY p.time_bounds_lower"
).to_table()
print(f"plane/artifact query: {len(r)} rows in {time.time() - t:.1f}s")
print("  first rows:", [(str(x["uri"]), float(x["time_bounds_lower"]), str(x["energy_bandpassname"])) for x in r[:3]])
uris = [str(x) for x in r["uri"]]
print("  distinct prefixes:", sorted({u.split("/level2/")[0] for u in uris}), "n unique uri", len(set(uris)))
print("  bandpass counts:", {b: int((r["energy_bandpassname"] == b).sum()) for b in sorted(set(r["energy_bandpassname"]))})


def irsa_cut(uri):
    t0 = time.time()
    try:
        resp = requests.get(f"https://irsa.ipac.caltech.edu/{uri}?center={c.ra.deg},{c.dec.deg}&size=0.1", timeout=180)
        b = resp.content
        if b[:6] != b"SIMPLE":
            return 0, time.time() - t0, f"{resp.status_code} {b[:150]!r}"
        return len(b), time.time() - t0, "ok"
    except Exception as e:
        return 0, time.time() - t0, repr(e)[:100]


pos = 20
for workers in (6, 10):
    sel = uris[pos:pos + 20]
    pos += 20
    t = time.time()
    with ThreadPoolExecutor(workers) as ex:
        cs = list(ex.map(irsa_cut, sel))
    bad = [x[2] for x in cs if x[0] == 0]
    print(f"IRSA 20 cutouts, {workers} workers: wall {time.time() - t:.1f}s, ok {20 - len(bad)}/20, median each {np.median([x[1] for x in cs]):.1f}s")
    if bad:
        print("   first failure:", bad[0])

# ---- S3 range reads over plain HTTPS (no extra deps) ----
S3 = "https://nasa-irsa-spherex.s3.amazonaws.com/"
sess = requests.Session()
sess.mount("https://", requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32))


def rng(url, a, b):
    resp = sess.get(url, headers={"Range": f"bytes={a}-{b}"}, timeout=120)
    resp.raise_for_status()
    return resp.content


def s3_cut(uri, half=30):
    """Read primary+IMAGE headers, locate target pixel, range-read only the needed rows of IMAGE."""
    t0 = time.time()
    try:
        url = S3 + uri.split("ibe/data/spherex/")[-1]
        head = rng(url, 0, 2880 * 12 - 1)  # primary + IMAGE header (with SIP) should fit
        nbytes = len(head)
        # walk header blocks
        off = 0
        h0 = fits.Header.fromstring(head[off:].decode("ascii", "replace"))
        off += ((len(h0.tostring()) + 2879) // 2880) * 2880
        h1 = fits.Header.fromstring(head[off:].decode("ascii", "replace"))
        data0 = off + ((len(h1.tostring()) + 2879) // 2880) * 2880
        w = WCS(h1).celestial
        x, y = w.world_to_pixel(c)
        x, y = int(round(float(x))), int(round(float(y)))
        nx, ny = h1["NAXIS1"], h1["NAXIS2"]
        y0, y1 = max(0, y - half), min(ny, y + half + 1)
        x0, x1 = max(0, x - half), min(nx, x + half + 1)
        if y1 <= y0 or x1 <= x0:
            return 0, time.time() - t0, "target outside image"
        raw = rng(url, data0 + y0 * nx * 4, data0 + y1 * nx * 4 - 1)
        nbytes += len(raw)
        img = np.frombuffer(raw, dtype=">f4").reshape(y1 - y0, nx)[:, x0:x1]
        return nbytes, time.time() - t0, f"ok {img.shape} bitpix={h1['BITPIX']} finite={np.isfinite(img).mean():.2f} det={h1.get('DETECTOR')}"
    except Exception as e:
        return 0, time.time() - t0, repr(e)[:120]


print("S3 single:", s3_cut(uris[0]))
for workers in (16, 32):
    sel = uris[60:60 + 64]
    t = time.time()
    with ThreadPoolExecutor(workers) as ex:
        cs = list(ex.map(s3_cut, sel))
    bad = [x[2] for x in cs if x[0] == 0]
    print(f"S3 64 cutouts, {workers} workers: wall {time.time() - t:.1f}s, ok {64 - len(bad)}/64, median each {np.median([x[1] for x in cs]):.1f}s, MB total {sum(x[0] for x in cs) / 1e6:.1f}")
    if bad:
        print("   failures:", bad[:3])
