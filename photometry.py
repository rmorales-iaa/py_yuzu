#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# photometry.py ? Python 3 port with robust parallel storage & batching

from __future__ import division

from util.coords import load_coordinates, DD_to_HMS, DD_to_DMS
from util.display import show_progress, utctime
from util.io import clean_tmp_files, owner_writable
from util.log import func_catchall

description = """
Aperture photometry on FITS images. Detect sources on the first image using
SExtractor, then do photometry at those coordinates on the rest. Results are
stored in a LEMON database (instrumental magnitudes with zero-point 25 and SNR).
"""

import atexit
import collections
import hashlib
import itertools
import logging
import multiprocessing
import numpy
import argparse
import os
import os.path
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
import qphot
import seeing
import style

# Message of the warning that is issued when the width of the sky annulus is
# smaller than the number of pixels specified with the --min-sky option.
DANNULUS_TOO_THIN_MSG = (
    "Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels"
)

# Global multiprocessing queue (initialized in main()) so child processes can put results
queue = None  # will be set to a multiprocessing.Manager().Queue()


def get_fwhm(img, options):
    """Return the FWHM of the FITS image.

    Try reading from header (keyword options.fwhmk). If not present, compute with
    FITSeeingImage.fwhm(). 'img' must be a fitsimage.FITSImage or FITSeeingImage.
    """

    try:
        logging.debug("%s: reading FWHM from keyword '%s'", img.path, options.fwhmk)
        fwhm = img.read_keyword(options.fwhmk)
        logging.debug("%s: FWHM = %.3f (keyword '%s')", img.path, fwhm, options.fwhmk)
        return fwhm

    except KeyError:
        logging.debug("%s: keyword '%s' not found in header", img.path, options.fwhmk)

        if not isinstance(img, seeing.FITSeeingImage):
            logging.debug("%s: argument 'img' not FITSeeingImage (%s)", img.path, type(img))
            logging.debug("%s: wrapping into FITSeeingImage", img.path)
            args = (img.path, options.maximum, options.margin)
            kwargs = dict(coaddk=options.coaddk)
            img = seeing.FITSeeingImage(*args, **kwargs)

        logging.debug("%s: computing FWHM via FITSeeingImage.fwhm()", img.path)
        mode = "mean" if options.mean else "median"
        kwargs = dict(per=options.per, mode=mode)
        fwhm = img.fwhm(**kwargs)
        logging.debug("%s: FITSeeingImage.fwhm() returned %.3f", img.path, fwhm)
        return fwhm


def parallel_photometry(args):
    """Worker function used with multiprocessing.Pool.map_async().

    args: (fitsimage.FITSImage, database.PhotometricParameters, options)

    Does qphot.run() for the image & returns data via the global queue as:
      (database.Image, database.PhotometricParameters, qphot.QPhot)
    """

    image, pparams, options = args

    logging.debug("Doing photometry on %s", image.path)
    logging.debug("%s: qphot aperture: %.3f", image.path, pparams.aperture)
    logging.debug("%s: qphot annulus: %.3f", image.path, pparams.annulus)
    logging.debug("%s: qphot dannulus: %.3f", image.path, pparams.dannulus)

    maximum = image.saturation(options.maximum, coaddk=options.coaddk)
    logging.debug("%s: saturation level = %d ADUs'", image.path, maximum)

    logging.info("Running qphot on %s", image.path)
    run_args = (
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
    img_qphot = qphot.run(*run_args, cbox=options.cbox)
    logging.info("Finished running qphot on %s", image.path)

    logging.debug("%s: qphot.run() returned %d records", image.path, len(img_qphot))

    pfilter = image.pfilter(options.filterk)
    logging.debug("%s: filter = %s", image.path, pfilter)

    kwargs = dict(
        date_keyword=options.datek,
        time_keyword=options.timek,
        exp_keyword=options.exptimek,
    )
    unix_time = image.date(**kwargs)
    logging.debug("%s: observation date: %.2f (%s)", image.path, unix_time, utctime(unix_time))

    object_ = image.read_keyword(options.objectk)
    logging.debug("%s: object = %s", image.path, object_)

    airmass = image.read_keyword(options.airmassk)
    logging.debug("%s: airmass = %.4f", image.path, airmass)

    # If not given with --gaink, read it from the FITS header
    gain = options.gain or image.read_keyword(options.gaink)
    logging.debug("%s: gain (%s) = %.4f", image.path, "user" if options.gain else "header", gain)

    logging.debug("%s: calculating coordinates of field center", image.path)
    ra, dec = image.center_wcs()
    logging.debug("%s: RA (field center) = %.8f", image.path, ra)
    logging.debug("%s: DEC (field center) = %.8f", image.path, dec)

    args_img = (image.path, pfilter, unix_time, object_, airmass, gain, ra, dec)
    db_image = database.Image(*args_img)

    # put into process-shared queue
    queue.put((db_image, pparams, img_qphot))
    logging.debug("%s: photometry result put into global queue", image.path)


# --- argparse-based parser ---
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
    # customparser may offer a Passband converter; if not, accept str
    type=str,
    dest="filters",
    default=None,
    help="do not do photometry on all the FITS files given "
    "as input, but only on those taken in this photometric "
    "filter. This option may be used multiple times in order "
    "to specify more than one filter with which we want to "
    "work. " + defaults.desc["filter"],
)

