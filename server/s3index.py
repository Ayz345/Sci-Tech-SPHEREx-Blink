"""Our own index of the SPHEREx Level 2 images in the public S3 bucket: a position query that needs no IRSA service.

IRSA's SIA2 and TAP answered 502 for hours on 15 Nov 2026 while the S3 bucket stayed up. Every image file starts
with its WCS (TAN-SIP: CRPIX, PC, CDELT, CRVAL) and times (MJD-BEG/AVG/END, TELAPSE) in the first ~22 kB; those
cards, read once for every file of the bucket, make this index.

    idx = S3Index.load(path)                      # server/index/spx_index.npz (built by build_compact)
    rows = idx.rows_at(ra, dec, "spherex_dr1")    # rows in the same form as spx.access caches SIA2 answers

Containment: the position is projected with the file's TAN WCS (SIP ignored) and must fall on the 2040 x 2040
detector with a MARGIN_PX margin. Ignoring SIP and the margin can list an image the position just misses; such an
image is dropped later, when its cutout is read ("position off the detector").
"""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np

NPIX = 2040                     # IMAGE NAXIS1 = NAXIS2 = 2040 (header, byte 3120 of each file)
MARGIN_PX = 40.0
COARSE_DEG = 2.6                # a detector is ~3.5 deg across: its centre is within ~2.5 deg of any point on it
COLLECTIONS = {"dr1": "spherex_dr1", "qr3": "spherex_qr3"}
ACCESS_BASE = "https://irsa.ipac.caltech.edu/ibe/data/spherex/"
KEY_RE = re.compile(r"^(dr1|qr3)/level2/([^/]+)/([^/]+)/([1-6])/level2_\2_(\d{4})_(\d+)D\4_spx_\3\.fits$")
COLUMNS = ("det", "crval1", "crval2", "pc11", "pc12", "pc21", "pc22", "crpix1", "crpix2", "cdelt1", "cdelt2",
           "mjd_beg", "mjd_avg", "mjd_end", "telapse")


def _unit(ra_deg, dec_deg):
    ra, dec = np.radians(ra_deg), np.radians(dec_deg)
    return np.stack([np.cos(dec) * np.cos(ra), np.cos(dec) * np.sin(ra), np.sin(dec)], axis=-1)


def tan_project(ra, dec, ra0, dec0):
    """Gnomonic (FITS TAN, default LONPOLE) intermediate coordinates in degrees of (ra, dec) about (ra0, dec0);
    vectorised over the reference points. Returns (xi, eta, cos_c): cos_c <= 0 means the far hemisphere."""
    a, d = math.radians(ra), math.radians(dec)
    a0, d0 = np.radians(ra0), np.radians(dec0)
    cos_c = math.sin(d) * np.sin(d0) + math.cos(d) * np.cos(d0) * np.cos(a - a0)
    xi = math.cos(d) * np.sin(a - a0) / cos_c
    eta = (math.sin(d) * np.cos(d0) - math.cos(d) * np.sin(d0) * np.cos(a - a0)) / cos_c
    return np.degrees(xi), np.degrees(eta), cos_c


def tan_unproject(xi_deg, eta_deg, ra0, dec0):
    """Inverse of tan_project for one reference point: intermediate degrees -> (ra, dec) degrees."""
    x, y = np.radians(xi_deg), np.radians(eta_deg)
    d0 = math.radians(dec0)
    rho = np.hypot(x, y)
    c = np.arctan(rho)
    with np.errstate(invalid="ignore", divide="ignore"):
        dec = np.arcsin(np.cos(c) * math.sin(d0) + np.where(rho > 0, y * np.sin(c) * math.cos(d0) / rho, 0.0))
    ra = math.radians(ra0) + np.arctan2(x * np.sin(c), rho * math.cos(d0) * np.cos(c) - y * math.sin(d0) * np.sin(c))
    return np.degrees(ra) % 360.0, np.degrees(dec)


