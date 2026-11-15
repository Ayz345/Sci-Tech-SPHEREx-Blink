"""Shared test setup."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_image_index(monkeypatch):
    """Tests see no image index unless they set one (tests/test_s3index.py does): the repository's
    server/index/spx_index.npz would otherwise feed every image list."""
    try:
        from server import pipeline as P
    except Exception:          # tests that do not import the server
        return
    monkeypatch.setattr(P, "_index_state", {"loaded": True, "indexes": []})
