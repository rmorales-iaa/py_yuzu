#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import atexit
import argparse
import logging
import multiprocessing
import os
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Iterable

from util.display import show_progress
from util.io import clean_tmp_files

# MontagePy (system package: python3-montagepy)
from MontagePy.main import (
    mImgtbl,
    mMakeHdr,
    mOverlaps,
    mDiffFitExec,
    mBgModel,
    mBgExec,
    mAdd,
)
# Use the fast plane-to-plane projector directly for parallelization
from MontagePy import mProjectPP

# LEMON modules
import customparser
import defaults
import fitsimage
import keywords
import style
import util

logger = logging.getLogger(__name__)


class OptionGroup:
    """Compat wrapper that mimics optparse.OptionGroup using argparse groups."""
    def __init__(self, parser: argparse.ArgumentParser, title: str, description: str | None = None):
        self._group = parser.add_argument_group(title=title, description=description)

    def add_argument(self, *args, **kwargs):
        return self._group.add_argument(*args, **kwargs)

    # Backward-compat alias for older code that called add_option(...)
    add_option = add_argument


description = """
Use the Montage (Montage Astronomical Image Mosaic Engine) toolkit [1] to
assemble the input FITS images into a composite mosaic that preserves their
flux calibration and positional fidelity. This command now uses the MontagePy
Python bindings to run the standard Montage workflow (reproject, optional
background matching, and co-add).

The input FITS images, all of which must have WCS (astrometrically calibrated),
are reprojected onto a common coordinate system and combined into a mosaic.

[1] http://montage.ipac.caltech.edu/
"""

# Parser
parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_IMGS... OUTPUT_IMG"

# Flags / options
parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite output image if it already exists",
)

parser.add_argument(
    "--background-match",
    action="store_true",
    dest="background_match",
    help=(
        "include a background-matching step (model and remove background differences). "
        "This improves seams but takes longer."
    ),
)

parser.add_argument(
    "--no-reprojection",
    action="store_false",
    dest="reproject",
    default=True,
    help="(kept for backward compatibility) With MontagePy the template header controls orientation.",
)

parser.add_argument(
    "--combine",
    dest="combine",
    choices=("mean", "median", "count"),
    default="mean",
    help="how FITS images are combined: %(choices)s [default: %(default)s]",
)

parser.add_argument(
    "--filter",
    dest="filter",
    type=str,
    default=None,
    help=(
        "only combine FITS files taken in this photometric filter. "
        + defaults.desc["filter"]
    ),
)

parser.add_argument(
    "--cores",
    dest="ncores",
    type=int,
    default=multiprocessing.cpu_count(),
    help="number of worker processes for reprojection (use 1..N CPUs).",
)

# Argument group for FITS keywords
key_group = OptionGroup(parser, "FITS Keywords", keywords.group_description)
key_group.add_argument(
    "--filterk",
    dest="filterk",
    type=str,
    default=keywords.filterk,
    help="keyword for the observation filter name. Only relevant when using --filter. [default: %(default)s]",
)

# Positional args: paths (>=3): N input FITS + 1 output FITS
parser.add_argument(
    "paths",
    nargs="+",
    help="two or more input FITS files followed by the output FITS path",
)

customparser.clear_metavars(parser)


# ---------- helpers ----------
_FITS_EXTS = {".fits", ".fit", ".fts"}

def _iter_fits(dir_path: Path) -> list[Path]:
    return [p for p in sorted(Path(dir_path).iterdir()) if p.suffix.lower() in _FITS_EXTS]

def _reproj_one(src: str, dst: str, hdr: str) -> dict:
    # Fast plane-to-plane reprojection; falls back internally if needed
    return mProjectPP(src, dst, hdr)  # returns Montage status dict


def main(arguments: list[str] | None = None) -> int:
    """Entry point to run mosaicking."""
    if arguments is None:
        arguments = sys.argv[1:]

    opts = parser.parse_args(arguments)
    if len(opts.paths) < 3:
        parser.print_help()
        return 2

    input_paths = set(opts.paths[:-1])
    out_path = Path(opts.paths[-1])

    # Overwrite handling
    if out_path.exists() and not opts.overwrite:
        print(f