class S3Index:
    def __init__(self, data: dict, meta: dict):
        self.d = data
        self.meta = meta
        self.n = len(data["det"])
        self._u = _unit(data["crval1"], data["crval2"])

    # ------------------------------------------------------------------ keys
    def key(self, i: int) -> str:
        if self.d["raw"][i] >= 0:
            return str(self.meta["raw_keys"][self.d["raw"][i]])
        rel = self.meta["rels"][self.d["rel"][i]]
        plan = self.meta["plans"][self.d["plan"][i]]
        ver = self.meta["vers"][self.d["ver"][i]]
        det = int(self.d["det"][i])
        return f"{rel}/level2/{plan}/{ver}/{det}/level2_{plan}_{int(self.d['seq'][i]):04d}_{int(self.d['sub'][i])}D{det}_spx_{ver}.fits"

    @staticmethod
    def obs_id(key: str) -> str:
        m = KEY_RE.match(key)
        if m:
            return f"{m.group(2)}_{m.group(5)}_{m.group(6)}"
        name = key.rsplit("/", 1)[-1]
        return re.sub(r"^level2_(.*)D[1-6]_spx_.*$", r"\1", name)

    # ------------------------------------------------------------------ geometry
    def _pixels(self, ra: float, dec: float, idx: np.ndarray):
        d = self.d
        xi, eta, cos_c = tan_project(ra, dec, d["crval1"][idx], d["crval2"][idx])
        c11 = d["pc11"][idx] * d["cdelt1"][idx]
        c12 = d["pc12"][idx] * d["cdelt1"][idx]
        c21 = d["pc21"][idx] * d["cdelt2"][idx]
        c22 = d["pc22"][idx] * d["cdelt2"][idx]
        det = c11 * c22 - c12 * c21
        dx = (c22 * xi - c12 * eta) / det
        dy = (-c21 * xi + c11 * eta) / det
        return d["crpix1"][idx] + dx, d["crpix2"][idx] + dy, cos_c

    def indices_at(self, ra: float, dec: float, margin_px: float = MARGIN_PX) -> np.ndarray:
        cand = np.nonzero(self._u @ _unit(ra, dec) > math.cos(math.radians(COARSE_DEG)))[0]
        if not len(cand):
            return cand
        x, y, cos_c = self._pixels(ra, dec, cand)
        lo, hi = 0.5 - margin_px, NPIX + 0.5 + margin_px
        ok = (cos_c > 0) & (x >= lo) & (x <= hi) & (y >= lo) & (y <= hi)
        return cand[ok]

    def s_regions(self, idx) -> list[str]:
        """Detector outlines (TAN, SIP ignored) as SIA2 s_region strings, for many rows at once."""
        d = self.d
        idx = np.asarray(idx, int)
        if not len(idx):
            return []
        out = np.zeros((len(idx), 10))
        for j, (px, py) in enumerate([(0.5, 0.5), (NPIX + 0.5, 0.5), (NPIX + 0.5, NPIX + 0.5), (0.5, NPIX + 0.5)]):
            dx, dy = px - d["crpix1"][idx], py - d["crpix2"][idx]
            xi = np.radians(d["pc11"][idx] * d["cdelt1"][idx] * dx + d["pc12"][idx] * d["cdelt1"][idx] * dy)
            eta = np.radians(d["pc21"][idx] * d["cdelt2"][idx] * dx + d["pc22"][idx] * d["cdelt2"][idx] * dy)
            a0, d0 = np.radians(d["crval1"][idx]), np.radians(d["crval2"][idx])
            rho = np.hypot(xi, eta)
            c = np.arctan(rho)
            safe = np.where(rho > 0, rho, 1.0)
            dec = np.arcsin(np.cos(c) * np.sin(d0) + np.where(rho > 0, eta * np.sin(c) * np.cos(d0) / safe, 0.0))
            ra = a0 + np.arctan2(xi * np.sin(c), rho * np.cos(d0) * np.cos(c) - eta * np.sin(d0) * np.sin(c))
            out[:, 2 * j], out[:, 2 * j + 1] = np.degrees(ra) % 360.0, np.degrees(dec)
        out[:, 8:] = out[:, :2]
        return ["POLYGON ICRS " + " ".join(f"{v:.10g}" for v in row) for row in out]

    def rows_at(self, ra: float, dec: float, collection: str) -> list[dict]:
        """Images of `collection` (spherex_dr1 / spherex_qr3) on which (ra, dec) falls, as SIA2 rows (the form
        spx.access caches). One row per exposure and detector; of two processing versions the later one."""
        from spx import access
        rel = {v: k for k, v in COLLECTIONS.items()}[collection]
        rel_i = self.meta["rels"].index(rel) if rel in self.meta["rels"] else -1
        idx = [i for i in self.indices_at(ra, dec) if self.d["rel"][i] == rel_i]
        best = {}
        for i in idx:
            key = self.key(i)
            k = (self.obs_id(key), int(self.d["det"][i]))
            ver = key.split("/")[3]
            if k not in best or _version_rank(ver) > _version_rank(best[k][1].split("/")[3]):
                best[k] = (i, key)
        items = sorted(best.items(), key=lambda kv: (float(self.d["mjd_beg"][kv[1][0]]), kv[0][1]))
        regions = self.s_regions([i for _, (i, _) in items])
        rows = []
        for ((obs, det), (i, key)), reg in zip(items, regions):
            lo, hi = access.DETECTOR_BANDS_UM[det]
            rows.append({"obs_id": obs, "detector": det, "collection": collection, "s3_key": key,
                         "access_url": ACCESS_BASE + key, "t_min": float(self.d["mjd_beg"][i]),
                         "t_max": float(self.d["mjd_end"][i]), "em_min_um": float(lo), "em_max_um": float(hi),
                         "bandpass": f"SPHEREx-D{det}", "exptime_s": float(self.d["telapse"][i]), "s_region": reg})
        return rows

    def covers(self, collection: str) -> bool:
        rel = {v: k for k, v in COLLECTIONS.items()}.get(collection)
        return rel in self.meta["rels"]

    # ------------------------------------------------------------------ files
    @classmethod
    def load(cls, path: Path) -> "S3Index":
        """Decode a compact file (build_compact) into float64 / float32 columns."""
        z = np.load(path, allow_pickle=False)
        n = len(z["det"])
        meta = {"rels": [str(x) for x in z["rels"]], "plans": [str(x) for x in z["plans"]],
                "vers": [str(x) for x in z["vers"]], "raw_keys": [str(x) for x in z["raw_keys"]],
                "built_utc": str(z["built_utc"]), "mjd_max": float(z["mjd_max"]), "path": str(path)}
        raw = np.full(n, -1, np.int32)
        raw[z["raw_rows"]] = np.arange(len(z["raw_rows"]), dtype=np.int32)
        base = float(z["mjd_base"])
        beg = base + z["t_beg_ds"].astype(np.float64) / 864000.0              # 0.1 s units
        data = {"rel": z["rel"], "plan": z["plan"], "ver": z["ver"], "seq": z["seq"], "sub": z["sub"], "raw": raw,
                "det": z["det"], "crval1": z["crval1"].astype(np.float64), "crval2": z["crval2"].astype(np.float64),
                "mjd_beg": beg, "mjd_end": beg + z["t_end_cs"].astype(np.float64) / 8640000.0,   # 0.01 s units
                "telapse": z["telapse_cs"].astype(np.float32) / 100.0}
        for c in ("pc11", "pc12", "pc21", "pc22", "crpix1", "crpix2", "cdelt1", "cdelt2"):
            v = z[c]
            data[c] = np.broadcast_to(v.astype(np.float32), (n,)) if v.ndim == 0 else v.astype(np.float32)
        return cls(data, meta)


