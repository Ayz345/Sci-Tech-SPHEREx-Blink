"""Where the validation scripts in tests/ write their results.

Default run (pytest, or `python tests/test_x.py`): everything goes to cache/test_results/, which is never tracked.
The tracked files in handoff/results/ are read by spx/mpc.py, server/pipeline.py and scripts/acceptance.py, so a test
run must not change them by accident.

Writing a tracked result is explicit: `python tests/test_x.py --write` (or SPX_WRITE_RESULTS=1 in the environment).
A JSON result is then merged into the existing file: the new top-level sections replace the ones with the same name,
and sections this run did not produce (for example "second_object" in astrometry_validation_v2.json) are kept.
Non-JSON results (CSV, PNG, PSV) are whole files and are replaced.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACKED = ROOT / "handoff" / "results"
SCRATCH = ROOT / "cache" / "test_results"


def write_enabled() -> bool:
    return "--write" in sys.argv[1:] or os.environ.get("SPX_WRITE_RESULTS") == "1"


def out_path(name: str) -> Path:
    """Destination of result file `name`: handoff/results/ with --write, cache/test_results/ otherwise."""
    d = TRACKED if write_enabled() else SCRATCH
    d.mkdir(parents=True, exist_ok=True)
    return d / name


def write_json(name: str, data: dict, **dump_kw) -> Path:
    """Write a JSON result, keeping top-level sections of an existing file that `data` does not replace."""
    path = out_path(name)
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            old = None
        if isinstance(old, dict):
            data = {**old, **data}
    dump_kw.setdefault("indent", 1)
    path.write_text(json.dumps(data, **dump_kw), encoding="utf-8", newline="\n")
    print(f"result written: {path.relative_to(ROOT)}")
    return path


def write_text(name: str, text: str) -> Path:
    path = out_path(name)
    path.write_text(text, encoding="utf-8", newline="\n")
    print(f"result written: {path.relative_to(ROOT)}")
    return path
