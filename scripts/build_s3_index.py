"""Scan the public SPHEREx bucket and write our own index of its Level 2 images (no IRSA service involved).

IRSA's SIA2 and TAP services, which answer "which images cover this position", were down for hours on
15 Nov 2026 while the bucket itself stayed up. Every Level 2 file carries its pointing and times in the
first kilobytes of its IMAGE header, so one small range request per file is enough to know where and when
it looked. This script reads those cards for every file and writes one chunk per week plan:

    <out>/<rel>__<plan>.npz   key (S3 keys), det (1..6), cols (card names), val (N x 14 float64)

Only the cards are kept (no SIP terms): the server tests a position with the TAN part of the WCS and a 40 px
margin (server/s3index.py). Files are `*D[1-6]_spx_*.fits` under dr1/level2/ and qr3/level2/; when a card is not
in RANGE, the file is read again over WIDE.

    python scripts/build_s3_index.py --out cache/s3index                 # everything (1,628,230 files on 15 Nov 2026)
    python scripts/build_s3_index.py --out chunks --slice 3/20           # one of 20 equal parts (CI matrix)

A plan whose chunk exists is skipped, so an interrupted run can simply be started again. The workflow "Image
index of the bucket" (.github/workflows/s3index.yml) runs 20 parts: 86 s for the run on 15 Nov 2026. Then

    gh run download <run id> -D cache/s3index
    python -m server.s3index cache/s3index server/index/spx_index.npz   # merge + encoding check
    python scripts/check_s3index.py --write                              # handoff/results/s3index_check.json

Needs numpy and requests.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import multiprocessing as mp
import os
import re
import threading
import time
from pathlib import Path

import numpy as np
import requests

BUCKET = "https://nasa-irsa-spherex.s3.amazonaws.com"
RELEASES = ("dr1", "qr3")
#: Bytes of the file that hold the WCS and time cards of the IMAGE header (PRIMARY is one 2880-byte block).
RANGE = (12960, 21599)
WIDE = (2880, 34559)
CARDS = ("CRPIX1", "CRPIX2", "PC1_1", "PC1_2", "PC2_1", "PC2_2", "CDELT1", "CDELT2", "CRVAL1", "CRVAL2",
         "MJD-BEG", "MJD-AVG", "MJD-END", "TELAPSE")
_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.mount("https://", requests.adapters.HTTPAdapter(pool_connections=2, pool_maxsize=2, max_retries=0))
        _local.s = s
    return s


def list_keys(prefix: str, delimiter: str | None = None) -> tuple[list[str], list[str]]:
    """All keys (and, with a delimiter, the common prefixes) under `prefix`."""
    keys, prefixes, token = [], [], None
    while True:
        params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
        if delimiter:
            params["delimiter"] = delimiter
        if token:
            params["continuation-token"] = token
        for attempt in range(8):
            try:
                r = _session().get(BUCKET + "/", params=params, timeout=60)
                if r.status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(1 + attempt)
        else:
            raise RuntimeError(f"listing {prefix} failed")
        text = r.text
        keys += re.findall(r"<Key>([^<]+)</Key>", text)
        prefixes += re.findall(r"<CommonPrefixes><Prefix>([^<]+)</Prefix>", text)
        m = re.search(r"<NextContinuationToken>([^<]+)</NextContinuationToken>", text)
        if not m:
            return keys, prefixes
        token = m.group(1)


def parse_cards(block: bytes) -> dict:
    values = {}
    for i in range(0, len(block) - 79, 80):
        if block[i + 8:i + 10] != b"= ":
            continue
        name = block[i:i + 8].rstrip().decode("ascii", "replace")
        if name in CARDS and name not in values:
            try:
                values[name] = float(block[i + 10:i + 80].split(b"/")[0])
            except ValueError:
                pass
    return values


def read_cards(key: str) -> list[float] | None:
    """The 14 card values of one file, NaN where a card is missing, None when the file cannot be read."""
    best = None
    for lo, hi in (RANGE, WIDE):
        for attempt in range(6):
            try:
                r = _session().get(f"{BUCKET}/{key}", headers={"Range": f"bytes={lo}-{hi}"}, timeout=45)
            except requests.RequestException:
                time.sleep(0.5 + attempt)
                continue
            if r.status_code == 206:
                values = parse_cards(r.content)
                if all(c in values for c in CARDS):
                    return [values[c] for c in CARDS]
                best = values
                break
            if r.status_code in (403, 404):
                return None
            time.sleep(0.5 + attempt)  # 500, 503 SlowDown
    if best:
        return [best.get(c, float("nan")) for c in CARDS]
    return None


def scan_plan(job: tuple[str, str, str, int]) -> tuple[str, str, int, int, float]:
    rel, plan, out_dir, threads = job
    out = Path(out_dir) / f"{rel}__{plan}.npz"
    if out.exists():
        return rel, plan, -1, 0, 0.0
    t0 = time.time()
    keys, _ = list_keys(f"{rel}/level2/{plan}/")
    keys = [k for k in keys if k.endswith(".fits") and re.search(r"D[1-6]_spx", k)]
    with cf.ThreadPoolExecutor(threads) as pool:
        values = list(pool.map(read_cards, keys))
    nan_row = [float("nan")] * len(CARDS)
    val = np.array([v if v is not None else nan_row for v in values], dtype=np.float64).reshape(-1, len(CARDS))
    bad = int((~np.all(np.isfinite(val), axis=1)).sum())
    det = np.array([int(re.search(r"D([1-6])_spx", k).group(1)) for k in keys], dtype=np.int8)
    tmp = out.with_name(out.stem + ".tmp.npz")
    np.savez_compressed(tmp, key=np.array(keys), det=det, cols=np.array(CARDS), val=val)
    os.replace(tmp, out)
    return rel, plan, len(keys), bad, time.time() - t0


def all_plans() -> list[tuple[str, str]]:
    plans = []
    for rel in RELEASES:
        _, weeks = list_keys(f"{rel}/level2/", "/")
        plans += [(rel, w.rstrip("/").split("/")[-1]) for w in weeks]
    return sorted(plans, key=lambda p: (p[1], p[0]))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="cache/s3index", help="directory for the chunk files")
    ap.add_argument("--slice", default="0/1", help="i/n: scan only plans whose position in the sorted list is i mod n")
    ap.add_argument("--procs", type=int, default=max(1, os.cpu_count() or 1))
    ap.add_argument("--threads", type=int, default=64, help="range requests in flight per process")
    args = ap.parse_args()
    i, n = (int(x) for x in args.slice.split("/"))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    plans = all_plans()
    mine = [p for j, p in enumerate(plans) if j % n == i]
    todo = [(rel, plan, str(out), args.threads) for rel, plan in mine if not (out / f"{rel}__{plan}.npz").exists()]
    print(f"{len(plans)} week plans in the bucket, {len(mine)} in slice {i}/{n}, {len(todo)} to scan; "
          f"{args.procs} processes x {args.threads} requests", flush=True)
    t0, files, bad_total = time.time(), 0, 0
    with mp.Pool(args.procs) as pool:
        for done, (rel, plan, nk, bad, dt) in enumerate(pool.imap_unordered(scan_plan, todo), 1):
            files += max(nk, 0)
            bad_total += bad
            el = time.time() - t0
            print(f"{done}/{len(todo)} {rel} {plan}: {nk} files, {bad} with a missing card, {dt:.0f} s | "
                  f"{files} files so far, {files / max(el, 1e-9):.0f}/s, {el / 60:.1f} min", flush=True)
    print(f"finished: {files} files, {bad_total} with a missing card, {(time.time() - t0) / 60:.1f} min", flush=True)


if __name__ == "__main__":
    main()