def _version_rank(ver: str) -> tuple:
    """Later processing first: the date in the version (l2b-v27-2026-247), then a retry over the first run."""
    m = re.search(r"(\d{4})-(\d{3})", ver)
    return (m.group(0) if m else "", "retry" in ver)


#: Scan chunk columns (cache/s3index/<rel>__<plan>.npz: key, det, cols, val[N x 14]) -> our column names.
CHUNK_COLS = {"CRPIX1": "crpix1", "CRPIX2": "crpix2", "PC1_1": "pc11", "PC1_2": "pc12", "PC2_1": "pc21",
              "PC2_2": "pc22", "CDELT1": "cdelt1", "CDELT2": "cdelt2", "CRVAL1": "crval1", "CRVAL2": "crval2",
              "MJD-BEG": "mjd_beg", "MJD-AVG": "mjd_avg", "MJD-END": "mjd_end", "TELAPSE": "telapse"}


def read_chunks(chunks: list[Path]) -> tuple[list[str], dict, dict]:
    """Rows of the scan's chunk files: (keys, columns, report). A row with an unreadable card (NaN) is left out
    and counted; files still being written (*.tmp.npz) are not read."""
    keys, cols, report = [], {c: [] for c in COLUMNS}, {"chunks": 0, "rows": 0, "rows_nan": 0}
    for ch in chunks:
        if ch.name.endswith(".tmp.npz"):
            continue
        z = np.load(ch, allow_pickle=False)
        names = [str(c) for c in z["cols"]]
        val = np.asarray(z["val"], np.float64)
        ok = np.all(np.isfinite(val), axis=1)
        report["chunks"] += 1
        report["rows"] += len(ok)
        report["rows_nan"] += int((~ok).sum())
        keys += [str(k) for k, g in zip(z["key"], ok) if g]
        cols["det"].append(np.asarray(z["det"])[ok])
        for src, dst in CHUNK_COLS.items():
            cols[dst].append(val[ok, names.index(src)])
    return keys, cols, report


