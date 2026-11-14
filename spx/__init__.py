"""spx: SPHEREx blink tool core (data access, co-adding, photometry)."""
__version__ = "0.1"


def code_hash() -> str:
    """Short hash of the spx package source (spx/*.py, sorted by name, line endings normalised to LF).

    Written as spx_code_hash into the gallery's provenance.json and into derived result files (e.g. the co-add
    session's saturation_check_v2.json) so a check can tell whether a result was made by the same code.
    """
    import hashlib
    from pathlib import Path
    h = hashlib.sha1()
    for f in sorted(Path(__file__).resolve().parent.glob("*.py"), key=lambda q: q.name):
        h.update(f.name.encode())
        h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]
