"""spx: SPHEREx blink tool core (data access, co-adding, photometry)."""
__version__ = "0.1"


#: The files that decide the frames and the photometry. Their hash ties a gallery to derived checks.
FRAME_FILES = ("access.py", "coadd.py", "display.py", "photometry.py")


def _hash(files) -> str:
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parent
    h = hashlib.sha1()
    for name in sorted(files):
        f = root / name
        h.update(f.name.encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def frame_code_hash() -> str:
    """Hash of the frame-making code only (FRAME_FILES, sorted, LF). Written as spx_frame_code_hash into the
    gallery's provenance.json and into derived result files (e.g. saturation_check_v2.json); a check compares the
    two RECORDED values."""
    return _hash(FRAME_FILES)


def code_hash() -> str:
    """Hash of the whole spx package (spx/*.py, sorted, LF); information only (e.g. /api/health)."""
    from pathlib import Path
    return _hash(f.name for f in Path(__file__).resolve().parent.glob("*.py"))
