#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import collections
import fnmatch
import logging
import operator
import os.path
import re
import shutil
import sys
from pathlib import Path

import numpy
from astropy.io import fits as pyfits

# LEMON modules
import customparser
import fitsimage
import keywords
import style
import util
from util.io import owner_writable


# --- argparse helpers ---------------------------------------------------------

class OptionGroup:
    """optparse-like group wrapper that delegates to argparse groups."""
    def __init__(self, parser: argparse.ArgumentParser, title: str, description: str | None = None):
        self._group = parser.add_argument_group(title=title, description=description)

    def add_option(self, *opts, **kwargs):
        return self._group.add_argument(*opts, **kwargs)


class CSVList(argparse.Action):
    """Parse a single comma-separated string into a list (no empty items)."""
    def __call__(self, parser, namespace, values, option_string=None):
        items = [s.strip() for s in values.split(",") if s.strip()]
        setattr(namespace, self.dest, items)


# --- description --------------------------------------------------------------

description = """
The purpose of this module, the first of the LEMON pipeline, is to
automatically detect all the FITS files belonging to an observation campaign
and save them to another directory. This is particularly needed when reducing
images taken at observatories that enforce specific naming conventions, such as
using a different directory for each night. In situations like that, the
astronomer may easily end up with hundreds of images scattered over dozens of
directories, many times also with duplicate filenames. As if, for example, the
first image of the night must be named 'ferM_0001.fits' and so on, there will
be many files with that name; this script can sequentially rename all images.

In short, the script: (a) finds all FITS files in a directory tree, (b) discards
those saturated (median ADUs above a threshold) or whose OBJECT keyword does
not match any provided patterns (e.g., 'bias', 'skyflat*', 'ngc2264*'),
(c) sorts them by observation date, and (d) copies them to the output directory,
renaming them sequentially. Some statistics are displayed before exit. It
enforces that all imported images share the same size: if multiple dimensions
exist, only the most common size is imported; the rest are ignored.
"""

# --- parser -------------------------------------------------------------------

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_DIRS... OUTPUT_DIR"

parser.add_argument(
    "--object",
    dest="objectn",
    action=CSVList,
    default=["*"],
    help=(
        "Comma-separated, case-insensitive Unix shell patterns of object names "
        "to import (matched against --objectk). Images that match none are ignored. "
        "[default: %(default)s]"
    ),
)

parser.add_argument(
    "--pattern",
    action="store",
    type=str,
    dest="pattern",
    help=(
        "Unix shell filename pattern that FITS images must match to be considered "
        "while scanning directories. Non-matching files are ignored."
    ),
)

parser.add_argument(
    "--counts",
    action="store",
    type=int,
    dest="max_counts",
    default=None,
    help=(
        "Median number of ADUs at which saturation is assumed. Images above this "
        "value are ignored. If unset, no image is discarded due to median ADUs. "
        "[default: %(default)s]"
    ),
)

parser.add_argument(
    "--filename",
    action="store",
    type=str,
    dest="filename",
    help=(
        "Base name for all output images; the sequence number will be appended. "
        "If not set, it is auto-detected as the most common prefix among inputs."
    ),
)

parser.add_argument(
    "--follow",
    action="store_true",
    default=False,
    dest="followlinks",
    help=(
        "Follow directory symlinks when walking input trees (may cause recursion "
        "loops if links point to parents)."
    ),
)

parser.add_argument(
    "--exact",
    action="store_true",
    default=False,
    dest="exact",
    help=(
        "Make exact copies (only rename). FITS headers are not altered and the "
        "--uik keyword is not added. Copy integrity verified via SHA-1."
    ),
)

key_group = OptionGroup(parser, "FITS Keywords", keywords.group_description)
key_group.add_option(
    "--datek", action="store", type=str, dest="datek",
    default=keywords.datek, help=keywords.desc["datek"]
)
key_group.add_option(
    "--timek", action="store", type=str, dest="timek",
    default=keywords.timek, help=keywords.desc["timek"]
)
key_group.add_option(
    "--expk", action="store", type=str, dest="exptimek",
    default=keywords.exptimek, help=keywords.desc["exptimek"]
)
key_group.add_option(
    "--objectk", action="store", type=str, dest="objectk",
    default=keywords.objectk, help=keywords.desc["objectk"]
)
key_group.add_option(
    "--uik",
    action="store",
    type=str,
    dest="uncimgk",
    default=keywords.uncimgk,
    help=(
        "Header keyword in which to store the absolute path to the original image "
        "we imported (useful for later saturation checks on uncalibrated data)."
    ),
)

customparser.clear_metavars(parser)

logger = logging.getLogger(__name__)


# --- main ---------------------------------------------------------------------

