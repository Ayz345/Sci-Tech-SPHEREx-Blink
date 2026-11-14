"""Fill the live server's cache with the demo fields before a presentation, and time every request.

    python scripts/warm_demo.py                       # http://127.0.0.1:8000
    python scripts/warm_demo.py --api http://host:8000 --only eros

Run it only after the code is frozen and the gallery rebuilt (--final): the server's cache folder is named after
the code hash, so a run before that is thrown away by the next code change. The orchestrator says when.

For each field: manifest (retried while IRSA answers 503), every listed band, candidates (waits for 202 -> 200),
the spectrum at the target; for the Eros pass also /api/track and the CSV and ADES drafts. Writes
cache/warm_demo_<UTC>.json with every time and status.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

#: The demo fields. Change them here.
FIELDS = [
    {"name": "eros", "what": "(433) Eros pass, D3: marks -> /api/track -> MPC draft (the marks are the e2e test's)",
     "track": {"band": "D3", "marks": [
         {"ra": 17.189553551067803, "dec": 17.678595848007337, "mjd": 60876.552522925},
         {"ra": 17.412703092818823, "dec": 17.87055406387909, "mjd": 60877.16839781}]}},
    {"name": "wise0855-15arcmin", "what": "15' field at the WISE 0855 position (free field, fov 0.25)",
     "manifest": {"ra": 133.7951, "dec": -7.2451, "fov": 0.25}},
    {"name": "free", "what": "a free position outside the gallery (from the 14 Nov acceptance run)",
     "manifest": {"ra": 172.7822, "dec": 30.0083}},
]
SUBMITTER = "I. M. Tester"


def timed(sess, method, url, **kw):
    t = time.time()
    try:
        r = sess.request(method, url, timeout=kw.pop("timeout", 400), **kw)
        return r, round(time.time() - t, 2)
    except requests.RequestException as e:
        return e, round(time.time() - t, 2)


def status(r):
    return r.status_code if hasattr(r, "status_code") else type(r).__name__


def warm_field(sess, api, f, log):
    m = None
    for i in range(6):
        r, t = timed(sess, "GET", f"{api}/api/manifest", params=f["manifest"])
        log.append({"field": f["name"], "step": "manifest", "try": i + 1, "status": status(r), "seconds": t})
        print(log[-1], flush=True)
        if status(r) == 200:
            m = r.json()
            break
        time.sleep(5)
    if m is None or not m.get("epochs"):
        return
    key = m["key"]
    for b in m["bands"]:
        r, t = timed(sess, "GET", f"{api}/api/band", params={"key": key, "band": b["id"]})
        log.append({"field": f["name"], "step": f"band {b['id']}", "status": status(r), "seconds": t})
        print(log[-1], flush=True)
    t0 = time.time()
    while time.time() - t0 < 300:
        r, t = timed(sess, "GET", f"{api}/api/candidates", params={"key": key}, timeout=60)
        if status(r) != 202:
            break
        time.sleep(5)
    log.append({"field": f["name"], "step": "candidates", "status": status(r), "seconds": round(time.time() - t0, 2)})
    print(log[-1], flush=True)
    r, t = timed(sess, "GET", f"{api}/api/spectrum", params={"key": key})
    log.append({"field": f["name"], "step": "spectrum", "status": status(r), "seconds": t})
    print(log[-1], flush=True)


def warm_track(sess, api, f, log):
    r, t = timed(sess, "POST", f"{api}/api/track", json=f["track"])
    log.append({"field": f["name"], "step": "track", "status": status(r), "seconds": t})
    print(log[-1], flush=True)
    if status(r) != 200:
        return
    rid = r.json()["report_id"]
    for fmt in ("csv", "ades", "ades_xml"):
        r, t = timed(sess, "GET", f"{api}/api/report", params={"id": rid, "format": fmt, "submitter": SUBMITTER})
        log.append({"field": f["name"], "step": f"report {fmt}", "status": status(r), "seconds": t})
        print(log[-1], flush=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--only", nargs="*", help="field names to warm (default: all)")
    a = ap.parse_args(argv)
    api = a.api.rstrip("/")
    sess = requests.Session()
    h = sess.get(f"{api}/api/health", timeout=10).json()
    started = dt.datetime.now(dt.timezone.utc)
    log: list = []
    for f in FIELDS:
        if a.only and f["name"] not in a.only:
            continue
        print(f"== {f['name']}: {f['what']}", flush=True)
        t0 = time.time()
        (warm_track if "track" in f else warm_field)(sess, api, f, log)
        log.append({"field": f["name"], "step": "total", "seconds": round(time.time() - t0, 2)})
        print(log[-1], flush=True)
    out = ROOT / "cache" / f"warm_demo_{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps({"started_utc": started.isoformat(timespec="seconds"), "api": api,
                               "server_commit": h.get("commit"), "code_hash": h.get("code_hash"), "log": log}, indent=1))
    print("written", out)
    bad = [x for x in log if x.get("status") not in (None, 200)]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
