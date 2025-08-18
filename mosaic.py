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

from util.display import show_progress
from util.io import clean_tmp_files

# MontagePy (system package: python3-montagepy)
from MontagePy.main import (
    mImgtbl,
    mMakeHdr,
    mProjExec,
    mOverlaps,
    mDiffFitExec,
    mBgModel,
    mBgExec,
    mAdd,
)

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
    help="(compat) number of cores. MontagePy does not use MPI; this option is ignored if >1.",
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
        print(f"{style.prefix}Error. The output file '{out_path}' already exists.")
        print(style.error_exit_message)
        return 1
    if out_path.exists() and opts.overwrite:
        try:
            out_path.unlink()
        except Exception:
            pass
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # MontagePy does not support MPI; ignore cores > 1
    if opts.ncores > 1:
        print(f"{style.prefix}Note: MontagePy doesn't use MPI; ignoring --cores={opts.ncores}.")
        opts.ncores = 1

    # Map each filter to a list of FITSImage objects
    files = fitsimage.InputFITSFiles()

    print(f"{style.prefix}Making sure the {len(input_paths)} input paths are FITS images...")

    show_progress(0.0)
    for index, path in enumerate(input_paths):
        try:
            img = fitsimage.FITSImage(path)
            pfilter = img.pfilter(opts.filterk) if opts.filter else None
            files[pfilter].append(img)
        except fitsimage.NonStandardFITS:
            print()
            msg = f"'{path}' is not a standard FITS file"
            raise fitsimage.NonStandardFITS(msg)

        percentage = (index + 1) / len(input_paths) * 100.0
        show_progress(percentage)
    print()  # newline after progress

    # If --filter provided, keep only that filter
    if opts.filter:
        print(f"{style.prefix}{len(files.keys())} different photometric filters were detected:")

        total_imgs = sum(len(v) for v in files.values()) or 1
        for pfilter, images in sorted(files.items()):
            pct = (len(images) / total_imgs) * 100.0
            print(f"{style.prefix} {pfilter}: {len(images)} files ({pct:.2f} %)")

        print(f"{style.prefix}Ignoring images not taken in the '{opts.filter}' photometric filter...", end="")
        sys.stdout.flush()

        discarded = 0
        to_delete = [pf for pf in list(files.keys()) if pf != opts.filter]
        for pf in to_delete:
            discarded += len(files[pf])
            del files[pf]

        if not files:
            print()
            print(f"{style.prefix}Error. No image was taken in the '{opts.filter}' filter.")
            print(style.error_exit_message)
            return 1
        else:
            print(" done.")
            kept = sum(len(v) for v in files.values())
            print(f"{style.prefix}{kept} images taken in the '{opts.filter}' filter, {discarded} were discarded.")

    # Ensure all images have WCS info
    for img in files:
        img.center_wcs()  # may raise NoWCSInformationError

    # Prepare temp input dir with symlinks to input images
    pid = os.getpid()
    suffix = f"_LEMON_{pid}_mosaic"

    input_dir = tempfile.mkdtemp(suffix=suffix + "_input")
    atexit.register(clean_tmp_files, input_dir)

    for img in files:
        src_path = Path(img.path).resolve()
        link_name = Path(input_dir) / src_path.name
        os.symlink(src_path, link_name)

    # Prepare temp working dir
    work_dir = tempfile.mkdtemp(suffix=suffix + "_output")
    atexit.register(clean_tmp_files, work_dir)

    # --- MontagePy workflow ---
    rimages = Path(work_dir) / "rimages.tbl"   # table of raw inputs
    pimages = Path(work_dir) / "pimages.tbl"   # table of projected images
    cimages = Path(work_dir) / "cimages.tbl"   # table of corrected images
    diffs   = Path(work_dir) / "diffs.tbl"
    fits    = Path(work_dir) / "fits.tbl"
    hdr     = Path(work_dir) / "mosaic.hdr"
    projdir = Path(work_dir) / "projected"
    corrdir = Path(work_dir) / "corrected"
    diffdir = Path(work_dir) / "diffs"
    projdir.mkdir(exist_ok=True)
    corrdir.mkdir(exist_ok=True)
    diffdir.mkdir(exist_ok=True)

    # 1) Index inputs and build a template header
    mImgtbl(str(input_dir), str(rimages))
    mMakeHdr(str(rimages), str(hdr))

    # 2) Reproject all inputs to the template geometry
    mProjExec(str(input_dir), str(rimages), str(hdr), projdir=str(projdir), quickMode=True)

    # Always build a table of projected images (used by coadd or bg-match)
    mImgtbl(str(projdir), str(pimages))
    use_dir = projdir
    table_for_add = pimages

    # 3) Optional background matching
    if opts.background_match:
        mOverlaps(str(pimages), str(diffs))
        mDiffFitExec(str(projdir), str(diffs), str(hdr), str(diffdir), str(fits))
        corrections = Path(work_dir) / "corrections.tbl"
        mBgModel(str(pimages), str(fits), str(corrections))
        mBgExec(str(projdir), str(pimages), str(corrections), str(corrdir))
        mImgtbl(str(corrdir), str(cimages))
        use_dir = corrdir
        table_for_add = cimages

    # 4) Co-add to make the mosaic — write DIRECTLY to final output path
    coadd_mode = {"mean": 0, "median": 1, "count": 2}[opts.combine]
    mAdd(
        str(use_dir),           # directory with projected/corrected images
        str(table_for_add),     # table listing those images
        str(hdr),               # template header (controls geometry/orientation)
        str(out_path),          # <-- final mosaic file (no cross-device move needed)
        haveAreas=True,         # area files used for weighting
        coadd=coadd_mode,       # 0=MEAN, 1=MEDIAN, 2=COUNT
    )

    # Orientation note: the template header defines final orientation (e.g., North-up).
    print(f"{style.prefix}Template-driven orientation already applied; saving mosaic... done.")
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
