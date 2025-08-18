#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations
from __future__ import division

from pathlib import Path

# Copyright (c) 2012 Victor Terron. All rights reserved.
# Institute of Astrophysics of Andalusia, IAA-CSIC
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import argparse
import atexit
import collections
import hashlib
import itertools
import logging
import multiprocessing
import numpy
import os
import pwd
import shutil
import socket
import sys
import tempfile
import time
import warnings

# LEMON modules
import util
import astromatic
import customparser
import database
import defaults
import fitsimage
import json_parse
import keywords
import passband
import qphot
import seeing
import style
from util.queue import Queue
import re as _re
from util.display import show_progress,utctime
from util.io import owner_writable, clean_tmp_files
from util.coords import DD_to_HMS, DD_to_DMS
from util.log import func_catchall


# Map string type names (optparse style) to callables (argparse style)
_TYPE_MAP = {"str": str, "int": int, "float": float, "complex": complex}

def _sanitize_help_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    # Convert common optparse substitutions to argparse style
    text = text.replace("%default", "%(default)s").replace("%prog", "%(prog)s")
    # Escape any remaining lone '%' that aren't '%(' or '%%'
    text = _re.sub(r"%(?!\(|%)", "%%", text)
    return text

# Patch OptionGroup to sanitize help and map stringy types
class OptionGroup:
    def __init__(self, parser: argparse.ArgumentParser, title: str, description: str | None = None):
        self._group = parser.add_argument_group(title=title, description=_sanitize_help_text(description) if description else None)

    def add_option(self, *opts, **kwargs):
        t = kwargs.get("type")
        if isinstance(t, str):
            kwargs["type"] = _TYPE_MAP.get(t, str)
        if "help" in kwargs and kwargs["help"]:
            kwargs["help"] = _sanitize_help_text(kwargs["help"])
        return self._group.add_argument(*opts, **kwargs)

# Also sanitize direct parser.add_argument() calls and map stringy types
_argparse_add_argument = argparse.ArgumentParser.add_argument
def _safe_add_argument(self, *opts, **kwargs):
    t = kwargs.get("type")
    if isinstance(t, str):
        kwargs["type"] = _TYPE_MAP.get(t, str)
    if "help" in kwargs and kwargs["help"]:
        kwargs["help"] = _sanitize_help_text(kwargs["help"])
    return _argparse_add_argument(self, *opts, **kwargs)
argparse.ArgumentParser.add_argument = _safe_add_argument

# Keep optparse-era no-op for add_option_group
if not hasattr(argparse.ArgumentParser, "add_option_group"):
    argparse.ArgumentParser.add_option_group = lambda self, group: group


description = """
This module does aperture photometry on all the FITS images that it receives as
arguments. Astronomical objects are automatically detected, using SExtractor,
on the first image (which will be referred to from now on as the 'sources
image'), and then photometry is done for their celestial coordinates on the
rest of the images. The output is a LEMON database, which contains the
instrumental magnitudes (with 25 as zero point) and signal-to-noise ratio
for the stars detected in the sources image. Additionally, the instrumental
magnitudes of the stars in the sources image is also stored in the database,
in order to allow us to approximately estimate how bright each object is.

By default, the sizes of the aperture and sky annulus are determined by the
median FWHM of the images in each photometric filter, but different options
make it possible, among others, to directly specify those sizes in pixels, or
to use apertures and sky annuli that depend on the FWHM of each individual
image. The celestial coordinates of the objects on which to do photometry,
if known, can be listed in a text file, in this manner skipping the sources
detection step and working exclusively with the specified objects.
"""

# Message shown when sky annulus width < --min-sky
DANNULUS_TOO_THIN_MSG = "Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels"

# Process-shared queue (see util.Queue)
queue = Queue()


def get_fwhm(img, options):
    """Return the FWHM of the FITS image."""
    try:
        logging.debug("%s: reading FWHM from keyword '%s'", img.path, options.fwhmk)
        fwhm = img.read_keyword(options.fwhmk)
        logging.debug("%s: FWHM = %.3f (keyword '%s')", img.path, fwhm, options.fwhmk)
        return fwhm
    except KeyError:
        logging.debug("%s: keyword '%s' not found in header", img.path, options.fwhmk)

        if not isinstance(img, seeing.FITSeeingImage):
            logging.debug("%s: type of argument 'img' is not FITSeeingImage ('%s')", img.path, type(img))
            logging.debug("%s: calling FITSeeingImage.__init__() with 'img'", img.path)
            args = (img.path, options.maximum, options.margin)
            kwargs = dict(coaddk=options.coaddk)
            img = seeing.FITSeeingImage(*args, **kwargs)

        logging.debug("%s: calling FITSeeingImage.fwhm() to compute FWHM", img.path)
        mode = "mean" if options.mean else "median"
        kwargs = dict(per=options.per, mode=mode)
        fwhm = img.fwhm(**kwargs)
        logging.debug("%s: FITSeeingImage.fwhm() returned %.3f", img.path, fwhm)
        return fwhm



