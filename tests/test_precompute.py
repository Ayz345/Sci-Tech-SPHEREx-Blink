"""scripts/precompute.py keeps the files other scripts add to a target folder (offline).
    python -m pytest -q tests/test_precompute.py
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pc():
    spec = importlib.util.spec_from_file_location("precompute", ROOT / "scripts" / "precompute.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _folder(path: Path, crval, companions=True):
    path.mkdir(parents=True)
    grid = {"ctype": ["RA---TAN", "DEC--TAN"], "crval": list(crval), "crpix": [70.5, 70.5],
            "cdelt": [-0.000833, 0.000833], "shape": [140, 140]}
    (path / "manifest.json").write_text(json.dumps({"schema": 1, "grid": grid}), encoding="utf-8")
    (path / "0_D1.f32").write_bytes(b"new" if not companions else b"old")
    if companions:
        for name in ("wise.json", "wise_W1.f32", "wise_W2.f32", "catwise.json", "candidates.json"):
            (path / name).write_text(f"old {name}", encoding="utf-8")


def test_swap_keeps_companions_same_grid(tmp_path):
    pc = _pc()
    dest, tmp = tmp_path / "wise-0855", tmp_path / ".wise-0855.tmp"
    _folder(dest, (133.76, -7.24))
    _folder(tmp, (133.76, -7.24), companions=False)
    warn = pc._swap(tmp, dest)
    assert not tmp.exists() and (dest / "0_D1.f32").read_bytes() == b"new"
    for name in pc.COMPANIONS:
        assert (dest / name).read_text(encoding="utf-8") == f"old {name}"
    assert warn == ["wise-0855: frames rebuilt: run scripts/make_candidates.py wise-0855"]


def test_swap_warns_when_the_grid_moves(tmp_path):
    pc = _pc()
    dest, tmp = tmp_path / "wise-0855", tmp_path / ".wise-0855.tmp"
    _folder(dest, (133.76, -7.24))
    _folder(tmp, (133.76, -7.24 + 20 / 3600), companions=False)
    (tmp / "candidates.json").write_text("fresh", encoding="utf-8")       # already made: not overwritten
    warn = pc._swap(tmp, dest)
    assert (dest / "candidates.json").read_text(encoding="utf-8") == "fresh"
    assert any("make_wise_images.py wise-0855" in w for w in warn)
    assert any("make_catwise.py --fetch wise-0855" in w and "20.0 arcsec" in w for w in warn)
    assert not any("make_candidates" in w for w in warn)


def test_swap_first_build_has_nothing_to_carry(tmp_path):
    pc = _pc()
    tmp = tmp_path / ".new.tmp"
    _folder(tmp, (10.0, 10.0), companions=False)
    assert pc._swap(tmp, tmp_path / "new") == [] and (tmp_path / "new" / "manifest.json").exists()
