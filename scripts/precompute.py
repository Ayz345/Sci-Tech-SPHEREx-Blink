"""Precompute the static gallery: web/data/{key}/ for the named targets (data contract v1, PLAN.md 5).

    python scripts/precompute.py                      # all five targets
    python scripts/precompute.py wise-0855 barnard    # some
    python scripts/precompute.py --out web/data --bands D6 W
    python scripts/precompute.py --final              # release build: only from committed code

Regenerated web/data is NOT committed except the single --final build (PLAN.md 14.6).

Per target it writes manifest.json, {band}.json, {epoch}_{band}.f32 (+ _weight, _flagfrac), gaia.json,
spectrum.json and provenance.json into a temporary folder and swaps it in when complete, so the page never
sees a half-written target. index.json lists every target folder present.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import platform
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import astropy  # noqa: E402
import numpy as np  # noqa: E402

import spx  # noqa: E402
from server import pipeline as P  # noqa: E402
from spx import access  # noqa: E402
from spx.targets import TARGETS  # noqa: E402

log = logging.getLogger("precompute")
LAYERS = ("IMAGE", "FLAGS", "VARIANCE")


def build_target(key: str, out_root: Path, bands=None) -> dict:
    t0 = time.time()
    f = P.open_field(target=key)
    tmp = out_root / f".{key}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    report = {"key": key, "n_images": len(f.refs), "bands": {}}
    if not f.refs:
        P.write_json(tmp / "manifest.json", P.manifest(f))
        _swap(tmp, out_root / key)
        return report

    order = [b["id"] for b in f.band_defs()]
    if f.tinfo.get("default_band") in order:  # default band first
        order.remove(f.tinfo["default_band"])
        order.insert(0, f.tinfo["default_band"])
    if bands:
        order = [b for b in order if b in bands]
    status, nexp, by_det, nused = {}, {}, {}, {}
    for band in order:
        dets = f.detectors_for(band)
        st = {}
        for d in dets:  # one fetch per detector, reused by custom windows (e.g. W = D5 + D6)
            if d not in by_det:
                s = {}
                by_det[d], _ = P.fetch_band_cutouts(f, f"D{d}", LAYERS, stats=s)
                st[f"D{d}"] = s
        cuts = [c for d in dets for c in by_det[d]]
        bstats = {}
        bj, _, frames = P.build_band(f, band, tmp, cuts=cuts, stats=bstats)
        status[band] = "ready" if frames else "empty"
        nused[band] = [e["n_used"] for e in bj["epochs"]]
        n = P.band_n_exposures(f, band, cuts)
        if n is not None:
            nexp[band] = n
        fetched = {k: {kk: v[kk] for kk in ("n_refs", "n_ok", "n_skipped", "n_from_cache", "bytes", "seconds")}
                   for k, v in st.items()}
        report["bands"][band] = {"n_cutouts": len(cuts), "frames": [(fr.epoch, fr.n_used) for fr in frames],
                                 "t_coadd": round(bstats["t_coadd"], 1), "fetch": fetched}
        print(f"  {key} {band}: {len(cuts)} cutouts -> {len(frames)} frames "
              f"{[(fr.epoch_label, fr.n_used) for fr in frames]}  coadd {bstats['t_coadd']:.1f}s"
              + "".join(f"  fetch {k} {v['seconds']:.0f}s {v['bytes'] / 1e6:.0f}MB skip {v['n_skipped']}"
                        for k, v in fetched.items()), flush=True)

    # gaia
    try:
        gaia = P.gaia_sources(f)
    except Exception as e:  # the gallery must not depend on VizieR being up
        log.warning("gaia for %s failed: %r", key, e)
        gaia = []
    P.write_json(tmp / "gaia.json", gaia, indent=None)
    # spectrum at the target (detector bands only, each exposure once)
    all_cuts = [c for d in sorted(by_det) for c in by_det[d]]
    spec = P.spectrum(f, all_cuts, gaia=gaia)
    P.write_json(tmp / "spectrum.json", spec, indent=None)
    f.notes = list((f.tinfo or {}).get("notes", [])) + P.measured_notes(f, all_cuts, spec, gaia)
    # manifest last
    man = P.manifest(f, status=status, n_exposures=nexp, n_used=nused)
    P.write_json(tmp / "manifest.json", man)
    prov = {
        "schema": 1, "key": key, "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "spx_version": spx.__version__, **{f"spx_{k}": v for k, v in P.spx_commit().items()},
        "spx_frame_code_hash": spx.frame_code_hash(),   # access, coadd, display, photometry: compared by checks
        "spx_code_hash": spx.code_hash(),               # whole package: information
        "collections": list(access.COLLECTIONS), "n_images_sia2": len(f.refs),
        "n_images_per_detector": {f"D{d}": sum(r.detector == d for r in f.refs) for d in range(1, 7)},
        "n_cutouts_used_per_detector": {f"D{d}": len(v) for d, v in sorted(by_det.items())},
        "fetch": {"layers": list(LAYERS), "half_px": f.half_px, "cap_per_epoch_detector": P.MAX_PER_EPOCH_BAND,
                  "source": "S3 range reads, s3://nasa-irsa-spherex"},
        "grid_center": [float(f.center.ra.deg), float(f.center.dec.deg)],
        "fov_deg": f.fov_deg,
        "build_frames": P.BUILD_PARAMS,
        "bands": report["bands"],
        "gaia": {"catalog": f"{P.GAIA_TABLE} via {P.GAIA_TAP}", "g_max": P.GAIA_GMAX, "n": len(gaia)},
        "spectrum": {"n_points": len(spec.get("points", []))},
        "software": {"python": platform.python_version(), "numpy": np.__version__, "astropy": astropy.__version__},
        "seconds": round(time.time() - t0, 1),
    }
    P.write_json(tmp / "provenance.json", prov)
    _swap(tmp, out_root / key)
    report["seconds"] = prov["seconds"]
    return report


def _swap(tmp: Path, dest: Path) -> None:
    old = dest.with_name(f".{dest.name}.old")
    if old.exists():
        shutil.rmtree(old)
    if dest.exists():
        dest.rename(old)
    tmp.rename(dest)
    if old.exists():
        shutil.rmtree(old, ignore_errors=True)


def write_index(out_root: Path) -> dict:
    import json
    targets = []
    order = list(TARGETS)
    for d in sorted(out_root.iterdir(), key=lambda p: (order.index(p.name) if p.name in order else 99, p.name)):
        m = d / "manifest.json"
        if d.is_dir() and not d.name.startswith(".") and m.exists():
            targets.append({"key": d.name, "name": json.loads(m.read_text(encoding="utf-8"))["name"]})
    idx = {"schema": 1, "targets": targets}
    P.write_json(out_root / "index.json", idx)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("keys", nargs="*", default=list(TARGETS))
    ap.add_argument("--out", default=str(ROOT / "web" / "data"))
    ap.add_argument("--bands", nargs="*")
    ap.add_argument("--final", action="store_true",
                    help="the release build: refuses to run unless spx/, server/, scripts/ match the commit")
    a = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if a.final:
        c = P.spx_commit()
        if not c["commit"] or c["uncommitted_code"]:
            sys.exit(f"--final refused: code differs from commit {c['commit']}: {c['uncommitted_code']}")
    out_root = Path(a.out)
    out_root.mkdir(parents=True, exist_ok=True)
    for key in a.keys:
        if key not in TARGETS:
            sys.exit(f"unknown target {key}; choose from {list(TARGETS)}")
        print(f"{key}: building", flush=True)
        r = build_target(key, out_root, a.bands)
        print(f"{key}: done in {r.get('seconds', 0)}s, {r['n_images']} images", flush=True)
    print("index:", write_index(out_root))


if __name__ == "__main__":
    main()