def parallel_photometry(args):
    """Function argument of map_async() to do photometry in parallel."""
    image, pparams, options = args

    logging.debug("Doing photometry on %s", image.path)
    logging.debug("%s: qphot aperture: %.3f", image.path, pparams.aperture)
    logging.debug("%s: qphot annulus: %.3f", image.path, pparams.annulus)
    logging.debug("%s: qphot dannulus: %.3f", image.path, pparams.dannulus)

    maximum = image.saturation(options.maximum, coaddk=options.coaddk)
    logging.debug("%s: saturation level = %d ADUs'", image.path, maximum)

    logging.info("Running qphot on %s", image.path)
    args = (
        image,
        options.coordinates,
        options.epoch,
        pparams.aperture,
        pparams.annulus,
        pparams.dannulus,
        maximum,
        options.datek,
        options.timek,
        options.exptimek,
        options.uncimgk,
    )
    img_qphot = qphot.run(*args, cbox=options.cbox)
    logging.info("Finished running qphot on %s", image.path)

    logging.debug("%s: qphot.run() returned %d records", image.path, len(img_qphot))

    pfilter = image.pfilter(options.filterk)
    logging.debug("%s: filter = %s", image.path, pfilter)

    kwargs = dict(date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek)
    unix_time = image.date(**kwargs)
    logging.debug("%s: observation date: %.2f (%s)", image.path, unix_time, utctime(unix_time))

    object_ = image.read_keyword(options.objectk)
    logging.debug("%s: object = %s", image.path, object_)

    airmass = image.read_keyword(options.airmassk)
    logging.debug("%s: airmass = %.4f", image.path, airmass)

    gain = options.gain or image.read_keyword(options.gaink)
    logging.debug("%s: gain (%s) = %.4f", image.path, "given by user" if options.gain else "read from header", gain)

    logging.debug("%s: calculating coordinates of field center", image.path)
    ra, dec = image.center_wcs()
    logging.debug("%s: RA (field center) = %.8f", image.path, ra)
    logging.debug("%s: DEC (field center) = %.8f", image.path, dec)

    args = (image.path, pfilter, unix_time, object_, airmass, gain, ra, dec)
    db_image = database.Image(*args)
    queue.put((db_image, pparams, img_qphot))
    logging.debug("%s: photometry result put into global queue", image.path)


# Parser setup
parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... SOURCES_IMG INPUT_IMGS... OUTPUT_DB"

parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite output database if it already exists",
)

parser.add_argument(
    "--filter",
    action="append",
    type=passband.Passband,
    dest="filters",
    default=None,
    help="do not do photometry on all the FITS files given as input, but only on those taken in this photometric "
         "filter. This option may be used multiple times. " + defaults.desc["filter"],
)

parser.add_argument(
    "--exclude",
    action="append",
    type=passband.Passband,
    dest="excluded_filters",
    default=None,
    help="ignore those FITS files taken in this photometric filter. May be used multiple times.",
)

parser.add_argument(
    "--cbox",
    action="store",
    type=float,
    dest="cbox",
    default=5.0,
    help="centering box width (pixels). Use 0 to skip centering [default: %(default)s]",
)

parser.add_argument(
    "--maximum",
    action="store",
    type=int,
    dest="maximum",
    default=defaults.maximum,
    help=defaults.desc["maximum"],
)

parser.add_argument(
    "--margin",
    action="store",
    type=int,
    dest="margin",
    default=defaults.margin,
    help=defaults.desc["margin"],
)

parser.add_argument(
    "--gain",
    action="store",
    type=float,
    dest="gain",
    default=None,
    help="CCD gain (e-/ADU). If given, do not read from header (--gaink).",
)

parser.add_argument(
    "--annuli",
    action="store",
    type=str,
    dest="json_annuli",
    default=None,
    help="read apertures and sky annuli from a JSON file output by the 'annuli' command.",
)

parser.add_argument(
    "--cores",
    action="store",
    type=int,
    dest="ncores",
    default=defaults.ncores,
    help=defaults.desc["ncores"],
)

parser.add_argument(
    "-v",
    "--verbose",
    action="count",
    dest="verbose",
    default=defaults.verbosity,
    help=defaults.desc["verbosity"],
)

# Groups
coords_group = OptionGroup(
    parser,
    "List of Coordinates",
    "By default, we run SExtractor on the sources image in order to detect the astronomical objects on which to do "
    "photometry. Alternatively, you can skip detection and provide a list of coordinates."
)
coords_group.add_option(
    "--coordinates",
    action="store",
    type=str,
    dest="coordinates",
    default=None,
    help="path to a file with RA,Dec (deg) and optional proper motions.",
)
coords_group.add_option(
    "--epoch",
    action="store",
    type=int,
    dest="epoch",
    default=2000,
    help="epoch of the coordinates [default: %(default)s]",
)

