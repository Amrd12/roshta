"""
tests/conftest.py — Global test configuration.

Ensures that temporary directories always fallback to project-local '.tmp/'
if Windows %TEMP% or %TMP% is missing, locked, or inaccessible.
"""
from __future__ import annotations

import os
import tempfile
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_TMP = os.path.join(PROJECT_ROOT, ".tmp")


def _init_tempdir():
    os.makedirs(LOCAL_TMP, exist_ok=True)
    # Ensure system TEMP/TMP directories exist if env vars are present
    for k in ("TEMP", "TMP"):
        p = os.environ.get(k)
        if p:
            try:
                os.makedirs(p, exist_ok=True)
            except Exception:
                pass
    try:
        probe = tempfile.mkdtemp(prefix="_probe_")
        os.rmdir(probe)
    except Exception:
        # Fallback globally to project-local .tmp
        tempfile.tempdir = LOCAL_TMP


_init_tempdir()
