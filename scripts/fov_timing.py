"""Cold timing of 15' (0.25 deg) fields on a server of its own (port 8001, own cache, removed after): per band seconds
and S3 MB, six-band time, server memory (peak working set, sampled every second; Windows tasklist).

    python scripts/fov_timing.py                     # measure, write cache/fov_timing.json
    python scripts/fov_timing.py --write             # also handoff/results/fov_timing_v2.json (tracked)
    python scripts/fov_timing.py --from cache/f4_15arcmin.json --write   # convert an earlier run, no download

PLAN.md 17 F4. Field 1 asks its bands one by one (queue=0: clean per-band numbers); field 2 opens like the page
(queue=1: first band while the rest fill in the background).
"""
import argparse, datetime as dt, json, os, shutil, subprocess, sys, threading, time
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent.parent
PORT = 8001
API = f"http://127.0.0.1:{PORT}"
CACHE = ROOT / "cache" / "f4_cache"
FOV = 0.25
FIELDS = [("wise-0855 position", 133.7951, -7.2451, "per-band"), ("barnard position", 269.452, 4.6934, "page")]

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--write", action="store_true", help="also write handoff/results/fov_timing_v2.json")
ap.add_argument("--from", dest="src", default=None, help="convert an earlier result file instead of measuring")
A = ap.parse_args()
TRACKED = ROOT / "handoff" / "results" / "fov_timing_v2.json"


def publish(res):
    res.setdefault("measured_utc", dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    res["conditions"] = ("cold cache, home connection (~5 MB/s), live layers IMAGE+FLAGS, 3 arcsec grid; one server "
                         "of its own; IRSA SIA2 retries included in manifest times")
    (ROOT / "cache" / "fov_timing.json").write_text(json.dumps(res, indent=1))
    if A.write:
        TRACKED.write_text(json.dumps(res, indent=1))
        print("written", TRACKED)


if A.src:
    src = json.loads(Path(A.src).read_text())
    src.setdefault("measured_utc", dt.datetime.fromtimestamp(Path(A.src).stat().st_mtime, dt.timezone.utc)
                   .isoformat(timespec="seconds") + " (file time of " + Path(A.src).name + ")")
    publish(src)
    sys.exit(0)

env = dict(os.environ, SPX_MAX_FOV_DEG=str(FOV), SPX_CACHE=str(CACHE))
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", str(PORT),
                         "--log-level", "warning"], cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
mem = {"peak_mb": 0.0, "samples": []}
stop = threading.Event()


def sampler():
    while not stop.is_set():
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {proc.pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=5).stdout
            kb = float(out.strip().split('","')[-1].strip('"').replace(" K", "").replace(".", "").replace(",", ""))
            mb = kb / 1024
            mem["samples"].append((round(time.time(), 1), round(mb, 1)))
            mem["peak_mb"] = max(mem["peak_mb"], mb)
        except Exception:
            pass
        stop.wait(1.0)


def health():
    return requests.get(API + "/api/health", timeout=10).json()


def manifest(ra, dec, queue):
    for i in range(8):
        t = time.time()
        r = requests.get(API + "/api/manifest", params={"ra": ra, "dec": dec, "fov": FOV, "queue": queue}, timeout=200)
        if r.status_code == 200:
            return r.json(), time.time() - t, i + 1
        print("manifest", r.status_code, r.text[:120], flush=True)
        time.sleep(5)
    raise RuntimeError("no manifest")


res = {"fov_deg": FOV, "fields": []}
try:
    for _ in range(90):
        try:
            h0 = health(); break
        except Exception:
            time.sleep(1)
    res["server"] = {"commit": h0["commit"], "max_fov_deg": h0["max_fov_deg"], "live_layers": h0["live_layers"]}
    threading.Thread(target=sampler, daemon=True).start()
    for label, ra, dec, mode in FIELDS:
        mem_before = mem["peak_mb"]
        tot0 = health()["network_totals"]
        m, t_man, tries = manifest(ra, dec, 0 if mode == "per-band" else 1)
        fr = {"label": label, "ra": ra, "dec": dec, "mode": mode, "key": m["key"], "grid_shape": m["grid"]["shape"],
              "n_images": m.get("n_images"), "manifest_s": round(t_man, 1), "manifest_tries": tries, "bands": []}
        print(label, m["key"], m["grid"]["shape"], m.get("n_images"), flush=True)
        t_field = time.time()
        order = [b["id"] for b in m["bands"]]
        for b in order:
            before = health()["network_totals"]
            t = time.time()
            r = requests.get(API + "/api/band", params={"key": m["key"], "band": b}, timeout=400)
            dt = time.time() - t
            after = health()["network_totals"]
            bj = r.json() if r.status_code == 200 else {}
            fr["bands"].append({"band": b, "status": r.status_code, "seconds": round(dt, 1),
                                "s3_mb": round((after["s3_bytes"] - before["s3_bytes"]) / 1e6, 1),
                                "s3_requests": after["s3_requests"] - before["s3_requests"],
                                "n_used": [e["n_used"] for e in bj.get("epochs", [])]})
            print(" ", fr["bands"][-1], flush=True)
            if mode == "page" and b == order[0]:
                fr["first_band_s"] = round(dt, 1)
        # page mode: the rest built in the background meanwhile; wait until idle
        while True:
            h = health()
            if not any(h["bands_queued"].values()) and h["bands_running"] == 0:
                break
            time.sleep(1)
        fr["six_bands_s"] = round(time.time() - t_field, 1)
        tot1 = health()["network_totals"]
        fr["s3_mb_total"] = round((tot1["s3_bytes"] - tot0["s3_bytes"]) / 1e6, 1)
        fr["sia_queries"] = tot1["sia_queries"] - tot0["sia_queries"]
        fr["sia_retries"] = tot1.get("sia_retries", 0) - tot0.get("sia_retries", 0)
        fr["peak_mem_mb"] = round(mem["peak_mb"], 1)
        d = CACHE / "fields"
        files = [p for p in d.rglob("*") if p.is_file() and m["key"] in str(p)]
        fr["disk_mb_frames"] = round(sum(p.stat().st_size for p in files) / 1e6, 1)
        res["fields"].append(fr)
        print(label, {k: v for k, v in fr.items() if k != "bands"}, flush=True)
finally:
    stop.set()
    res["memory_peak_mb"] = round(mem["peak_mb"], 1)
    res["cutout_cache_mb"] = round(sum(p.stat().st_size for p in (CACHE / "cutout").rglob("*") if p.is_file()) / 1e6, 1) \
        if (CACHE / "cutout").exists() else None
    proc.terminate()
    proc.wait(15)
    shutil.rmtree(CACHE, ignore_errors=True)
    publish(res)
    print(json.dumps({k: v for k, v in res.items() if k != "fields"}))