qphot_group = OptionGroup(
    parser,
    "Aperture Photometry (FWHM)",
    "qphot parameters expressed as multiples of the median FWHM per filter."
)
qphot_group.add_option("--aperture", action="store", type=float, dest="aperture", default=3.0,
                       help="aperture radius in FWHM [default: %(default)s]")
qphot_group.add_option("--annulus", action="store", type=float, dest="annulus", default=4.5,
                       help="inner sky annulus radius in FWHM [default: %(default)s]")
qphot_group.add_option("--dannulus", action="store", type=float, dest="dannulus", default=1.0,
                       help="sky annulus width in FWHM [default: %(default)s]")
qphot_group.add_option("--min-sky", action="store", type=float, dest="min", default=3.0,
                       help="minimum sky annulus width (pixels) [default: %(default)s]")
qphot_group.add_option("--individual-fwhm", action="store_true", dest="individual_fwhm",
                       help="derive parameters per-image from its FWHM, not the filter median.")

qphot_fixed = OptionGroup(
    parser,
    "Aperture Photometry (pixels)",
    "Specify fixed pixel sizes. If used, all three must be given."
)
qphot_fixed.add_option("--aperture-pix", action="store", type=float, dest="aperture_pix", default=None,
                       help="aperture radius (pixels)")
qphot_fixed.add_option("--annulus-pix", action="store", type=float, dest="annulus_pix", default=None,
                       help="inner sky annulus radius (pixels)")
qphot_fixed.add_option("--dannulus-pix", action="store", type=float, dest="dannulus_pix", default=None,
                       help="sky annulus width (pixels)")

fwhm_group = OptionGroup(
    parser,
    "FWHM",
    "If FWHM is not in the header (e.g., mosaics), compute it like the 'seeing' command."
)
fwhm_group.add_option("--snr-percentile", action="store", type=float, dest="per",
                      default=defaults.snr_percentile, help=defaults.desc["snr_percentile"])
fwhm_group.add_option("--mean", action="store_true", dest="mean", help=defaults.desc["mean"])

key_group = OptionGroup(parser, "FITS Keywords", keywords.group_description)
key_group.add_option("--objectk", action="store", type=str, dest="objectk",
                     default=keywords.objectk, help=keywords.desc["objectk"])
key_group.add_option("--filterk", action="store", type=str, dest="filterk",
                     default=keywords.filterk, help=keywords.desc["filterk"])
key_group.add_option("--datek", action="store", type=str, dest="datek",
                     default=keywords.datek, help=keywords.desc["datek"])
key_group.add_option("--timek", action="store", type=str, dest="timek",
                     default=keywords.timek, help=keywords.desc["timek"])
key_group.add_option("--expk", action="store", type=str, dest="exptimek",
                     default=keywords.exptimek, help=keywords.desc["exptimek"])
key_group.add_option("--coaddk", action="store", type=str, dest="coaddk",
                     default=keywords.coaddk, help=keywords.desc["coaddk"])
key_group.add_option("--gaink", action="store", type=str, dest="gaink",
                     default=keywords.gaink, help=keywords.desc["gaink"])
key_group.add_option("--fwhmk", action="store", type=str, dest="fwhmk",
                     default=keywords.fwhmk, help=keywords.desc["fwhmk"])
key_group.add_option("--airmk", action="store", type=str, dest="airmassk",
                     default=keywords.airmassk, help=keywords.desc["airmassk"])
key_group.add_option("--uik", action="store", type=str, dest="uncimgk",
                     default=keywords.uncimgk, help=keywords.desc["uncimgk"])

# Positionals (keep optparse feel: we’ll split them ourselves)
parser.add_argument("paths", nargs="+", help="SOURCES_IMG INPUT_IMGS... OUTPUT_DB")
customparser.clear_metavars(parser)