def main(arguments: list[str] | None = None) -> int:
    """Main entry point."""
    if arguments is None:
        arguments = sys.argv[1:]
    args = parser.parse_args(args=arguments)

    # Need at least one INPUT_DIR and one OUTPUT_DIR
    if len(args.__dict__.get("paths", [])) > 0:
        # (compat with other customparser configs)
        pass

    # argparse doesn't enforce positional for a variable number; emulate optparse layout:
    # INPUT_DIRS... OUTPUT_DIR  -> everything except last is input
    # We read them directly from sys.argv via parser? Better: parse unknowns? Simpler:
    # After parsing, remaining positionals are in 'args' because we didn't define them.
    # So re-parse raw arguments to extract positionals:
    # But our parser doesn't define positionals; we can just split from sys.argv manually.

    # Reconstruct positionals from the original CLI (anything not starting with '-')
    # Safer: use parse_known_args
    args, unknown = parser.parse_known_args(arguments)
    positionals = [x for x in unknown if not x.startswith("-")]
    if not positionals:
        # fall back to scanning sys.argv after options
        positionals = [x for x in arguments if not x.startswith("-")]

    if len(positionals) < 2:
        parser.print_help()
        return 2

    input_dirs = positionals[:-1]
    output_dir = positionals[-1]

    # Validate input dirs exist
    for path in input_dirs:
        if not Path(path).exists():
            print(f"{style.prefix}The input directory '{path}' does not exist. Exiting.")
            return 1

    # Input and output must be different
    for path in input_dirs:
        if Path(path).resolve() == Path(output_dir).resolve():
            print(f"{style.prefix}[INPUT_DIRS] and OUTPUT_DIR must be different. Exiting.")
            return 1

    # Ensure output directory exists
    util.determine_output_dir(output_dir)

    # Find regular files, then detect FITS among them
    print(f"{style.prefix}Indexing regular files within directory trees starting at INPUT_DIRS...", end="")
    sys.stdout.flush()
    files_paths = fitsimage.find_files(
        input_dirs, followlinks=args.followlinks, pattern=args.pattern
    )
    print(" done.")

    print(f"{style.prefix}Detecting FITS images among the {len(files_paths)} indexed regular files...", end="")
    sys.stdout.flush()

    images_set: set[fitsimage.FITSImage] = set()
    util.show_progress(0.0)
    for idx, path in enumerate(files_paths):
        try:
            images_set.add(fitsimage.FITSImage(path))
        except fitsimage.NonStandardFITS:
            pass
        finally:
            fraction = ((idx + 1) / float(len(files_paths))) * 100.0 if files_paths else 100.0
            util.show_progress(fraction)
    util.show_progress(100.0)
    print()

    if not images_set:
        print(f"{style.prefix}No FITS files were found. Exiting.")
        return 1
    else:
        print(f"{style.prefix}{len(images_set)} FITS files detected.")

    # Enforce same size: keep the most common dimensions
    print(style.prefix)
    print(f"{style.prefix}Checking the sizes of the detected images...", end="")
    sys.stdout.flush()
    img_sizes: dict[tuple[int, int], int] = collections.defaultdict(int)
    for img in images_set:
        img_sizes[img.size] += 1
    print(" done.")

    x_size, y_size = max(img_sizes.keys(), key=img_sizes.get)[:2]
    if len(img_sizes) == 1:
        print(f"{style.prefix}All FITS images share the same size: {x_size} x {y_size} pixels.")
    else:
        print(f"{style.prefix}Multiple sizes detected among FITS images.")
        print(
            f"{style.prefix}Discarding images not sized {x_size} x {y_size} pixels (most common)...",
            end="",
        )
        sys.stdout.flush()
        old_size = len(images_set)
        images_set = {img for img in images_set if img.size == (x_size, y_size)}
        print(" done.")

        if not images_set:
            print(f"{style.prefix}There are no FITS files left. Exiting.")
            return 1
        else:
            print(
                f"{style.prefix}{old_size - len(images_set)} files discarded by size; "
                f"{len(images_set)} remain."
            )

    # Filter by OBJECT keyword patterns and saturation threshold
    print(style.prefix)
    print(
        f"{style.prefix}Importing only those FITS files whose '{args.objectk}' keyword exists "
        "and matches one of the following Unix patterns:"
    )
    print(f"{style.prefix}  {args.objectn}")

    object_set: set[fitsimage.FITSImage] = set()
    saturated_excluded = 0
    non_match_excluded = 0

    for img in images_set:
        try:
            object_name = img.read_keyword(args.objectk)
        except KeyError:
            continue

        matched = False
        for pattern in args.objectn:
            regexp = re.compile(fnmatch.translate(pattern), re.IGNORECASE)
            if regexp.match(object_name):
                # If --counts provided, ensure not saturated
                if args.max_counts is not None:
                    with pyfits.open(img.path, readonly=True) as hdu:
                        median_counts = float(numpy.median(hdu[0].data))
                    if median_counts > args.max_counts:
                        print(
                            f"{style.prefix}{img.path} excluded (matched, but saturated with "
                            f"{median_counts:.0f} ADUs)"
                        )
                        saturated_excluded += 1
                        matched = True  # matched name but excluded by saturation
                        break
                print(f"{style.prefix}{img.path} imported ({object_name} matches '{pattern}')")
                object_set.add(img)
                matched = True
                break

        if not matched:
            print(f"{style.prefix}{img.path} excluded ({object_name} matches none)")
            non_match_excluded += 1

    if not saturated_excluded and not non_match_excluded:
        print(f"{style.prefix}No images were filtered out. Hooray!")
    if saturated_excluded:
        print(
            f"{style.prefix}{saturated_excluded} files discarded due to saturation "
            f"(> {args.max_counts} ADUs)."
        )
    if non_match_excluded:
        print(
            f"{style.prefix}{non_match_excluded} files discarded due to non-matching object names."
        )

    if not object_set:
        print(f"{style.prefix}There are no FITS files left. Exiting.")
        return 1

    # Sort by observation date
    print(style.prefix)
    print(
        f"{style.prefix}Sorting FITS files by observation date "
        f"[keywords: {args.datek}/{args.timek}/{args.exptimek}]...",
        end="",
    )
    sys.stdout.flush()

    kwargs = dict(date_keyword=args.datek, time_keyword=args.timek, exp_keyword=args.exptimek)
    get_date = operator.methodcaller("date", **kwargs)

    sorted_imgs = []
    for img in object_set:
        try:
            _ = get_date(img)
            sorted_imgs.append(img)
        except Exception:
            # skip images whose date cannot be parsed
            pass

    sorted_imgs.sort(key=get_date)

    difference = len(object_set) - len(sorted_imgs)
    if difference:
        print()
        print(
            f"{style.prefix}{difference} files were discarded because the observation date "
            "could not be read or parsed according to FITS standards."
        )
        if not sorted_imgs:
            print(f"{style.prefix}There are no FITS files left. Exiting.")
            return 1
    else:
        print(" done.")

    # Auto-detect base filename if not given
    if not args.filename:
        print(style.prefix)
        print(f"{style.prefix}Detecting the most common base name among input files...", end="")
        sys.stdout.flush()

        prefixes: dict[str, int] = collections.defaultdict(int)
        for prefix in (img.prefix for img in sorted_imgs):
            prefixes[prefix] += 1
        args.filename = max(prefixes, key=prefixes.get)
        print(" done.")

    print(f"{style.prefix}Imported FITS filenames will start with: '{args.filename}'")

    # Copy files and rename sequentially
    assert len(sorted_imgs) > 0
    ndigits = len(str(len(sorted_imgs) - 1))
    print(
        f"{style.prefix}{ndigits} digits are needed to enumerate {len(sorted_imgs)} files."
    )

    print(style.prefix)
    print(f"{style.prefix}Copying FITS files to '{output_dir}'...")

    for index, fits_file in enumerate(sorted_imgs):
        dest_name = f"{args.filename}{index:0{ndigits}d}.fits"
        dest_path = Path(output_dir) / dest_name

        shutil.copy2(fits_file.path, dest_path)
        owner_writable(dest_path, True)  # chmod u+w

        dest_img = fitsimage.FITSImage(dest_path)

        if not args.exact:
            msg1 = f"File imported by LEMON on {util.utctime()}"
            dest_img.add_history(msg1)

            if args.uncimgk:
                comment = "before any calibration task"
                dest_img.update_keyword(args.uncimgk, Path(fits_file.path).resolve(), comment=comment)
                dest_img.add_history(f"[Import] Original image: {Path(fits_file.path).resolve()}")
        else:
            # Exact copy: verify SHA-1
            if fits_file.sha1sum != dest_img.sha1sum:
                raise IOError(f"copy of {fits_file.path} not identical (SHA-1 differs)")

        print(f"{style.prefix}`{fits_file.path}' -> `{dest_path}'")

    # Stats
    print(style.prefix)
    ifraction = (len(sorted_imgs) / float(len(images_set))) * 100.0 if images_set else 0.0
    print(f"{style.prefix}FITS files detected: {len(images_set)}")
    print(f"{style.prefix}FITS files successfully imported: {len(sorted_imgs)} ({ifraction:.2f}%)")

    total_size = 0.0
    for fits_file in sorted_imgs:
        total_size += os.path.getsize(fits_file.path)  # bytes
    print(f"{style.prefix}Total size of imported files: {total_size / (1024.0 ** 2):.2f} MB")
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
