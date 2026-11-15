"""Checks of our own image index (server/index/spx_index.npz) against what it was built from and against IRSA.

    python scripts/check_s3index.py            # writes cache/s3index_check.json
    python scripts/check_s3index.py --write    # also handoff/results/s3index_check.json (tracked)

1. the index: rows per collection, plans, versions, first and last exposure; load time, memory, query time;
2. encoding error of the compact file against the scan's chunks (cache/s3index/*.npz, when present);
3. the effect of ignoring SIP: the target pixel by the index's TAN WCS against astropy's full TAN-SIP WCS, for
   WISE 0855's images (their headers are in the access layout cache, when present);
4. the five gallery targets: the index's image list against IRSA's SIA2 list kept in web/data/_sia/ (missing,
   extra, same S3 key), and where the target falls on the extra images (pixels from the nearest detector edge:
   negative = off the detector, inside the 40 px margin; positive = on it, an image IRSA did not list).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import warnings
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from server import s3index as X  # noqa: E402

TRACKED = ROOT / "handoff" / "results" / "s3index_check.json"
SEED_NAMES = {"269.44626": "barnard", "331.09670": "eps-indi-b", "133.75978": "wise-0855", "76.95609": "rw-aur",
              "248.16723": "r-dra"}


def _rss() -> int:
    """Resident memory of this process in bytes (0 when psutil is missing)."""
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except Exception:
        pass
    try:                                      # Windows without psutil
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                (n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]
        c = PMC()
        c.cb = ctypes.sizeof(c)
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        f = ctypes.windll.psapi.GetProcessMemoryInfo
        f.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
        f(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize
    except Exception:
        pass
    try:                                      # Linux
        return int(open("/proc/self/statm").read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


def index_stats(idx) -> dict:
    from astropy.time import Time
    rel = np.asarray(idx.d["rel"])
    beg, end = idx.d["mjd_beg"], idx.d["mjd_end"]
    return {"file": Path(idx.meta["path"]).name, "bytes": Path(idx.meta["path"]).stat().st_size, "rows": idx.n,
            "rows_per_collection": {X.COLLECTIONS[r]: int((rel == i).sum()) for i, r in enumerate(idx.meta["rels"])},
            "plans": len(idx.meta["plans"]), "versions": len(idx.meta["vers"]), "built_utc": idx.meta["built_utc"],
            "first_exposure": Time(float(beg.min()), format="mjd").isot[:19],
            "last_exposure": Time(float(end.max()), format="mjd").isot[:19]}


def encoding_error(idx, chunk_dir: Path) -> dict | None:
    chunks = sorted((p for p in chunk_dir.rglob("*.npz") if not p.name.endswith(".tmp.npz")), key=lambda p: p.name)
    if not chunks:
        return None
    keys, cols, report = X.read_chunks(chunks)
    order = np.argsort(np.array(keys))
    arr = {c: np.concatenate(v)[order] for c, v in cols.items() if v}
    if len(order) != idx.n:
        return {"note": f"index has {idx.n} rows, chunks {len(order)}: built from other chunks"}
    return {"chunks": report["chunks"], "rows": report["rows"], "rows_nan": report["rows_nan"],
            "crval_max_arcsec": float(np.max(np.hypot((idx.d["crval1"] - arr["crval1"]) *
                                                      np.cos(np.radians(arr["crval2"])),
                                                      idx.d["crval2"] - arr["crval2"])) * 3600),
            "t_beg_max_s": float(np.max(np.abs(idx.d["mjd_beg"] - arr["mjd_beg"])) * 86400),
            "t_end_max_s": float(np.max(np.abs(idx.d["mjd_end"] - arr["mjd_end"])) * 86400),
            "pc_max_rel": float(max(np.max(np.abs(idx.d[c] - arr[c])) for c in ("pc11", "pc12", "pc21", "pc22"))
                                / np.max(np.abs(arr["pc11"])))}


def sip_effect(idx) -> dict | None:
    from astropy.io import fits
    from astropy.wcs import WCS
    from spx import access
    seed = ROOT / "web" / "data" / "_sia" / "spherex_dr1" / "133.75978_-7.24233_0.001.json"
    if not seed.exists():
        return None
    d = json.loads(seed.read_text(encoding="utf-8"))
    ra, dec = d["ra"], d["dec"]
    key_row = {idx.key(i): i for i in idx.indices_at(ra, dec)}
    diffs = []
    for r in d["rows"]:
        L = access._load_layout(r["s3_key"])
        if L is None or not L.cel_hdr or r["s3_key"] not in key_row:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            w = WCS(fits.Header.fromstring(L.cel_hdr))
            px, py = w.all_world2pix(ra, dec, 1)
        x, y, _ = idx._pixels(ra, dec, np.array([key_row[r["s3_key"]]]))
        diffs.append(float(np.hypot(px - x[0], py - y[0])))
    if not diffs:
        return None
    a = np.array(diffs)
    return {"target": "wise-0855", "images": len(a), "median_px": round(float(np.median(a)), 2),
            "p95_px": round(float(np.percentile(a, 95)), 2), "max_px": round(float(a.max()), 2),
            "margin_px": X.MARGIN_PX}


def gallery(idx) -> dict:
    out = {}
    for f in sorted((ROOT / "web" / "data" / "_sia").glob("*/*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        name = SEED_NAMES.get(f.name.split("_")[0], f.stem)
        coll = d["collection"]
        got = idx.rows_at(d["ra"], d["dec"], coll)
        a = {(r["obs_id"], r["detector"]): r["s3_key"] for r in d["rows"]}
        b = {(r["obs_id"], r["detector"]): r for r in got}
        extra = [k for k in b if k not in a]
        dist = []
        if extra:
            ii = {(X.S3Index.obs_id(idx.key(i)), int(idx.d["det"][i])): i for i in idx.indices_at(d["ra"], d["dec"])}
            sel = np.array([ii[k] for k in extra if k in ii])
            x, y, _ = idx._pixels(d["ra"], d["dec"], sel)
            dist = np.minimum(np.minimum(x - 0.5, X.NPIX + 0.5 - x), np.minimum(y - 0.5, X.NPIX + 0.5 - y))
        t = out.setdefault(name, {"sia": 0, "index": 0, "missing": 0, "extra": 0, "extra_on_detector": 0,
                                  "extra_in_margin": 0, "key_differs": 0, "per_collection": {}})
        rec = {"sia": len(a), "index": len(b), "missing": len([k for k in a if k not in b]), "extra": len(extra),
               "extra_on_detector": int((np.asarray(dist) > 0).sum()) if len(dist) else 0,
               "extra_in_margin": int((np.asarray(dist) <= 0).sum()) if len(dist) else 0,
               "key_differs": len([k for k in a if k in b and b[k]["s3_key"] != a[k]]),
               "extra_edge_px_quantiles_0_50_100": [round(float(q), 0) for q in np.percentile(dist, [0, 50, 100])]
               if len(dist) else None}
        t["per_collection"][coll] = rec
        for k in ("sia", "index", "missing", "extra", "extra_on_detector", "extra_in_margin", "key_differs"):
            t[k] += rec[k]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--index", default=str(ROOT / "server" / "index" / "spx_index.npz"))
    ap.add_argument("--chunks", default=str(ROOT / "cache" / "s3index"))
    ap.add_argument("--write", action="store_true", help="also write handoff/results/s3index_check.json")
    a = ap.parse_args(argv)
    t0 = time.perf_counter()
    rss0 = _rss()
    idx = X.S3Index.load(Path(a.index))
    load = {"load_s": round(time.perf_counter() - t0, 2),
            "rss_added_mb": round((_rss() - rss0) / 1e6) if rss0 else None,
            "arrays_mb": round(sum(getattr(v, "nbytes", 0) for v in idx.d.values()) / 1e6)}
    q = []
    for ra, dec in ((270.0, 66.5), (133.75978, -7.24233), (10.0, 0.0), (200.0, -60.0)):
        t1 = time.perf_counter()
        n = sum(len(idx.rows_at(ra, dec, c)) for c in ("spherex_dr1", "spherex_qr3"))
        q.append({"ra": ra, "dec": dec, "images": n, "query_s": round(time.perf_counter() - t1, 3)})
    load["queries"] = q
    res = {"checked_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "index": index_stats(idx), "load": load, "encoding_error": encoding_error(idx, Path(a.chunks)),
           "sip_ignored": sip_effect(idx), "gallery": gallery(idx)}
    text = json.dumps(res, indent=1)
    (ROOT / "cache").mkdir(exist_ok=True)
    (ROOT / "cache" / "s3index_check.json").write_bytes(text.encode("utf-8"))
    if a.write:
        TRACKED.write_bytes(text.encode("utf-8"))
        print("written", TRACKED)
    print(json.dumps({k: res[k] for k in ("index", "load", "encoding_error", "sip_ignored")}, indent=1))
    print(json.dumps({k: {kk: v[kk] for kk in ("sia", "index", "missing", "extra", "extra_on_detector",
                                                "extra_in_margin", "key_differs")} for k, v in res["gallery"].items()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
