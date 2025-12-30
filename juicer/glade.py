#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import os
import functools

# Base dir of this package
_PKG_DIR = Path(__file__).resolve().parent

# Allow overriding the UI dir via env var if packaged differently
_ENV_DIR = os.environ.get("LEMON_GLADE_DIR", "")

# Candidate locations for the UI files
_CANDIDATES = [
    Path(_ENV_DIR) if _ENV_DIR else None,
    _PKG_DIR / "gui",              # juicer/gui/*.glade
    _PKG_DIR / "data" / "gui",     # juicer/data/gui/*.glade (optional layout)
]

# Pick the first existing directory
for _d in _CANDIDATES:
    if _d and _d.is_dir():
        GLADE_DIR = _d
        break
else:
    # Fallback to package dir (won't crash on import; paths may not exist)
    GLADE_DIR = _PKG_DIR

def _get(name: str) -> str:
    """Return absolute path to a UI file as a string (GTK expects str)."""
    return str((GLADE_DIR / name).resolve())

# Public constants used elsewhere
GUI_MAIN = _get("main.glade")
GUI_ABOUT = _get("about.glade")
GUI_OVERVIEW = _get("overview.glade")
LOADING_DIALOG = _get("loading-dialog.glade")
STAR_DETAILS = _get("star-details.glade")
SNR_THRESHOLD_DIALOG = _get("snr-threshold-dialog.glade")
EXPORT_CURVE_DIALOG = _get("curve-dump-dialog.glade")
FINDING_CHART_DIALOG = _get("finding-chart-dialog.glade")
CHART_PREFERENCES_DIALOG = _get("chart-preferences-dialog.glade")