def main(arguments: list[str] | None = None) -> int:
    """Main entry point."""
    if arguments is None:
        arguments = sys.argv[1:]

    # argparse already expects strings
    options = parser.parse_args(args=arguments)

    # Logging level
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    if len(options.paths) < 3:
        parser.print_help()
        return 2

    sources_img_path = options.paths[0]
    input_paths = set(options.paths[1:-1])
    output_db_path = options.paths[-1]
    assert input_paths

    # Normalize --uncimgk empty => None
    if not options.uncimgk:
        options.uncimgk = None

    # --filter vs --exclude
    if options.filters and options.excluded_filters:
        print(f"{style.prefix}Error. The --filter and --exclude options are incompatible.")
        print(style.error_exit_message)
        return 1

    # Annuli JSON
    json_annuli = None
    fixed_annuli = False
    if options.json_annuli:
        if not Path(options.json_annuli).exists():
            print(f"{style.prefix}Error. The file '{options.json_annuli}' does not exist.")
            print(style.error_exit_message)
            return 1
        json_annuli = json_parse.CandidateAnnuli.load(options.json_annuli)
        print(f"{style.prefix}Photometric parameters read from the '{Path(options.json_annuli).name}' file.")

    # Fixed annuli triplet validation
    fixed_pix_count = sum(bool(x) for x in (options.aperture_pix, options.annulus_pix, options.dannulus_pix))
    if fixed_pix_count:
        if fixed_pix_count < 3:
            print(f"{style.prefix}Error. The --aperture-pix, --annulus-pix and --dannulus-pix options must be used together.")
            print(style.error_exit_message)
            return 1
        fixed_annuli = True

    if fixed_annuli and json_annuli:
        print(f"{style.prefix}Error. The --aperture-pix/--annulus-pix/--dannulus-pix options are incompatible with --annuli.")
        print(style.error_exit_message)
        return 1

    if options.individual_fwhm:
        if fixed_annuli:
            print(f"{style.prefix}Error. --aperture-pix/--annulus-pix/--dannulus-pix are incompatible with --individual-fwhm.")
            print(style.error_exit_message)
            return 1
        if json_annuli:
            print(f"{style.prefix}Error. --annuli is incompatible with --individual-fwhm.")
            print(style.error_exit_message)
            return 1

    # Validate positive aperture/annulus values
    fwhm_options = (options.aperture, options.annulus, options.dannulus)
    pixel_options = (options.aperture_pix, options.annulus_pix, options.dannulus_pix)
    if (not fixed_annuli and min(fwhm_options) <= 0) or (fixed_annuli and min(pixel_options) <= 0):
        print(f"{style.prefix}Error. The aperture, annulus and dannulus values must be positive numbers.")
        print(style.error_exit_message)
        return 1

    if (not fixed_annuli and options.aperture > options.annulus) or (
        fixed_annuli and options.aperture_pix > options.annulus_pix
    ):
        print(
            f"{style.prefix}Error. The aperture radius "
            f"({options.aperture_pix if fixed_annuli else options.aperture:.2f}) must be smaller than or equal\n"
            f"{style.prefix}to the inner radius of the sky annulus "
            f"({options.annulus_pix if fixed_annuli else options.annulus:.2f})"
        )
        print(style.error_exit_message)
        return 1

    # Load coordinates file if provided
    if options.coordinates:
        sources_coordinates = []
        for args_ in util.load_coordinates(options.coordinates):
            coords = astromatic.Coordinates(*args_)
            sources_coordinates.append(coords)
        if not sources_coordinates:
            print(f"{style.prefix}Error. Coordinates file '{options.coordinates}' is empty.")
            print(style.error_exit_message)
            return 1

    # Output DB existence
    if Path(output_db_path).exists():
        if not options.overwrite:
            print(f"{style.prefix}Error. The output database '{output_db_path}' already exists.")
            print(style.error_exit_message)
            return 1
        else:
            os.unlink(output_db_path)

    # Map filter -> list of images; image -> date
    print(f"{style.prefix}Examining the headers of the {len(input_paths)} FITS files given as input...")
    def get_date(img):
        return img.date(date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek)

    files = fitsimage.InputFITSFiles()
    img_dates: dict[str, float] = {}

    show_progress(0.0)
    for index, img_path in enumerate(input_paths):
        img = fitsimage.FITSImage(img_path)
        pfilter = img.pfilter(options.filterk)
        files[pfilter].append(img_path)
        date = get_date(img)
        img_dates[img_path] = date
        show_progress((index + 1) / float(len(input_paths)) * 100.0)
    print()  # newline

    print(f"{style.prefix}{len(files.keys())} different photometric filters were detected:")
    for pfilter, images in sorted(files.items()):
        percentage = (len(images) / float(len(files))) * 100.0 if len(files) else 0.0
        print(f"{style.prefix} {pfilter}: {len(images)} files ({percentage:.2f} %)")

    # Detect duplicate (date, filter) pairs
    print(f"{style.prefix}Making sure there are no images with the same date and filter...", end="")
    sys.stdout.flush()

    get_dict = lambda: collections.defaultdict(list)
    dates_counter: dict[float, dict[passband.Passband, list[str]]] = collections.defaultdict(get_dict)
    for pfilter, images in files.items():
        for img_path in images:
            date = img_dates[img_path]
            dates_counter[date][pfilter].append(img_path)

    discarded = 0
    for date, date_images in dates_counter.items():
        for pfilter, images in date_images.items():
            if len(images) > 1:
                if not discarded:
                    print()
                warnings.warn(f"{style.prefix}Warning! Multiple images have date {utctime(date)} and filter {pfilter}")
                del dates_counter[date][pfilter]
                for img in images:
                    discarded += files.remove(img)

    if not discarded:
        print("done.")
    else:
        if __debug__:
            dates = [img_dates[image] for image in files]
            assert len(set(dates)) == len(dates)
            assert discarded >= 2
        print(f"{style.prefix}{discarded} images had duplicate dates and were discarded, {len(files)} remain.")

    # --filter
    if options.filters:
        print(f"{style.prefix}Ignoring images not taken in any of the following filters:")
        for index, pf in enumerate(sorted(options.filters)):
            print(f"{style.prefix} ({index + 1}) {pf}")
        sys.stdout.flush()

        discarded = 0
        for pf, images in list(files.items()):
            if pf not in options.filters:
                discarded += len(images)
                del files[pf]

        if not files:
            print()
            print(f"{style.prefix}Error. No image was taken in any of the above filters.")
            print(style.error_exit_message)
            return 1
        else:
            former_total = len(files) + discarded
            percentage_kept = (len(files) / float(former_total)) * 100.0
            print(f"{style.prefix}{len(files)} images ({percentage_kept:.2f} %) taken in the above filters,", end=" ")
            percentage_discarded = (discarded / float(former_total)) * 100.0
            print(f"{discarded} ({percentage_discarded:.2f} %) were discarded.")

    # --exclude
    if options.excluded_filters:
        print(f"{style.prefix}Discarding images taken in any of the following filters:")
        for index, pf in enumerate(sorted(options.excluded_filters)):
            msg = f"{style.prefix} ({index + 1}) {pf}: "
            images = files[pf]
            if not images:
                msg += "(no images)"
            else:
                percentage = (len(images) / float(len(files))) * 100.0 if len(files) else 0.0
                msg += f"{len(images)} files to discard ({percentage:.2f} %)"
            print(msg)

        sys.stdout.flush()
        discarded = 0
        for pf, images in list(files.items()):
            if pf in options.excluded_filters:
                discarded += len(images)
                del files[pf]

        if not files:
            print()
            print(f"{style.prefix}Error. All images were discarded.")
            print(style.error_exit_message)
            return 1
        else:
            former_total = len(files) + discarded
            percentage_discarded = (discarded / float(former_total)) * 100.0
            print(f"{style.prefix}{discarded} images ({percentage_discarded:.2f} %) discarded,", end=" ")
            percentage_remain = (len(files) / float(former_total)) * 100.0
            print(f"{len(files)} ({percentage_remain:.2f} %) remain.")

    # JSON annuli must contain all needed filters
    if json_annuli:
        for pf in files.keys():
            if pf not in json_annuli.keys():
                json_basename = Path(options.json_annuli).name
                print(f"{style.prefix}Error. Photometric parameters for the '{pf}' filter not listed in '{json_basename}'. Wrong file, maybe?")
                print(style.error_exit_message)
                return 1

    print(f"{style.prefix}Sources image: {sources_img_path}")
    print(f"{style.prefix}Running SExtractor on the sources image...", end="")
    sys.stdout.flush()

    # Work on a temporary copy of the sources image
    basename = Path(sources_img_path).name
    root, extension = os.path.splitext(basename)
    tmp_fd, tmp_sources_img_path = tempfile.mkstemp(prefix=f"{root}_", suffix=extension)
    os.close(tmp_fd)
    shutil.copy2(sources_img_path, tmp_sources_img_path)
    owner_writable(tmp_sources_img_path, True)
    atexit.register(clean_tmp_files, tmp_sources_img_path)

    img = fitsimage.FITSImage(tmp_sources_img_path)
    img.delete_keyword(keywords.sex_catalog)

    args = (tmp_sources_img_path, sys.maxsize, options.margin)
    kwargs = dict(coaddk=options.coaddk)
    sources_img = seeing.FITSeeingImage(*args, **kwargs)
    print("done.")

    print(f"{style.prefix}Calculating coordinates of field center...", end="")
    sys.stdout.flush()
    ra, dec = sources_img.center_wcs()
    sources_img_ra, sources_img_dec = ra, dec
    print("done.")

    # Print center coords
    h, m, s = DD_to_HMS(sources_img_ra)
    print(f"{style.prefix}α = {sources_img_ra:11.7f} ({h:02d} {m:02d} {s:05.2f})")
    d, m2, s2 = DD_to_DMS(sources_img_dec)
    print(f"{style.prefix}δ = {sources_img_dec:11.7f} ({d:+02d} {m2:02d} {s2:05.2f})")

    # Coordinates list: from file or detections
    if options.coordinates:
        print(f"{style.prefix}Photometry will be done on the {len(sources_coordinates)} coordinates listed in '{options.coordinates}'.")
    else:
        sources_coordinates = sources_img.coordinates
        if __debug__:
            for coord in sources_coordinates:
                assert coord.pm_ra == 0
                assert coord.pm_dec == 0
        assert len(sources_coordinates) == len(sources_img)
        ipct = (sources_img.ignored / float(sources_img.total)) * 100.0 if sources_img.total else 0.0
        rpct = (len(sources_img) / float(sources_img.total)) * 100.0 if sources_img.total else 0.0
        if sources_img.ignored:
            print(f"{style.prefix}{sources_img.ignored} detections ({ipct:.2f} %) within {options.margin} pixels of the edge were removed.")
            print(f"{style.prefix}There remain {len(sources_img)} sources ({rpct:.2f} %) on which to do photometry.")
        else:
            print(f"{style.prefix}Detected {len(sources_img)} sources on which to do photometry.")

    options.coordinates = sources_coordinates

    print(style.prefix)
    print(f"{style.prefix}Need to determine the instrumental magnitude of each source.")
    print(f"{style.prefix}Doing photometry on the sources image, using the parameters:")

    if not fixed_annuli:
        sources_img_fwhm = get_fwhm(sources_img, options)
        sources_aperture = options.aperture * sources_img_fwhm
        sources_annulus = options.annulus * sources_img_fwhm
        sources_dannulus = options.dannulus * sources_img_fwhm

        print(f"{style.prefix}FWHM (sources image) = {sources_img_fwhm:.3f} pixels, therefore:")
        print(f"{style.prefix}Aperture radius = {sources_img_fwhm:.3f} x {options.aperture:.2f} = {sources_aperture:.3f} pixels")
        print(f"{style.prefix}Sky annulus, inner radius = {sources_img_fwhm:.3f} x {options.annulus:.2f} = {sources_annulus:.3f} pixels")
        print(f"{style.prefix}Sky annulus, width = {sources_img_fwhm:.3f} x {options.dannulus:.2f} = {sources_dannulus:.3f} pixels")

        if sources_dannulus < options.min:
            sources_dannulus = options.min
            warnings.warn(style.prefix + (DANNULUS_TOO_THIN_MSG % sources_dannulus))
    else:
        sources_aperture = options.aperture_pix
        sources_annulus = options.annulus_pix
        sources_dannulus = options.dannulus_pix
        print(f"{style.prefix}Aperture radius = {sources_aperture:.3f} pixels")
        print(f"{style.prefix}Sky annulus, inner radius = {sources_annulus:.3f} pixels")
        print(f"{style.prefix}Sky annulus, width = {sources_dannulus:.3f} pixels")

    print(style.prefix)
    print(f"{style.prefix}Running IRAF's qphot...", end="")
    sys.stdout.flush()

    qphot_args = [
        sources_img,
        options.coordinates,
        options.epoch,
        sources_aperture,
        sources_annulus,
        sources_dannulus,
        sys.maxsize,
        options.datek,
        options.timek,
        options.exptimek,
        None,
    ]

    with warnings.catch_warnings():
        kwargs = dict(category=qphot.MissingFITSKeyword)
        warnings.filterwarnings("ignore", **kwargs)
        sources_phot = qphot.run(*qphot_args, cbox=options.cbox)

    print("done.")

    # Remove INDEFs
    print(f"{style.prefix}Detecting INDEF objects...", end="")
    sys.stdout.flush()

    ignored_counter = 0
    non_ignored_counter = 0
    original_size = len(sources_phot)

    assert len(options.coordinates) == len(sources_phot)
    options.coordinates = []
    for coord, object_phot in zip(sources_coordinates, sources_phot):
        if object_phot.mag is not None:
            options.coordinates.append(coord)
            non_ignored_counter += 1
        else:
            ignored_counter += 1

    # delete INDEF from sources_phot (in place, reverse)
    for index in range(len(sources_phot) - 1, -1, -1):
        if sources_phot[index].mag is None:
            sources_phot.pop(index)

    assert non_ignored_counter == len(sources_phot)
    assert ignored_counter + non_ignored_counter == original_size
    print("done.")

    if ignored_counter:
        msg = f"{style.prefix}{ignored_counter} objects"
    else:
        msg = f"{style.prefix}No objects"
    print(msg + " are INDEF in the sources image.")

    if not non_ignored_counter:
        print(f"{style.prefix}Error. There are no objects left on which to do photometry.")
        print(style.error_exit_message)
        return 1
    elif ignored_counter:
        print(f"{style.prefix}There are {len(sources_phot)} objects left on which to do photometry.")

    if __debug__:
        print(f"{style.prefix}Making sure INDEF objects were removed...", end="")
        sys.stdout.flush()
        qphot_args[1] = options.coordinates
        with warnings.catch_warnings():
            kwargs = dict(category=qphot.MissingFITSKeyword)
            warnings.filterwarnings("ignore", **kwargs)
            non_INDEF_phot = qphot.run(*qphot_args, cbox=options.cbox)
        assert sources_phot == non_INDEF_phot
        print("done.")

    print(style.prefix)
    print(f"{style.prefix}Initializing output LEMONdB...", end="")
    sys.stdout.flush()

    with database.LEMONdB(output_db_path) as output_db:
        assert len(options.coordinates) == len(sources_phot)
        for id_, (object_coords, object_phot) in enumerate(zip(options.coordinates, sources_phot)):
            x, y = object_phot.x, object_phot.y
            ra_, dec_, pm_ra, pm_dec = object_coords
            imag = object_phot.mag
            args = (id_, x, y, ra_, dec_, options.epoch, pm_ra, pm_dec, imag)
            output_db.add_star(*args)

        output_db.commit()
        print("done.")

        # Store sources image info
        path = sources_img.path
        pfilter = func_catchall(sources_img.pfilter, options.filterk)
        kwargs = dict(date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek)
        unix_time = func_catchall(sources_img.date, **kwargs)

        if dates_counter[unix_time][pfilter]:
            assert len(dates_counter[unix_time][pfilter]) == 1
            img_ = fitsimage.FITSImage(dates_counter[unix_time][pfilter][0])
            if pfilter == img_.pfilter(options.filterk):
                logging.debug(
                    "%s has the same date (%.4f, %s) and filter (%s) as the sources image (%s)",
                    img_.path, unix_time, utctime(unix_time), pfilter, path
                )
                logging.debug("Avoid collision: ignore date and filter of the sources image (store None)")
                unix_time = None
                pfilter = None

        object_ = func_catchall(sources_img.read_keyword, options.objectk)
        airmass = func_catchall(sources_img.read_keyword, options.airmassk)
        gain = options.gain if options.gain else func_catchall(sources_img.read_keyword, options.gaink)
        ra_c, dec_c = sources_img_ra, sources_img_dec

        simage = database.Image(path, pfilter, unix_time, object_, airmass, gain, ra_c, dec_c)
        output_db.simage = simage
        output_db.commit()

        for pfilter, images in sorted(files.items()):
            print(style.prefix)
            print(f"{style.prefix}Let's do photometry on the {len(images)} images taken in the {pfilter} filter.")

            if json_annuli:
                assert len(json_annuli[pfilter])
                for cand in json_annuli[pfilter]:
                    output_db.add_candidate_pparams(cand, pfilter)
                filter_annuli = json_annuli[pfilter][0]
                aperture = filter_annuli.aperture
                annulus = filter_annuli.annulus
                dannulus = filter_annuli.dannulus
                print(f"{style.prefix}Using the parameters listed in the JSON file, which are:")
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")

            elif options.individual_fwhm:
                print(f"{style.prefix}Using parameters derived from the FWHM of each image:")
                print(f"{style.prefix}Aperture radius = {options.aperture:.2f} x FWHM pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {options.annulus:.2f} x FWHM pixels")
                print(f"{style.prefix}Sky annulus, width = {options.dannulus:.2f} x FWHM pixels")

            elif not fixed_annuli:
                print(f"{style.prefix}Calculating the median FWHM for this filter...", end="")
                sys.stdout.flush()
                pfilter_fwhms = []
                for path_ in images:
                    img_ = fitsimage.FITSImage(path_)
                    img_fwhm = get_fwhm(img_, options)
                    logging.debug("%s: FWHM = %.3f", img_.path, img_fwhm)
                    pfilter_fwhms.append(img_fwhm)
                fwhm = float(numpy.median(pfilter_fwhms))
                print("done.")
                aperture = fwhm * options.aperture
                annulus = fwhm * options.annulus
                dannulus = fwhm * options.dannulus
                print(f"{style.prefix}FWHM ({pfilter}) = {fwhm:.3f} pixels, therefore:")
                print(f"{style.prefix}Aperture radius = {fwhm:.3f} x {options.aperture:.2f} = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {fwhm:.3f} x {options.annulus:.2f} = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {fwhm:.3f} x {options.dannulus:.2f} = {dannulus:.3f} pixels")
                if dannulus < options.min:
                    dannulus = options.min
                    warnings.warn(style.prefix + (DANNULUS_TOO_THIN_MSG % dannulus))
            else:
                aperture = options.aperture_pix
                annulus = options.annulus_pix
                dannulus = options.dannulus_pix
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")

            # Prepare parallel pool
            pool = multiprocessing.Pool(options.ncores)

            def fwhm_derived_params(img_):
                fwhm_ = get_fwhm(img_, options)
                aperture_ = fwhm_ * options.aperture
                annulus_ = fwhm_ * options.annulus
                dannulus_ = fwhm_ * options.dannulus
                path_ = img_.path
                logging.debug("%s: FWHM = %.3f", path_, fwhm_)
                logging.debug("%s: FWHM-derived aperture: %.3f x %.2f = %.3f pixels", path_, fwhm_, options.aperture, aperture_)
                logging.debug("%s: FWHM-derived annulus: %.3f x %.2f = %.3f pixels", path_, fwhm_, options.annulus, annulus_)
                logging.debug("%s: FWHM-derived dannulus: %.3f x %.2f = %.3f pixels", path_, fwhm_, options.dannulus, dannulus_)
                return database.PhotometricParameters(aperture_, annulus_, dannulus_)

            if not options.individual_fwhm:
                pparams = database.PhotometricParameters(aperture, annulus, dannulus)
                qphot_params = lambda _x: pparams
            else:
                qphot_params = fwhm_derived_params

            def map_async_args():
                for path_ in images:
                    img_ = fitsimage.FITSImage(path_)
                    yield (img_, qphot_params(img_), options)

            result = pool.map_async(parallel_photometry, map_async_args())
            show_progress(0.0)
            while not result.ready():
                time.sleep(1)
                show_progress(queue.qsize() / float(len(images)) * 100.0)
                if logging_level < logging.WARNING:
                    print()
            result.get()
            show_progress(100.0)
            print()

            print(f"{style.prefix}Storing photometric measurements in the database...")
            sys.stdout.flush()

            show_progress(0.0)
            qphot_results = (queue.get() for _ in range(queue.qsize()))
            for index, args_ in enumerate(qphot_results):
                db_image, pparams_, img_qphot = args_
                logging.debug("Storing image %s in database", db_image.path)
                output_db.add_image(db_image)
                logging.debug("Image %s successfully stored", db_image.path)

                for object_id, object_phot in enumerate(img_qphot):
                    if object_phot.mag is None:
                        logging.debug("%s: object %d is INDEF (None)", db_image.path, object_id)
                        continue
                    elif object_phot.mag == float("infinity"):
                        logging.debug("%s: object %d is saturated (infinity)", db_image.path, object_id)
                        continue
                    else:
                        logging.debug("%s: object %d magnitude = %f", db_image.path, object_id, object_phot.mag)

                    object_snr = object_phot.snr(db_image.gain)
                    if object_snr <= 1:
                        logging.debug("%s: object %d ignored (SNR = %f <= 1)", db_image.path, object_id, object_snr)
                        continue
                    else:
                        logging.debug("%s: object %d SNR = %f", db_image.path, object_id, object_snr)
                        logging.debug("%s: storing measurement for object %d in database", db_image.path, object_id)

                        output_db.add_photometry(
                            object_id,
                            db_image.unix_time,
                            db_image.pfilter,
                            object_phot.mag,
                            object_snr,
                        )

                        logging.debug("%s: measurement for object %d successfully stored", db_image.path, object_id)

                        pm_ra, pm_dec = output_db.get_star(object_id)[5:7]
                        if not pm_ra and not pm_dec:
                            logging.debug("%s: object %d does not have proper motion", db_image.path, object_id)
                        else:
                            assert pm_ra is not None and pm_dec is not None
                            logging.debug("%s: object %d pm_ra = %f (x = %f)", db_image.path, object_id, pm_ra, object_phot.x)
                            logging.debug("%s: object %d pm_dec = %f (y = %f)", db_image.path, object_id, pm_dec, object_phot.y)
                            logging.debug("%s: storing proper-motion corrections for object %d", db_image.path, object_id)
                            output_db.add_pm_correction(
                                object_id,
                                db_image.unix_time,
                                db_image.pfilter,
                                object_phot.x,
                                object_phot.y,
                            )
                            logging.debug("%s: proper-motion correction for object %d sucessfully stored", db_image.path, object_id)

                show_progress(100.0 * (index + 1) / float(len(images)))
                if logging_level < logging.WARNING:
                    print()
            else:
                logging.info("Photometry for %s completed", pfilter)
                logging.debug("Committing database transaction")
                output_db.commit()
                logging.info("Database transaction committed")
                show_progress(100.0)
                print()

        print(f"{style.prefix}Gathering statistics about tables and indexes...", end="")
        sys.stdout.flush()
        output_db.analyze()
        print("done.")

        output_db.date = time.time()
        output_db.author = pwd.getpwuid(os.getuid())[0]
        output_db.hostname = socket.gethostname()
        output_db.commit()

        md5 = hashlib.md5()
        md5.update(str(output_db.date).encode())
        md5.update(str(output_db.author).encode())
        md5.update(str(output_db.hostname).encode())
        output_db.id = md5.hexdigest()
        output_db.commit()

    owner_writable(output_db_path, False)
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())

logger = logging.getLogger(__name__)