def build_compact(chunks: list[Path], out: Path, built_utc: str) -> dict:
    """Merge the scan's chunk files into one compact file: keys rebuilt from a pattern (collection, plan,
    version, detector, exposure number) where they fit it, kept as text otherwise."""
    keys, cols, report = read_chunks(chunks)
    n = len(keys)
    arr = {c: np.concatenate(v) if v else np.zeros(0) for c, v in cols.items()}
    order = sorted(range(n), key=lambda i: keys[i])          # sorted keys: ids and times compress well
    keys = [keys[i] for i in order]
    arr = {c: v[order] if len(v) else v for c, v in arr.items()}
    rels, plans, vers, raw_rows, raw_keys = [], {}, {}, [], []
    rel = np.zeros(n, np.uint8)
    plan = np.zeros(n, np.uint16)
    ver = np.zeros(n, np.uint16)
    seq = np.zeros(n, np.uint16)
    sub = np.zeros(n, np.uint8)
    for i, k in enumerate(keys):
        m = KEY_RE.match(k)
        if not m:
            raw_rows.append(i)
            raw_keys.append(k)
            continue
        if m.group(1) not in rels:
            rels.append(m.group(1))
        rel[i] = rels.index(m.group(1))
        plan[i] = plans.setdefault(m.group(2), len(plans))
        ver[i] = vers.setdefault(m.group(3), len(vers))
        seq[i] = int(m.group(5))
        sub[i] = int(m.group(6))
    base = float(np.floor(arr["mjd_beg"].min())) if n else 0.0
    t_beg = np.round((arr["mjd_beg"] - base) * 864000.0)
    t_end = np.round((arr["mjd_end"] - base) * 8640000.0 - t_beg * 10.0)
    tel = np.round(arr["telapse"] * 100.0)
    if n and (t_beg.max() >= 2 ** 32 or t_end.min() < 0 or t_end.max() >= 2 ** 16 or tel.max() >= 2 ** 16):
        raise ValueError("times out of the compact ranges")

    def maybe_scalar(v, dtype):
        v = np.asarray(v, dtype)
        return np.array(v[0], dtype) if n and np.all(v == v[0]) else v
    store = {"rel": rel, "plan": plan, "ver": ver, "seq": seq, "sub": sub, "det": arr["det"].astype(np.uint8),
             "crval1": arr["crval1"].astype(np.float32), "crval2": arr["crval2"].astype(np.float32),
             "pc11": maybe_scalar(arr["pc11"], np.float16), "pc12": maybe_scalar(arr["pc12"], np.float16),
             "pc21": maybe_scalar(arr["pc21"], np.float16), "pc22": maybe_scalar(arr["pc22"], np.float16),
             "crpix1": maybe_scalar(arr["crpix1"], np.float32), "crpix2": maybe_scalar(arr["crpix2"], np.float32),
             "cdelt1": maybe_scalar(arr["cdelt1"], np.float32), "cdelt2": maybe_scalar(arr["cdelt2"], np.float32),
             "mjd_base": np.array(base), "t_beg_ds": t_beg.astype(np.uint32), "t_end_cs": t_end.astype(np.uint16),
             "telapse_cs": tel.astype(np.uint16),
             "rels": np.array(rels if rels else [""]), "plans": np.array(list(plans) if plans else [""]),
             "vers": np.array(list(vers) if vers else [""]), "raw_rows": np.array(raw_rows, np.int32),
             "raw_keys": np.array(raw_keys if raw_keys else [""]), "built_utc": np.array(built_utc),
             "mjd_max": np.array(float(arr["mjd_end"].max()) if n else 0.0)}
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(out, **store)
    idx = S3Index.load(out)
    bad = [i for i in range(n) if idx.key(i) != keys[i]]
    if bad:
        raise AssertionError(f"{len(bad)} keys do not round-trip, e.g. {keys[bad[0]]}")
    err = {"crval_arcsec": float(np.max(np.abs(idx.d["crval2"] - arr["crval2"])) * 3600) if n else 0.0,
           "t_beg_s": float(np.max(np.abs(idx.d["mjd_beg"] - arr["mjd_beg"])) * 86400) if n else 0.0,
           "t_end_s": float(np.max(np.abs(idx.d["mjd_end"] - arr["mjd_end"])) * 86400) if n else 0.0,
           "pc_rel": float(np.max(np.abs(idx.d["pc11"] - arr["pc11"]) / np.max(np.abs(arr["pc11"])))) if n else 0.0}
    if err["crval_arcsec"] > 0.2 or err["t_beg_s"] > 0.1 or err["t_end_s"] > 0.1 or err["pc_rel"] > 1e-3:
        raise AssertionError(f"compact encoding too coarse: {err}")
    return {**report, "rows_kept": n, "bytes": out.stat().st_size, "raw_keys": len(raw_keys), "plans": len(plans),
            "vers": len(vers), "encoding_error": err}


def main(argv=None) -> int:
    """python -m server.s3index [CHUNK_DIR] [OUT]: merge the scan's chunks (default cache/s3index/*.npz) into the
    server's file (default server/index/spx_index.npz) and print the report; --split writes dr1 and qr3 apart."""
    import argparse
    import datetime as dt
    import json
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("chunks", nargs="?", default=str(root / "cache" / "s3index"))
    ap.add_argument("out", nargs="?", default=str(root / "server" / "index" / "spx_index.npz"))
    ap.add_argument("--split", action="store_true", help="one file per collection (spx_index_dr1.npz, ..._qr3.npz)")
    a = ap.parse_args(argv)
    chunks = sorted((p for p in Path(a.chunks).rglob("*.npz") if not p.name.endswith(".tmp.npz")),
                    key=lambda p: p.name)          # gh run download puts each artifact in a folder of its own
    built = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    out = Path(a.out)
    groups = {rel: [c for c in chunks if c.name.startswith(rel + "__")] for rel in COLLECTIONS} if a.split \
        else {"": chunks}
    for rel, group in groups.items():
        target = out.with_name(f"{out.stem}_{rel}{out.suffix}") if rel else out
        print(target.name, json.dumps(build_compact(group, target, built)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