parser.add_argument(
    "--exclude",
    action="append",
    type=str,
    dest="excluded_filters",
    default=None,
    help="ignore those FITS files taken in this photometric "
    "filter. This option is the opposite of --filter, and may "
    "be as well used multiple times in order to specify more "
    "than one photometric filter that must be discarded.",
)

parser.add_argument(
    "--cbox",
    action="store",
    type=float,
    dest="cbox",
    default=5,
    help="centering box width in pixels [default: %(default)s]",
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
    help="CCD gain in e-/ADU. If given, do not read from header (--gaink).",
)

parser.add_argument(
    "--annuli",
    action="store",
    type=str,
    dest="json_annuli",
    default=None,
    help="read apertures/annuli from a JSON file produced by the 'annuli' command.",
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

coords_group = parser.add_argument_group(
    "List of Coordinates",
    "Alternatively to SExtractor on sources image, you can provide a file with "
    "celestial coordinates (RA, DEC [deg]) and optional proper motions (as/yr).",
)

coords_group.add_argument(
    "--coordinates",
    action="store",
    type=str,
    dest="coordinates",
    default=None,
    help="path to file containing celestial coordinates for photometry",
)

coords_group.add_argument(
    "--epoch",
    action="store",
    type=int,
    dest="epoch",
    default=2000,
    help="epoch of the coordinates [default: %(default)s]",
)

qphot_group = parser.add_argument_group(
    "Aperture Photometry (FWHM)",
    "Use FWHM-scaled aperture/annulus sizes.",
)

qphot_group.add_argument(
    "--aperture",
    action="store",
    type=float,
    dest="aperture",
    default=3.0,
    help="aperture radius, in number of times the median FWHM [default: %(default)s]",
)

qphot_group.add_argument(
    "--annulus",
    action="store",
    type=float,
    dest="annulus",
    default=4.5,
    help="inner radius of the sky annulus, in times the median FWHM [default: %(default)s]",
)

qphot_group.add_argument(
    "--dannulus",
    action="store",
    type=float,
    dest="dannulus",
    default=1.0,
    help="width of the sky annulus, in times the median FWHM [default: %(default)s]",
)

qphot_group.add_argument(
    "--min-sky",
    action="store",
    type=float,
    dest="min",
    default=3.0,
    help="minimum width of the sky annulus, in pixels [default = %(default)s]",
)

qphot_group.add_argument(
    "--individual-fwhm",
    action="store_true",
    dest="individual_fwhm",
    help="derive aperture/annuli per-image from that image FWHM instead of median",
)

qphot_fixed = parser.add_argument_group(
    "Aperture Photometry (pixels)",
    "Specify exact sizes in pixels (use together).",
)

qphot_fixed.add_argument(
    "--aperture-pix",
    action="store",
    type=float,
    dest="aperture_pix",
    default=None,
    help="aperture radius in pixels",
)

qphot_fixed.add_argument(
    "--annulus-pix",
    action="store",
    type=float,
    dest="annulus_pix",
    default=None,
    help="inner radius of sky annulus in pixels",
)

qphot_fixed.add_argument(
    "--dannulus-pix",
    action="store",
    type=float,
    dest="dannulus_pix",
    default=None,
    help="width of sky annulus in pixels",
)

fwhm_group = parser.add_argument_group(
    "FWHM",
    "If header does not contain FWHM, compute it like 'seeing' command would.",
)

fwhm_group.add_argument(
    "--snr-percentile",
    action="store",
    type=float,
    dest="per",
    default=defaults.snr_percentile,
    help=defaults.desc["snr_percentile"],
)

fwhm_group.add_argument(
    "--mean", action="store_true", dest="mean", help=defaults.desc["mean"]
)

key_group = parser.add_argument_group("FITS Keywords", keywords.group_description)

key_group.add_argument(
    "--objectk",
    action="store",
    type=str,
    dest="objectk",
    default=keywords.objectk,
    help=keywords.desc["objectk"],
)

key_group.add_argument(
    "--filterk",
    action="store",
    type=str,
    dest="filterk",
    default=keywords.filterk,
    help=keywords.desc["filterk"],
)

key_group.add_argument(
    "--datek",
    action="store",
    type=str,
    dest="datek",
    default=keywords.datek,
    help=keywords.desc["datek"],
)

key_group.add_argument(
    "--timek",
    action="store",
    type=str,
    dest="timek",
    default=keywords.timek,
    help=keywords.desc["timek"],
)

key_group.add_argument(
    "--expk",
    action="store",
    type=str,
    dest="exptimek",
    default=keywords.exptimek,
    help=keywords.desc["exptimek"],
)

key_group.add_argument(
    "--coaddk",
    action="store",
    type=str,
    dest="coaddk",
    default=keywords.coaddk,
    help=keywords.desc["coaddk"],
)

key_group.add_argument(
    "--gaink",
    action="store",
    type=str,
    dest="gaink",
    default=keywords.gaink,
    help=keywords.desc["gaink"],
)

key_group.add_argument(
    "--fwhmk",
    action="store",
    type=str,
    dest="fwhmk",
    default=keywords.fwhmk,
    help=keywords.desc["fwhmk"],
)

key_group.add_argument(
    "--airmk",
    action="store",
    type=str,
    dest="airmassk",
    default=keywords.airmassk,
    help=keywords.desc["airmassk"],
)

key_group.add_argument(
    "--uik",
    action="store",
    type=str,
    dest="uncimgk",
    default=keywords.uncimgk,
    help=keywords.desc["uncimgk"],
)

# Let customparser tweak metavar/formatting if needed
customparser.clear_metavars(parser)


def main(arguments=None):
    """main() function. Returns exit code (0 OK)."""

    global queue  # initialize the multiprocessing queue here, before spawning
    if arguments is None:
        arguments = sys.argv[1:]  # ignore argv[0], the script name

    # argparse already handles typing; still cast defensively to str
    arguments = [str(param) for param in arguments]
    (options, args) = parser.parse_known_args(arguments)

    # Adjust logger level to WARNING/INFO/DEBUG based on -v count
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    # Require at least: sources image, one or more input images, output db
    if len(args) < 3:
        parser.print_help()
        return 2
    else:
        sources_img_path = args[0]
        input_paths = set(args[1:-1])
        output_db_path = args[-1]

    assert input_paths

    # Normalize options.uncimgk: empty string => None
    if not options.uncimgk:
        options.uncimgk = None

    # filter/exclude cannot be used together
    if options.filters and options.excluded_filters:
        print(f"{style.prefix}Error. The --filter and --exclude options are incompatible.")
        print(style.error_exit_message)
        return 1

    # --annuli JSON
    json_annuli = None
    fixed_annuli = False
    if options.json_annuli:
        if not os.path.exists(options.json_annuli):
            print(f"{style.prefix}Error. The file '{options.json_annuli}' does not exist.")
            print(style.error_exit_message)
            return 1
        else:
            json_annuli = json_parse.CandidateAnnuli.load(options.json_annuli)
            print(f"{style.prefix}Photometric parameters read from the '{os.path.basename(options.json_annuli)}' file.")

    # Fixed pixel sizes?
    fixed_pix_count = (
        bool(options.aperture_pix)
        + bool(options.annulus_pix)
        + bool(options.dannulus_pix)
    )
    if fixed_pix_count:
        if fixed_pix_count < 3:
            print(f"{style.prefix}Error. The --aperture-pix, --annulus-pix and --dannulus-pix options must be used together.")
            print(style.error_exit_message)
            return 1
        fixed_annuli = True

    if fixed_annuli and json_annuli:
        print(f"{style.prefix}Error. --aperture-pix/--annulus-pix/--dannulus-pix are incompatible with --annuli.")
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

    # Validate positive sizes and aperture <= annulus
    fwhm_options = (options.aperture, options.annulus, options.dannulus)
    pixel_options = (options.aperture_pix, options.annulus_pix, options.dannulus_pix)

    if (not fixed_annuli and min(fwhm_options) <= 0) or (
        fixed_annuli and min(pixel_options) <= 0
    ):
        print(f"{style.prefix}Error. The aperture, annulus and dannulus values must be positive numbers.")
        print(style.error_exit_message)
        return 1

    if (not fixed_annuli and options.aperture > options.annulus) or (
        fixed_annuli and options.aperture_pix > options.annulus_pix
    ):
        ap = options.aperture_pix if fixed_annuli else options.aperture
        an = options.annulus_pix if fixed_annuli else options.annulus
        print(f"{style.prefix}Error. The aperture radius ({ap:.2f}) must be <= inner radius of the sky annulus ({an:.2f})")
        print(style.error_exit_message)
        return 1

    # If --coordinates given, load
    if options.coordinates:
        sources_coordinates = []
        for cargs in load_coordinates(options.coordinates):
            coords = astromatic.Coordinates(*cargs)
            sources_coordinates.append(coords)
        if not sources_coordinates:
            print(f"{style.prefix}Error. Coordinates file '{options.coordinates}' is empty.")
            print(style.error_exit_message)
            return 1

    # Create / overwrite database file
    if os.path.exists(output_db_path):
        if not options.overwrite:
            print(f"{style.prefix}Error. The output database '{output_db_path}' already exists.")
            print(style.error_exit_message)
            return 1
        else:
            os.unlink(output_db_path)

    # Examine input FITS
    print(f"{style.prefix}Examining the headers of the {len(input_paths)} FITS files given as input...")

    def get_date(img):
        return img.date(
            date_keyword=options.datek,
            time_keyword=options.timek,
            exp_keyword=options.exptimek,
        )

    files = fitsimage.InputFITSFiles()
    img_dates = {}
    show_progress(0.0)
    for index, img_path in enumerate(sorted(input_paths)):
        img = fitsimage.FITSImage(img_path)
        pfilter = img.pfilter(options.filterk)
        files[pfilter].append(img_path)
        date = get_date(img)
        img_dates[img_path] = date
        percentage = (index + 1) / float(len(input_paths)) * 100.0
        show_progress(percentage)
    print()

    print(f"{style.prefix}{len(files.keys())} different photometric filters were detected:")
    for pfilter, images in sorted(files.items()):
        percentage = len(images) / float(len(files)) * 100.0
        print(f"{style.prefix} {pfilter}: {len(images)} files ({percentage:.2f} %)")

    # Ensure no duplicate (date, filter)
    print(f"{style.prefix}Making sure there are no images with the same date and filter...", end=" ")
    sys.stdout.flush()

    def get_dict():
        return collections.defaultdict(list)

    dates_counter = collections.defaultdict(get_dict)
    for pfilter, images in files.items():
        for img_path in images:
            date = img_dates[img_path]
            dates_counter[date][pfilter].append(img_path)

    discarded = 0
    for date, date_images in list(dates_counter.items()):
        for pfilter, images in list(date_images.items()):
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
            dates = []
            for image in files:
                dates.append(img_dates[image])
            assert len(set(dates)) == len(dates)
            assert discarded >= 2
        print(f"{style.prefix}{discarded} images had duplicate dates and were discarded, {len(files)} remain.")

    # --filter include
    if options.filters:
        print(f"{style.prefix}Ignoring images not taken in any of the following filters:")
        for index, pfilter in enumerate(sorted(options.filters)):
            print(f"{style.prefix} ({index + 1}) {pfilter}")
        sys.stdout.flush()

        discarded = 0
        for pfilter in list(files.keys()):
            if pfilter not in options.filters:
                discarded += len(files[pfilter])
                del files[pfilter]

        if not files:
            print()
            print(f"{style.prefix}Error. No image was taken in any of the above filters.")
            print(style.error_exit_message)
            return 1
        else:
            former_total = len(files) + discarded
            percentage_kept = len(files) / float(former_total) * 100.0
            percentage_disc = discarded / float(former_total) * 100.0
            print(
                f"{style.prefix}{len(files)} images ({percentage_kept:.2f} %) taken in the above filters, "
                f"{discarded} ({percentage_disc:.2f} %) were discarded."
            )

    # --exclude
    if options.excluded_filters:
        print(f"{style.prefix}Discarding images taken in any of the following filters:")
        for index, pfilter in enumerate(sorted(options.excluded_filters)):
            if pfilter in files:
                percentage = len(files[pfilter]) / float(len(files)) * 100.0 if len(files) else 0.0
                msg = f"{style.prefix} ({index + 1}) {pfilter}: {len(files[pfilter])} files to discard ({percentage:.2f} %)"
            else:
                msg = f"{style.prefix} ({index + 1}) {pfilter}: (no images)"
            print(msg)
        sys.stdout.flush()

        discarded = 0
        for pfilter in list(files.keys()):
            if pfilter in options.excluded_filters:
                discarded += len(files[pfilter])
                del files[pfilter]

        if not files:
            print()
            print(f"{style.prefix}Error. All images were discarded.")
            print(style.error_exit_message)
            return 1
        else:
            former_total = len(files) + discarded
            percentage_disc = discarded / float(former_total) * 100.0
            percentage_rem = len(files) / float(former_total) * 100.0
            print(
                f"{style.prefix}{discarded} images ({percentage_disc:.2f} %) discarded, "
                f"{len(files)} ({percentage_rem:.2f} %) remain."
            )

    # --annuli must cover all filters
    if json_annuli:
        for pfilter in files.keys():
            if pfilter not in json_annuli.keys():
                json_basename = os.path.basename(options.json_annuli)
                print(
                    f"{style.prefix}Error. Photometric parameters for the '{pfilter}' filter not listed in '{json_basename}'."
                )
                print(style.error_exit_message)
                return 1

    print(f"{style.prefix}Sources image: {sources_img_path}")
    print(f"{style.prefix}Running SExtractor on the sources image...", end=" ")
    sys.stdout.flush()

    # Temporary copy of sources image
    basename = os.path.basename(sources_img_path)
    root, extension = os.path.splitext(basename)
    kwargs = dict(prefix=f"{root}_", suffix=extension)
    tmp_fd, tmp_sources_img_path = tempfile.mkstemp(**kwargs)
    os.close(tmp_fd)
    shutil.copy2(sources_img_path, tmp_sources_img_path)
    owner_writable(tmp_sources_img_path, True)
    atexit.register(clean_tmp_files, tmp_sources_img_path)

    img = fitsimage.FITSImage(tmp_sources_img_path)
    img.delete_keyword(keywords.sex_catalog)

    # Avoid using --maximum for sources image saturation; use huge integer
    args = (tmp_sources_img_path, sys.maxsize, options.margin)
    kwargs = dict(coaddk=options.coaddk)
    sources_img = seeing.FITSeeingImage(*args, **kwargs)
    print("done.")

    print(f"{style.prefix}Calculating coordinates of field center...", end=" ")
    sys.stdout.flush()
    ra, dec = sources_img.center_wcs()
    sources_img_ra = ra
    sources_img_dec = dec
    print("done.")

    print(f"{style.prefix}? = {sources_img_ra:11.7f}", end=" ")
    h, m, s = DD_to_HMS(sources_img_ra)
    print(f"({h:02d} {m:02d} {s:05.2f})")
    print(f"{style.prefix}? = {sources_img_dec:11.7f}", end=" ")
    d, m, s = DD_to_DMS(sources_img_dec)
    sign = "+" if d >= 0 else "-"
    print(f"({sign}{abs(d):02d} {m:02d} {s:05.2f})")

    # Coordinates list
    if options.coordinates:
        print(
            f"{style.prefix}Photometry will be done on the {len(sources_coordinates)} "
            f"coordinates listed in '{options.coordinates}'."
        )
    else:
        sources_coordinates = sources_img.coordinates
        if __debug__:
            for coord in sources_coordinates:
                assert coord.pm_ra == 0
                assert coord.pm_dec == 0
        assert len(sources_coordinates) == len(sources_img)
        ipercentage = sources_img.ignored / float(sources_img.total) * 100.0
        rpercentage = len(sources_img) / float(sources_img.total) * 100.0
        if sources_img.ignored:
            print(
                f"{style.prefix}{sources_img.ignored} detections ({ipercentage:.2f} %) within "
                f"{options.margin} pixels of the edge were removed."
            )
            print(
                f"{style.prefix}There remain {len(sources_img)} sources ({rpercentage:.2f} %) on which to do photometry."
            )
        else:
            print(f"{style.prefix}Detected {len(sources_img)} sources on which to do photometry.")

    options.coordinates = sources_coordinates

    print(style.prefix)
    print(f"{style.prefix}Need to determine the instrumental magnitude of each source.")
    print(f"{style.prefix}Doing photometry on the sources image, using the parameters:")

    # Aperture/annuli for sources image
    if not fixed_annuli:
        sources_img_fwhm = get_fwhm(sources_img, options)
        sources_aperture = options.aperture * sources_img_fwhm
        sources_annulus = options.annulus * sources_img_fwhm
        sources_dannulus = options.dannulus * sources_img_fwhm

        print(f"{style.prefix}FWHM (sources image) = {sources_img_fwhm:.3f} pixels, therefore:")
        print(
            f"{style.prefix}Aperture radius = {sources_img_fwhm:.3f} x {options.aperture:.2f} = {sources_aperture:.3f} pixels"
        )
        print(
            f"{style.prefix}Sky annulus, inner radius = {sources_img_fwhm:.3f} x {options.annulus:.2f} = {sources_annulus:.3f} pixels"
        )
        print(
            f"{style.prefix}Sky annulus, width = {sources_img_fwhm:.3f} x {options.dannulus:.2f} = {sources_dannulus:.3f} pixels"
        )

        if sources_dannulus < options.min:
            sources_dannulus = options.min
            warnings.warn(style.prefix + DANNULUS_TOO_THIN_MSG % sources_dannulus)

    else:
        sources_aperture = options.aperture_pix
        sources_annulus = options.annulus_pix
        sources_dannulus = options.dannulus_pix
        print(f"{style.prefix}Aperture radius = {sources_aperture:.3f} pixels")
        print(f"{style.prefix}Sky annulus, inner radius = {sources_annulus:.3f} pixels")
        print(f"{style.prefix}Sky annulus, width = {sources_dannulus:.3f} pixels")

    print(style.prefix)
    print(f"{style.prefix}Running IRAF's qphot...", end=" ")
    sys.stdout.flush()

    # Use huge saturation to avoid any object being flagged saturated in sources image
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

    # Remove INDEF objects from sources
    print(f"{style.prefix}Detecting INDEF objects...", end=" ")
    sys.stdout.flush()

    original_size = len(sources_phot)
    assert len(options.coordinates) == len(sources_phot)
    options.coordinates = [coord for coord, obj in zip(options.coordinates, sources_phot) if obj.mag is not None]
    non_ignored_counter = len(options.coordinates)
    ignored_counter = original_size - non_ignored_counter

    # Delete INDEF photometric measurements, in-place
    for index in range(len(sources_phot) - 1, -1, -1):
        if sources_phot[index].mag is None:
            sources_phot.pop(index)

    assert non_ignored_counter == len(sources_phot)
    assert ignored_counter + non_ignored_counter == original_size
    print("done.")

    if ignored_counter:
        print(f"{style.prefix}{ignored_counter} objects are INDEF in the sources image.")
    else:
        print(f"{style.prefix}No objects are INDEF in the sources image.")

    if not non_ignored_counter:
        print(f"{style.prefix}Error. There are no objects left on which to do photometry.")
        print(style.error_exit_message)
        return 1
    elif ignored_counter:
        print(f"{style.prefix}There are {len(sources_phot)} objects left on which to do photometry.")

    if __debug__:
        print(f"{style.prefix}Making sure INDEF objects were removed...", end=" ")
        sys.stdout.flush()
        qphot_args[1] = options.coordinates
        with warnings.catch_warnings():
            kwargs = dict(category=qphot.MissingFITSKeyword)
            warnings.filterwarnings("ignore", **kwargs)
            non_INDEF_phot = qphot.run(*qphot_args, cbox=options.cbox)
        assert sources_phot == non_INDEF_phot
        print("done.")

    print(style.prefix)
    print(f"{style.prefix}Initializing output LEMONdB...", end=" ")
    sys.stdout.flush()

    # Create DB, schema is ensured by database.LEMONdB
    with database.LEMONdB(output_db_path) as output_db:
        # Insert stars
        assert len(options.coordinates) == len(sources_phot)
        for id_, (object_coords, object_phot) in enumerate(zip(options.coordinates, sources_phot)):
            x, y = object_phot.x, object_phot.y
            ra0, dec0, pm_ra, pm_dec = object_coords
            imag = object_phot.mag
            output_db.add_star(id_, x, y, ra0, dec0, options.epoch, pm_ra, pm_dec, imag)
        output_db.commit()
        print("done.")

        # Store sources image metadata
        path = sources_img.path
        pfilter = func_catchall(sources_img.pfilter, options.filterk)
        kwargs = dict(
            date_keyword=options.datek,
            time_keyword=options.timek,
            exp_keyword=options.exptimek,
        )
        unix_time = func_catchall(sources_img.date, **kwargs)

        # Avoid collision if same (filter, time) as any photometry image
        if unix_time in dates_counter and pfilter in dates_counter[unix_time]:
            assert len(dates_counter[unix_time][pfilter]) == 1
            img_same = fitsimage.FITSImage(dates_counter[unix_time][pfilter][0])
            if pfilter == img_same.pfilter(options.filterk):
                logging.debug(
                    "%s has same date (%.4f, %s) & filter (%s) as sources image (%s)",
                    img_same.path,
                    unix_time,
                    utctime(unix_time),
                    pfilter,
                    path,
                )
                unix_time = None
                pfilter = None

        object_ = func_catchall(sources_img.read_keyword, options.objectk)
        airmass = func_catchall(sources_img.read_keyword, options.airmassk)
        gain = options.gain if options.gain else func_catchall(sources_img.read_keyword, options.gaink)
        ra0, dec0 = sources_img_ra, sources_img_dec

        simg = database.Image(path, pfilter, unix_time, object_, airmass, gain, ra0, dec0)
        output_db.simage = simg
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
                print(f"{style.prefix}Calculating the median FWHM for this filter...", end=" ")
                sys.stdout.flush()

                pfilter_fwhms = []
                for path in images:
                    img_ = fitsimage.FITSImage(path)
                    img_fwhm = get_fwhm(img_, options)
                    logging.debug("%s: FWHM = %.3f", img_.path, img_fwhm)
                    pfilter_fwhms.append(img_fwhm)

                fwhm = numpy.median(pfilter_fwhms)
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
                    warnings.warn(style.prefix + DANNULUS_TOO_THIN_MSG % dannulus)

            else:
                aperture = options.aperture_pix
                annulus = options.annulus_pix
                dannulus = options.dannulus_pix
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")

            # Create the multiprocessing queue & pool
            manager = multiprocessing.Manager()
            queue = manager.Queue()  # set global for workers
            globals()['queue'] = queue

            pool = multiprocessing.Pool(options.ncores)

            def fwhm_derived_params(img_):
                fwhm_ = get_fwhm(img_, options)
                aperture_ = fwhm_ * options.aperture
                annulus_ = fwhm_ * options.annulus
                dannulus_ = fwhm_ * options.dannulus

                path = img_.path
                logging.debug("%s: FWHM = %.3f", path, fwhm_)
                logging.debug("%s: FWHM-derived aperture: %.3f x %.2f = %.3f pixels", path, fwhm_, options.aperture, aperture_)
                logging.debug("%s: FWHM-derived annulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.annulus, annulus_)
                logging.debug("%s: FWHM-derived dannulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.dannulus, dannulus_)

                return database.PhotometricParameters(aperture_, annulus_, dannulus_)

            if not options.individual_fwhm:
                pparams = database.PhotometricParameters(aperture, annulus, dannulus)

                def qphot_params(x):  # noqa
                    return pparams
            else:
                qphot_params = fwhm_derived_params

            def map_async_args():
                for path in images:
                    img_ = fitsimage.FITSImage(path)
                    yield (img_, qphot_params(img_), options)

            result = pool.map_async(parallel_photometry, map_async_args())
            show_progress(0.0)
            while not result.ready():
                time.sleep(1)
                try:
                    show_progress(queue.qsize() / float(len(images)) * 100.0)
                except NotImplementedError:
                    # qsize not supported on some platforms; skip progress update
                    pass
                if logging_level < logging.WARNING:
                    print()

            # Raise any remote exceptions
            result.get()

            # Ensure 100% in case queue was ready too soon
            show_progress(100.0)
            print()

            print(f"{style.prefix}Storing photometric measurements in the database...")
            sys.stdout.flush()

            show_progress(0.0)

            # Drain exactly len(images) items (one per worker task)
            result_items = [queue.get() for _ in range(len(images))]

            def _identify_components(item):
                db_image = pparams_ = img_qphot = None
                if isinstance(item, tuple) and len(item) == 3:
                    for comp in item:
                        if hasattr(comp, "path") and hasattr(comp, "ra") and hasattr(comp, "dec"):
                            db_image = comp
                        elif hasattr(comp, "aperture") and hasattr(comp, "annulus") and hasattr(comp, "dannulus"):
                            pparams_ = comp
                        else:
                            img_qphot = comp
                return db_image, pparams_, img_qphot

            def _store_results(items):
                total = len(items) if items else 1
                for index, item in enumerate(items):
                    db_image, pparams_used, img_qphot = _identify_components(item)
                    if db_image is None or img_qphot is None:
                        logging.warning(
                            "Skipping malformed photometry result (types: %s)",
                            [type(x).__name__ for x in (item if isinstance(item, tuple) else (item,))],
                        )
                        show_progress(100.0 * (index + 1) / float(total))
                        if logging_level < logging.WARNING:
                            print()
                        continue

                    logging.debug("Storing image %s in database", getattr(db_image, "path", "<unknown>"))
                    output_db.add_image(db_image)
                    try:
                        image_id = output_db.get_or_add_image_id(db_image)
                    except Exception as e:
                        logging.warning(
                            "Could not resolve image_id for %r: %s",
                            getattr(db_image, "path", db_image),
                            e,
                        )
                        show_progress(100.0 * (index + 1) / float(total))
                        if logging_level < logging.WARNING:
                            print()
                        continue

                    # Collect rows (star_id, image_id, magnitude, snr)
                    bulk_rows = []
                    for object_id, object_phot in enumerate(img_qphot):
                        mag = getattr(object_phot, "mag", None)
                        if mag is None or mag == float("inf") or mag == float("-inf"):
                            logging.debug(
                                "%s: object %d ignored (mag=%r)",
                                getattr(db_image, "path", "<unknown>"),
                                object_id,
                                mag,
                            )
                            continue
                        obj_snr = object_phot.snr(db_image.gain)
                        if obj_snr is None or obj_snr <= 1:
                            logging.debug(
                                "%s: object %d ignored (SNR=%r)",
                                getattr(db_image, "path", "<unknown>"),
                                object_id,
                                obj_snr,
                            )
                            continue
                        bulk_rows.append((object_id, image_id, float(mag), float(obj_snr)))

                    if bulk_rows:
                        if hasattr(output_db, "add_photometry_bulk"):
                            output_db.add_photometry_bulk(bulk_rows)
                        else:
                            # fallback per-row
                            for s, imgid, mval, sval in bulk_rows:
                                output_db.add_photometry(s, db_image.unix_time, db_image.pfilter, mval, sval)

                    show_progress(100.0 * (index + 1) / float(total))
                    if logging_level < logging.WARNING:
                        print()

            # One tuned transaction for speed if available
            if hasattr(output_db, "fast_transaction"):
                with output_db.fast_transaction():
                    _store_results(result_items)
            else:
                _store_results(result_items)
                output_db.commit()

            logging.info("Photometry for %s completed", pfilter)
            logging.debug("Committing database transaction")
            output_db.commit()
            logging.info("Database transaction committed")

            show_progress(100.0)
            print()

        # ANALYZE to gather stats
        print(f"{style.prefix}Gathering statistics about tables and indexes...", end=" ")
        sys.stdout.flush()
        output_db.analyze()
        print("done.")

        # Metadata
        output_db.date = time.time()
        output_db.author = pwd.getpwuid(os.getuid())[0]
        output_db.hostname = socket.gethostname()
        output_db.commit()

        md5 = hashlib.md5()
        md5.update(str(output_db.date).encode("utf-8"))
        md5.update(str(output_db.author).encode("utf-8"))
        md5.update(str(output_db.hostname).encode("utf-8"))
        output_db.id = md5.hexdigest()
        output_db.commit()

    owner_writable(output_db_path, False)  # chmod u-w
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
