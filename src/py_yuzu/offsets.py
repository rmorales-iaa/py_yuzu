#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compute image offsets between frames using WCS (default approach).

This command computes the pixel shift needed to align each image to a reference
image based on WCS. The reference image center (in world coordinates) is
projected into each image to obtain the offset.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import astropy.io.fits
import astropy.wcs

try:
    import style  # type: ignore
except (ImportError, ModuleNotFoundError):
    class _Style:
        prefix = ">> "
        error_exit_message = ""
    style = _Style()  # type: ignore

import fitsimage


description = """
Compute WCS-based pixel offsets between images.

By default the first input image is used as the reference. The output is a
tab-separated table with one row per image and the recommended shift (dx, dy)
to align it to the reference image center.
""".strip()

parser = argparse.ArgumentParser(
    prog="offsets",
    description=description,
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.add_argument("images", nargs="+", help="input FITS images")
parser.add_argument("--reference", dest="reference", default=None,
                    help="reference image (defaults to first input)")
parser.add_argument("--output", dest="output", default=None,
                    help="output table path (defaults to stdout)")
parser.add_argument("--skip-missing", action="store_true", default=False,
                    help="skip images without valid WCS instead of failing")


def _load_wcs(path: str) -> astropy.wcs.WCS:
    with astropy.io.fits.open(path) as hdul:
        header = hdul[0].header
    wcs = astropy.wcs.WCS(header)
    if not wcs.has_celestial:
        raise ValueError("WCS has no celestial coordinates")
    return wcs


def _fmt(val: float | None) -> str:
    if val is None or not math.isfinite(val):
        return "nan"
    return f"{val:.6f}"


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    opts = parser.parse_args(argv)

    images = [str(p) for p in opts.images]
    if not images:
        print(f"{style.prefix}Error. No input images provided.")
        print(getattr(style, "error_exit_message", ""))
        return 2

    ref_path = opts.reference or images[0]
    if ref_path not in images:
        images = [ref_path] + images

    if not Path(ref_path).exists():
        print(f"{style.prefix}Error. Reference image not found: {ref_path}")
        print(getattr(style, "error_exit_message", ""))
        return 1

    try:
        ref_img = fitsimage.FITSImage(ref_path)
        ref_ra, ref_dec = ref_img.center_wcs()
        ref_center = ref_img.center
        ref_cx, ref_cy = float(ref_center[0]), float(ref_center[1])
    except Exception as e:
        print(f"{style.prefix}Error. Reference image lacks valid WCS: {e}")
        print(getattr(style, "error_exit_message", ""))
        return 1

    out_fd = None
    try:
        if opts.output:
            out_fd = open(opts.output, "w", encoding="utf-8")
        else:
            out_fd = sys.stdout

        out_fd.write("# path\tdx\tdy\tx_ref\ty_ref\tref_cx\tref_cy\tstatus\n")

        for path in images:
            if not Path(path).exists():
                msg = f"{style.prefix}Warning: image not found, skipping: {path}"
                if out_fd is sys.stdout:
                    print(msg)
                else:
                    sys.stderr.write(msg + "\n")
                continue

            try:
                wcs = _load_wcs(path)
                x_ref, y_ref = wcs.all_world2pix([[ref_ra, ref_dec]], 1)[0]
                dx = ref_cx - float(x_ref)
                dy = ref_cy - float(y_ref)
                status = "ok"
            except Exception as e:
                if not opts.skip_missing:
                    print(f"{style.prefix}Error. WCS failed for {path}: {e}")
                    print(getattr(style, "error_exit_message", ""))
                    return 1
                dx = dy = x_ref = y_ref = float("nan")
                status = "no_wcs"

            line = "\t".join(
                [
                    str(path),
                    _fmt(dx),
                    _fmt(dy),
                    _fmt(x_ref),
                    _fmt(y_ref),
                    _fmt(ref_cx),
                    _fmt(ref_cy),
                    status,
                ]
            )
            out_fd.write(line + "\n")

        if out_fd is not sys.stdout:
            print(f"{style.prefix}Offsets written to {opts.output}")

    finally:
        if out_fd not in (None, sys.stdout):
            out_fd.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
