"""Timing probe: how fast can we get SPHEREx cutouts for one position? (read-only)"""
import io
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

import numpy as np
import pyvo
import requests
from astropy.coordinates import SkyCoord
from astropy.io import fits
from astropy.io.votable import parse

TAP = "https://irsa.ipac.caltech.edu/TAP"
c = SkyCoord("08h55m10.831s -07d14m42.53s")
tap = pyvo.dal.TAPService(TAP)

t = time.time()
cols = tap.run_sync("select column_name, datatype from TAP_SCHEMA.columns where table_name='ivoa.obscore'").to_table()
print("obscore columns:", list(cols["column_name"]), f"{time.time() - t:.1f}s")

t = time.time()
r = tap.run_sync(
    "select * from ivoa.obscore where obs_collection in ('spherex_dr1','spherex_qr3') and dataproduct_type='image' "
    f"and 1=CONTAINS(POINT('ICRS',{c.ra.deg},{c.dec.deg}), s_region)"
).to_table()
print(f"TAP query: {len(r)} rows in {time.time() - t:.1f}s")
row = r[0]
for k in r.colnames:
    print("  ", k, "=", str(row[k])[:160])

t = time.time()
tabs = tap.run_sync("select table_name from TAP_SCHEMA.tables where table_name like '%spherex%'").to_table()
print("spherex tables:", list(tabs["table_name"]), f"{time.time() - t:.1f}s")


def resolve(dl):
    base, ident = dl.split("?ID=")
    t0 = time.time()
    vt = parse(io.BytesIO(requests.get(base, params={"ID": ident}, timeout=120).content))
    tb = vt.get_first_table().to_table()
    url = [str(x["col_1"]) for x in tb if str(x["col_4"]) == "#this"][0]
    return url, time.time() - t0


def cutout(url):
    t0 = time.time()
    try:
        b = requests.get(url + f"?center={c.ra.deg},{c.dec.deg}&size=0.1", timeout=180).content
        h = fits.open(io.BytesIO(b))
        return len(b), time.time() - t0, [(x.name, getattr(x.data, "shape", None)) for x in h]
    except Exception as e:
        return 0, time.time() - t0, repr(e)[:100]


sel = [str(x) for x in r["access_url"][:16]]
print("datalink example:", sel[0])
t = time.time()
with ThreadPoolExecutor(16) as ex:
    res = list(ex.map(resolve, sel))
print(f"16 datalink resolves parallel: wall {time.time() - t:.1f}s, each {np.round([x[1] for x in res], 1)}")
urls = [x[0] for x in res]
print("fits url example:", urls[0])

for workers in (1, 16):
    n = 2 if workers == 1 else 16
    t = time.time()
    with ThreadPoolExecutor(workers) as ex:
        cs = list(ex.map(cutout, urls[:n]))
    print(f"{n} cutouts, {workers} workers: wall {time.time() - t:.1f}s, each {np.round([x[1] for x in cs], 1)}, bytes {[x[0] for x in cs][:4]}")
    print("   first:", cs[0][2])
    print("   failures:", [x[2] for x in cs if x[0] == 0])

# S3 mirror: same key path under the public bucket?
key = urls[0].split("/ibe/data/spherex/")[-1]
for s3 in (f"https://nasa-irsa-spherex.s3.amazonaws.com/{key}", f"https://nasa-irsa-spherex.s3.us-east-1.amazonaws.com/{quote(key)}"):
    t = time.time()
    try:
        h = requests.head(s3, timeout=60)
        print("S3 HEAD", h.status_code, h.headers.get("Content-Length"), h.headers.get("Accept-Ranges"), f"{time.time() - t:.1f}s", s3)
    except Exception as e:
        print("S3 ERR", repr(e)[:120])
