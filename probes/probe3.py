"""Probe 3: size of the image index and SIA2 speed. Read-only."""
import time

import pyvo
import requests

tap = pyvo.dal.TAPService("https://irsa.ipac.caltech.edu/TAP")
for q in (
    "select count(*) as n from spherex.plane",
    "select energy_bandpassname, count(*) as n, min(time_bounds_lower) as t0, max(time_bounds_lower) as t1 from spherex.plane group by energy_bandpassname",
    "select top 3 planeid, obsid, energy_bandpassname, time_bounds_lower, position_bounds from spherex.plane",
):
    t = time.time()
    try:
        r = tap.run_sync(q).to_table()
        print(f"[{time.time() - t:.1f}s] {q[:70]}")
        for row in r:
            print("   ", [str(v)[:110] for v in row])
    except Exception as e:
        print(f"[{time.time() - t:.1f}s] ERR {q[:60]}: {repr(e)[:200]}")

for coll in ("spherex_dr1", "spherex_qr3"):
    t = time.time()
    try:
        resp = requests.get("https://irsa.ipac.caltech.edu/SIA", params={"COLLECTION": coll, "POS": "circle 133.795129 -7.245147 0.001"}, timeout=300)
        print(f"SIA2 {coll}: {resp.status_code} {len(resp.content)} bytes, {resp.text.count('<TR>')} rows in {time.time() - t:.1f}s")
    except Exception as e:
        print(f"SIA2 {coll} ERR {repr(e)[:150]} {time.time() - t:.1f}s")
