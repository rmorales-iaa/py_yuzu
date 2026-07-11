#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Aperture photometry on FITS images. Detect sources on the first image using
SExtractor (via seeing.FITSeeingImage), then do photometry at those coordinates
on the rest. Results are stored in a LEMON database (instrumental magnitudes
with zero-point 25 and SNR).

This is a Python 3 port of LEMON's photometry.py that preserves upstream CLI
and console messages while using the provided modules and SQLAlchemy-backed DB.

ENHANCED with real-time progress tracking and comprehensive statistics.
"""

from __future__ import annotations

# stdlib
import atexit
import collections
import hashlib
import itertools
import logging
import math
import multiprocessing
import os
import os.path
import pwd
import shutil
import socket
import sys
import tempfile
import time
import warnings
from collections import namedtuple
from pathlib import Path

# third-party
import numpy

# project modules (provided by user)
import astromatic
import customparser
import defaults
import fitsimage
import json_parse
import keywords
import qphot
import seeing
import style
import database

from util.coords import load_coordinates
from util.display import show_progress, utctime
from util.io import clean_tmp_files, owner_writable
from util.log import func_catchall

try:
    import getpass  # type: ignore
except Exception:  # pragma: no cover
    getpass = None  # type: ignore

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

PhotometricParameters = namedtuple("PhotometricParameters", "aperture annulus dannulus")

DANNULUS_TOO_THIN_MSG = (
    "Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels"
)

# Global multiprocessing queue set in main(); workers will .put() results here
MP_QUEUE = None  # type: ignore
MP_COUNTER = None  # type: ignore


def _init_pool(queue, counter):
    """Initializer for multiprocessing.Pool to set shared queue/counter in children."""
    global MP_QUEUE, MP_COUNTER
    MP_QUEUE = queue
    MP_COUNTER = counter


# -----------------------------------------------------------------------------
# Enhanced Progress Display with Statistics
# -----------------------------------------------------------------------------

class _EnhancedProgress:
    """Enhanced progress bar with ETA and real-time statistics."""

    def __init__(self, total: int, label: str = "", enabled: bool = True):
        self.total = max(1, int(total))
        self.current = 0
        self.label = label
        self.enabled = bool(enabled)
        self._interactive = bool(enabled and sys.stdout.isatty())
        self.start_time = time.time()
        self._last_update = 0
        self._last_render = ""
        self._last_logged_pct = -1
        self.stats = {
            'valid': 0,
            'saturated': 0,
            'indef': 0,
            'snr_filtered': 0,
            'failed': 0
        }

    def _term_width(self) -> int:
        try:
            return shutil.get_terminal_size(fallback=(80, 24)).columns
        except (OSError, AttributeError):
            return 80

    def update(self, count: int = None, stats: dict = None):
        """Update progress display with optional statistics."""
        if not self.enabled:
            return

        if count is not None:
            self.current = min(self.total, count)
        else:
            self.current = min(self.total, self.current + 1)

        if stats:
            for key in self.stats:
                if key in stats:
                    self.stats[key] = stats[key]

        # Throttle updates to twice per second for smoother display
        now = time.time()
        if now - self._last_update < 0.5 and self.current < self.total:
            return
        self._last_update = now

        # Calculate metrics
        elapsed = now - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0
        pct = (self.current / self.total) * 100.0

        # Calculate ETA
        if rate > 0 and self.current < self.total:
            remaining = (self.total - self.current) / rate
            eta_min = int(remaining // 60)
            eta_sec = int(remaining % 60)
            eta_str = f"{eta_min:02d}:{eta_sec:02d}"
        else:
            eta_str = "00:00"

        # Build progress bar
        width = max(10, min(30, self._term_width() - 90))
        filled = int(width * self.current / self.total)
        bar = "#" * filled + "-" * (width - filled)

        # Build line with statistics
        parts = [
            f"{self.label} [{bar}] {pct:5.1f}%",
            f"{self.current}/{self.total}",
            f"{rate:.1f} img/s",
            f"ETA {eta_str}"
        ]

        # Add statistics if any
        if self.stats['valid'] > 0:
            parts.append(f"?{self.stats['valid']}")
        if self.stats['failed'] > 0:
            parts.append(f"?{self.stats['failed']}")

        line = " | ".join(parts)

        # Truncate if necessary
        max_len = self._term_width() - 2
        if len(line) > max_len:
            line = line[:max_len - 3] + "..."

        if self._interactive:
            try:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
                self._last_render = line
            except (OSError, UnicodeEncodeError):
                pass
        else:
            pct_int = int(pct)
            should_log = (
                self.current >= self.total or
                pct_int >= self._last_logged_pct + 5 or
                now - self._last_update >= 10.0
            )
            if should_log:
                print(line, flush=True)
                self._last_logged_pct = pct_int
                self._last_render = line

    def finish(self):
        """Complete the progress bar with final statistics."""
        if not self.enabled:
            return

        self.current = self.total
        elapsed = time.time() - self.start_time
        rate = self.total / elapsed if elapsed > 0 else 0

        width = max(10, min(30, self._term_width() - 90))
        bar = "#" * width

        parts = [
            f"{self.label} [{bar}] 100.0%",
            f"{self.total}/{self.total}",
            f"{rate:.1f} img/s",
            "Done!"
        ]

        line = " | ".join(parts) + "\n"

        if self._interactive:
            try:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
            except (OSError, UnicodeEncodeError):
                pass
        else:
            print(line.rstrip(), flush=True)

    def clear_line(self):
        """Clear the current progress line."""
        if not self._interactive:
            return
        width = self._term_width()
        try:
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()
        except (OSError, UnicodeEncodeError):
            pass

    def print_above(self, text: str):
        """Print a message above the progress bar."""
        if not self.enabled:
            print(text, flush=True)
            return
        if not self._interactive:
            print(text, flush=True)
            return
        self.clear_line()
        print(text)
        # Redraw progress if not finished
        if self.current < self.total and self._last_render:
            try:
                sys.stdout.write("\r" + self._last_render)
                sys.stdout.flush()
            except (OSError, UnicodeEncodeError):
                pass


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------

def _float_or(default, val):
    try:
        return float(val)
    except Exception:
        return float(default)


def _float_or_none(val):
    try:
        return float(val)
    except Exception:
        return None


def get_fwhm(img, options):
    """Return the FWHM of *img*.

    Try to read it from the header keyword given by --fwhmk; if not present,
    compute it using seeing.FITSeeingImage.fwhm() with the same options as the
    'seeing' command (provided module).
    """
    try:
        logging.debug("%s: reading FWHM from keyword '%s'", img.path, options.fwhmk)
        fwhm = float(img.read_keyword(options.fwhmk))
        logging.debug("%s: FWHM = %.3f (keyword '%s')", img.path, fwhm, options.fwhmk)
        return fwhm
    except Exception:
        logging.debug("%s: keyword '%s' not found; computing FWHM", img.path, options.fwhmk)

    if not isinstance(img, seeing.FITSeeingImage):
        img = seeing.FITSeeingImage(
            img.path,
            options.maximum,
            options.margin,
            coaddk=options.coaddk,
            gaink=options.gaink,
            min_snr=options.detect_min_snr,
            max_elongation=options.detect_max_elongation,
            min_area=options.detect_min_area,
            keep_saturated=options.detect_keep_saturated,
        )

    mode = "mean" if getattr(options, "mean", False) else "median"
    fwhm = float(img.fwhm(per=options.per, mode=mode))
    logging.debug("%s: FITSeeingImage.fwhm() -> %.3f (mode=%s, per=%s)", img.path, fwhm, mode, options.per)
    return fwhm


def _infer_sources_image_filter(
    sources_img: fitsimage.FITSImage,
    input_paths: list[str],
    options,
) -> str:
    """Infer passband for sources image, falling back to science inputs."""
    try:
        return str(sources_img.pfilter(options.filterk))
    except Exception as e:
        logging.debug("%s: %s; trying to infer passband from science inputs", sources_img.path, e)

    inferred_filters = set()
    for img_path in input_paths:
        try:
            inferred_filters.add(str(fitsimage.FITSImage(img_path).pfilter(options.filterk)))
        except Exception as e:
            logging.debug("%s: %s; skipping for sources-image passband inference", img_path, e)

    if len(inferred_filters) == 1:
        inferred = next(iter(inferred_filters))
        logging.debug("%s: inferred passband '%s' from science inputs", sources_img.path, inferred)
        return inferred

    logging.debug(
        "%s: could not infer unique passband from science inputs (%s); defaulting to 'UNKNOWN'",
        sources_img.path,
        sorted(inferred_filters),
    )
    return "UNKNOWN"


def _build_db_image(img: fitsimage.FITSImage, options, *, forced_pfilter: str | None = None) -> database.Image:
    """Collect image metadata and return a database.Image object."""
    # Passband: be tolerant when FILTER keyword is missing on the sources image
    if forced_pfilter is not None:
        pfilter = forced_pfilter
    else:
        try:
            pfilter = str(img.pfilter(options.filterk))
        except Exception as e:
            logging.debug("%s: %s; defaulting passband to 'UNKNOWN'", img.path, e)
            pfilter = "UNKNOWN"

    # Time: if DATE/TIME keywords are missing, fall back to 0.0
    try:
        unix_time = float(img.date(date_keyword=options.datek,
                                   time_keyword=options.timek,
                                   exp_keyword=options.exptimek))
    except Exception as e:
        logging.debug("%s: failed to read DATE/TIME: %r; unix_time=0.0", img.path, e)
        unix_time = 0.0

    object_ = func_catchall(img.read_keyword, options.objectk)
    airmass = _float_or(0.0, func_catchall(img.read_keyword, options.airmassk))
    raw_gain = options.gain if options.gain is not None else func_catchall(img.read_keyword, options.gaink)
    gain = _float_or_none(raw_gain)
    if gain is not None and (not math.isfinite(gain) or gain <= 0):
        gain = None
    try:
        ra, dec = img.center_wcs()
        ra = float(ra)
        dec = float(dec)
    except Exception:
        ra = _float_or(0.0, getattr(img, "ra", 0.0))
        dec = _float_or(0.0, getattr(img, "dec", 0.0))

    return database.Image(img.path, pfilter, unix_time, object_, airmass, gain, ra, dec)


def parallel_photometry(args):
    """Worker for Pool.map_async.

    Args is (path:str, PhotometricParameters, options).
    Runs qphot and places (database.Image, qphot_records) on the global queue.
    """
    path, pparams, options = args

    try:
        img = fitsimage.FITSImage(path)
        logging.debug("Doing photometry on %s", img.path)
        logging.debug(
            "%s: qphot aperture=%.3f annulus=%.3f dannulus=%.3f",
            img.path, pparams.aperture, pparams.annulus, pparams.dannulus
        )

        maximum = int(img.saturation(options.maximum, coaddk=options.coaddk))
        logging.debug("%s: saturation level = %d ADUs", img.path, maximum)

        run_args = (
            img,
            options.coordinates,
            options.epoch,
            float(pparams.aperture),
            float(pparams.annulus),
            float(pparams.dannulus),
            maximum,
            options.datek,
            options.timek,
            options.exptimek,
            options.uncimgk,
        )
        img_qphot = qphot.run(*run_args, cbox=options.cbox)
        logging.debug("%s: qphot.run() returned %d records", img.path, len(img_qphot))

        db_image = _build_db_image(img, options)

        MP_QUEUE.put((db_image, img_qphot, None))  # None = no error
        if MP_COUNTER is not None:
            with MP_COUNTER.get_lock():
                MP_COUNTER.value += 1
    except Exception as e:
        logging.error(f"Failed to process {path}: {e}")
        MP_QUEUE.put((None, None, str(e)))  # Error result
        if MP_COUNTER is not None:
            with MP_COUNTER.get_lock():
                MP_COUNTER.value += 1


# -----------------------------------------------------------------------------
# Parser (keep upstream CLI; help text pulled from defaults/keywords)
# -----------------------------------------------------------------------------

description = defaults.photometry_description if hasattr(defaults, "photometry_description") else (
    """
Aperture photometry on FITS images. Detect sources on the first image using
SExtractor. For each source found, aperture photometry is performed on all the
images given as input. You can choose (using '--filter') the images to be
photometered.

Usage examples
--------------

- Aperture photometry on all the images that satisfy the '--filter R' condition:

  yuzu photometry mosaic.fits *.fits output.LEMONdB --filter R

- Aperture photometry on all the images that were acquired *after* a
  given date (time 00:00:00 assumed):

  yuzu photometry mosaic.fits *.fits output.LEMONdB \
    --from "2009-02-28"  # February 28th, 2009 00:00:00

- Aperture photometry on all the images that were acquired *during* a
  given time interval:

  yuzu photometry mosaic.fits *.fits output.LEMONdB \
    --from "2009-02-27 20:10:47" --to "2009-02-28 05:12:11"

- Aperture photometry on all images that pass two different filter
  conditions:

  yuzu photometry mosaic.fits *.fits output.LEMONdB \
    --filter R --from "2009-02-27" --to "2009-02-28"
"""
)

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... SOURCES_IMG INPUT_IMGS... OUTPUT_DB"

parser.add_argument("--overwrite", action="store_true", dest="overwrite",
                    help="overwrite output database if it already exists")

# Filter images for photometry
filter_group = parser.add_argument_group(
    "Filter images for photometry",
    "Select which images to include by date, time, and/or passband.",
)
filter_group.add_argument("--from", action="store", dest="from_", default=None, metavar="DATE",
                          help=defaults.desc.get("from_", ""))
filter_group.add_argument("--to", action="store", dest="to", default=None, metavar="DATE",
                          help=defaults.desc.get("to", ""))
filter_group.add_argument("--date", action="store", dest="date", default=None,
                          help=defaults.desc.get("date", ""))
filter_group.add_argument("--time", action="store", dest="time", default=None,
                          help=defaults.desc.get("time", ""))
filter_group.add_argument("--filter", action="append", type=str, dest="filters", default=None,
                          help=defaults.desc.get("pfilter", "photometric filter to consider (may be repeated)"))
filter_group.add_argument("--exclude", action="append", type=str, dest="excluded_filters", default=None, help=(
    "ignore those FITS files taken in this photometric filter. This option is the opposite "
    "of --filter, and may be as well used multiple times in order to specify more than one "
    "photometric filter that must be discarded."
))

parser.add_argument("--cbox", action="store", type=float, dest="cbox", default=5,
                    help="centering box width in pixels [default: %(default)s]")
parser.add_argument("--maximum", action="store", type=int, dest="maximum", default=defaults.maximum,
                    help=defaults.desc.get("maximum", ""))
parser.add_argument("--margin", action="store", type=int, dest="margin", default=defaults.margin,
                    help=defaults.desc.get("margin", ""))
parser.add_argument("--gain", action="store", type=float, dest="gain", default=None,
                    help="CCD gain in e-/ADU. If given, do not read from header (--gaink).")
parser.add_argument("--min-snr", action="store", type=float, dest="min_snr", default=1.0,
                    help="minimum SNR required to keep a photometric measurement [default: %(default)s]")
parser.add_argument("--annuli", action="store", type=str, dest="json_annuli", default=None,
                    help="read apertures/annuli from a JSON file produced by the 'annuli' command.")
parser.add_argument("--cores", action="store", type=int, dest="ncores",
                    default=max(1, getattr(defaults, "ncores", multiprocessing.cpu_count())),
                    help=defaults.desc.get("ncores", "parallel workers for photometry"))
parser.add_argument("-v", "--verbose", action="count", dest="verbose", default=defaults.verbosity,
                    help=defaults.desc.get("verbosity", ""))

# Source detection filters (applied to SExtractor detections on sources image)
detect_group = parser.add_argument_group(
    "Source detection filters",
    "Filters applied to SExtractor detections before building the source list.",
)
detect_group.add_argument("--detect-min-snr", action="store", type=float, dest="detect_min_snr",
                          default=None, help="minimum SNR for a detection to be kept (unset disables)")
detect_group.add_argument("--detect-max-elongation", action="store", type=float, dest="detect_max_elongation",
                          default=None, help="maximum elongation (A/B) for a detection to be kept (unset disables)")
detect_group.add_argument("--detect-min-area", action="store", type=int, dest="detect_min_area",
                          default=None, help="minimum ISOAREAF_IMAGE in pixels^2 (unset disables)")
detect_group.add_argument("--detect-keep-saturated", action="store_true", dest="detect_keep_saturated",
                          default=False, help="keep saturated detections (default: discard)")

# FITS keyword options
key_group = parser.add_argument_group("FITS Keywords", getattr(keywords, "group_description", ""))
key_group.add_argument("--objectk", action="store", type=str, dest="objectk", default=keywords.objectk,
                       help=keywords.desc["objectk"])
key_group.add_argument("--filterk", action="store", type=str, dest="filterk", default=keywords.filterk,
                       help=keywords.desc["filterk"])
key_group.add_argument("--datek", action="store", type=str, dest="datek", default=keywords.datek,
                       help=keywords.desc["datek"])
key_group.add_argument("--timek", action="store", type=str, dest="timek", default=keywords.timek,
                       help=keywords.desc["timek"])
key_group.add_argument("--expk", action="store", type=str, dest="exptimek", default=keywords.exptimek,
                       help=keywords.desc["exptimek"])
key_group.add_argument("--airmassk", action="store", type=str, dest="airmassk", default=keywords.airmassk,
                       help=keywords.desc["airmassk"])
key_group.add_argument("--gaink", action="store", type=str, dest="gaink", default=keywords.gaink,
                       help=keywords.desc["gaink"])
key_group.add_argument("--uncimgk", action="store", type=str, dest="uncimgk", default=keywords.uncimgk,
                       help=keywords.desc["uncimgk"])
key_group.add_argument("--coaddk", action="store", type=str, dest="coaddk", default=keywords.coaddk,
                       help=keywords.desc["coaddk"])
key_group.add_argument("--fwhmk", action="store", type=str, dest="fwhmk", default=keywords.fwhmk,
                       help=keywords.desc["fwhmk"])

# Coordinates
coords_group = parser.add_argument_group(
    "List of Coordinates",
    "Alternatively to SExtractor on sources image, you can provide a file with "
    "celestial coordinates (RA, DEC [deg]) and optional proper motions (as/yr).",
)
coords_group.add_argument("--coordinates", action="store", type=str, dest="coordinates", default=None,
                          help="path to file containing celestial coordinates for photometry")
coords_group.add_argument("--epoch", action="store", type=int, dest="epoch", default=2000,
                          help="epoch of the coordinates [default: %(default)s]")

# Photometry radii (FWHM-scaled)
qphot_group = parser.add_argument_group("Aperture Photometry (FWHM)", "Use FWHM-scaled aperture/annulus sizes.")
qphot_group.add_argument("--aperture", action="store", type=float, dest="aperture", default=3.0,
                         help="aperture radius, in number of times the median FWHM [default: %(default)s]")
qphot_group.add_argument("--annulus", action="store", type=float, dest="annulus", default=4.5,
                         help="inner radius of the sky annulus, in times the median FWHM [default: %(default)s]")
qphot_group.add_argument("--dannulus", action="store", type=float, dest="dannulus", default=1.0,
                         help="width of the sky annulus, in times the median FWHM [default: %(default)s]")
qphot_group.add_argument("--min-sky", action="store", type=float, dest="min", default=3.0,
                         help="minimum width of the sky annulus, in pixels [default = %(default)s]")
qphot_group.add_argument("--individual-fwhm", action="store_true", dest="individual_fwhm",
                         help="derive aperture/annuli per-image from that image FWHM instead of median")

# Photometry radii (pixels)
qphot_fixed = parser.add_argument_group("Aperture Photometry (pixels)", "Specify exact sizes in pixels (use together).")
qphot_fixed.add_argument("--aperture-pix", action="store", type=float, dest="aperture_pix", default=None,
                         help="aperture radius in pixels")
qphot_fixed.add_argument("--annulus-pix", action="store", type=float, dest="annulus_pix", default=None,
                         help="inner radius of sky annulus in pixels")
qphot_fixed.add_argument("--dannulus-pix", action="store", type=float, dest="dannulus_pix", default=None,
                         help="width of sky annulus in pixels")

# FWHM computation
fwhm_group = parser.add_argument_group("FWHM", "If header does not contain FWHM, compute it like 'seeing' would.")
fwhm_group.add_argument("--snr-percentile", action="store", type=float, dest="per", default=defaults.snr_percentile,
                        help=defaults.desc.get("snr_percentile", ""))
fwhm_group.add_argument("--mean", action="store_true", dest="mean", help=defaults.desc.get("mean", ""))

customparser.clear_metavars(parser)


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def main(arguments=None):
    global MP_QUEUE
    if arguments is None:
        arguments = sys.argv[1:]

    (options, args) = parser.parse_known_args(list(map(str, arguments)))

    # logging
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose and int(options.verbose) >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(level=logging_level, format=getattr(style, "logging_format", "%(message)s"))

    # Inputs
    try:
        sources_img_path = args[0]
        sources_img = fitsimage.FITSImage(sources_img_path)
    except Exception:
        print("The first input file must be the 'sources' image.")
        print(parser.format_usage())
        return 2

    output_db_path = args[-1]
    input_paths = args[1:-1]

    # Overwrite existing DB
    if options.overwrite and Path(output_db_path).exists():
        try:
            os.remove(output_db_path)
        except OSError:
            pass

    # JSON annuli
    json_annuli = {}
    if options.json_annuli:
        json_annuli = json_parse.parse_annuli_file(options.json_annuli)

    # Decide on fixed vs FWHM radii
    fixed_annuli = (
        options.aperture_pix is not None and
        options.annulus_pix is not None and
        options.dannulus_pix is not None
    )

    # Validate radii
    if fixed_annuli:
        radii = (options.aperture_pix, options.annulus_pix, options.dannulus_pix)
    else:
        radii = (options.aperture, options.annulus, options.dannulus)
    if min(radii) <= 0:
        print(f"{style.prefix}Error. The aperture, annulus and dannulus values must be positive numbers.")
        print(style.error_exit_message)
        return 1
    if (fixed_annuli and options.aperture_pix > options.annulus_pix) or ((not fixed_annuli) and options.aperture > options.annulus):
        ap = options.aperture_pix if fixed_annuli else options.aperture
        an = options.annulus_pix if fixed_annuli else options.annulus
        print(f"{style.prefix}Error. The aperture radius ({ap:.2f}) must be <= inner radius of the sky annulus ({an:.2f})")
        print(style.error_exit_message)
        return 1

    # Coordinates file or SExtractor on sources image
    if options.coordinates:
        sources_coordinates = []
        for cargs in load_coordinates(options.coordinates):
            sources_coordinates.append(astromatic.Coordinates(*cargs))
        if not sources_coordinates:
            print(f"{style.prefix}Error. Coordinates file '{options.coordinates}' is empty.")
            print(style.error_exit_message)
            return 1
        print(f"{style.prefix}Using {len(sources_coordinates)} coordinates from file.")
    else:
        print(f"{style.prefix}No --coordinates provided; detecting sources on the first image with SExtractor...")
        # Work on a temporary copy to let FITSeeingImage write header keywords
        basename = os.path.basename(sources_img_path)
        root, extension = os.path.splitext(basename)
        tmp_fd, tmp_sources_img_path = tempfile.mkstemp(prefix=f"{root}_", suffix=extension)
        os.close(tmp_fd)
        shutil.copy2(sources_img_path, tmp_sources_img_path)
        owner_writable(tmp_sources_img_path, True)
        atexit.register(clean_tmp_files, tmp_sources_img_path)

        img = fitsimage.FITSImage(tmp_sources_img_path)
        img.delete_keyword(keywords.sex_catalog)

        sources_img = seeing.FITSeeingImage(
            tmp_sources_img_path,
            sys.maxsize,
            options.margin,
            coaddk=options.coaddk,
            gaink=options.gaink,
            min_snr=options.detect_min_snr,
            max_elongation=options.detect_max_elongation,
            min_area=options.detect_min_area,
            keep_saturated=options.detect_keep_saturated,
        )

        # Build coordinates from detections (already trimmed by margin)
        sources_coordinates = sources_img.coordinates
        if __debug__:
            for coord in sources_coordinates:
                assert coord.pm_ra == 0
                assert coord.pm_dec == 0
        assert len(sources_coordinates) == len(sources_img)

        if sources_img.ignored:
            ipct = sources_img.ignored / float(sources_img.total) * 100.0
            print(f"{style.prefix}{sources_img.ignored} detections ({ipct:.2f} %) within {options.margin} pixels of the edge were removed.")
        if sources_img.filtered:
            fpct = sources_img.filtered / float(sources_img.total) * 100.0
            print(f"{style.prefix}{sources_img.filtered} detections ({fpct:.2f} %) were filtered by detection criteria.")
        rpct = len(sources_img) / float(sources_img.total) * 100.0
        print(f"{style.prefix}There remain {len(sources_img)} sources ({rpct:.2f} %) on which to do photometry.")

    options.coordinates = sources_coordinates

    # Photometry on sources image to get instrumental magnitudes
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

    qphot_args = [
        sources_img,
        options.coordinates,
        options.epoch,
        sources_aperture,
        sources_annulus,
        sources_dannulus,
        sys.maxsize,  # avoid saturation on sources image
        options.datek,
        options.timek,
        options.exptimek,
        None,
    ]
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=qphot.MissingFITSKeyword)
        sources_phot = qphot.run(*qphot_args, cbox=options.cbox)
    print("done.")

    # Remove INDEF objects
    print(f"{style.prefix}Detecting INDEF objects...", end=" ")
    sys.stdout.flush()
    original_size = len(sources_phot)
    options.coordinates = [coord for coord, obj in zip(options.coordinates, sources_phot) if obj.mag is not None]
    non_ignored_counter = len(options.coordinates)
    ignored_counter = original_size - non_ignored_counter
    for i in range(len(sources_phot) - 1, -1, -1):
        if sources_phot[i].mag is None:
            sources_phot.pop(i)
    assert non_ignored_counter == len(sources_phot)
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
            warnings.filterwarnings("ignore", category=qphot.MissingFITSKeyword)
            non_INDEF_phot = qphot.run(*qphot_args, cbox=options.cbox)
        assert sources_phot == non_INDEF_phot
        print("done.")

    # ------------------------------------------------------------------
    # Create DB and insert stars & sources image metadata
    # ------------------------------------------------------------------
    print(style.prefix)
    print(f"{style.prefix}Initializing output LEMONdB...", end=" ")
    sys.stdout.flush()

    with database.LEMONdB(output_db_path) as output_db:
        print("done.")

        # Insert stars (single-writer)
        for id_, (object_coords, object_phot) in enumerate(zip(options.coordinates, sources_phot)):
            x, y = float(object_phot.x), float(object_phot.y)
            ra0, dec0, pm_ra, pm_dec = object_coords
            imag = object_phot.mag
            output_db.add_star(
                int(id_), x, y,
                _float_or(0.0, ra0), _float_or(0.0, dec0),
                float(options.epoch),
                _float_or(0.0, pm_ra), _float_or(0.0, pm_dec),
                _float_or(0.0, imag),
            )

        # Sources image metadata
        simg_filter = _infer_sources_image_filter(sources_img, input_paths, options)
        simg = _build_db_image(sources_img, options, forced_pfilter=simg_filter)
        output_db.simage = simg  # stores & indexes

        # ------------------------------------------------------------------
        # Scan input images and group by filter
        # ------------------------------------------------------------------
        print(f"{style.prefix}Examining the headers of the {len(input_paths)} FITS files given as input...")

        def get_date(img):
            return img.date(date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek)

        files = fitsimage.InputFITSFiles()
        img_dates = {}
        show_progress(0.0)
        for index, img_path in enumerate(sorted(input_paths)):
            img = fitsimage.FITSImage(img_path)
            try:
                pfilter = str(img.pfilter(options.filterk))
            except Exception as e:
                logging.debug("%s: %s; using passband 'UNKNOWN' for grouping", img.path, e)
                pfilter = "UNKNOWN"
            files[pfilter].append(img_path)
            img_dates[img_path] = float(get_date(img))
            show_progress((index + 1) / float(len(input_paths)) * 100.0)
        print()

        print(f"{style.prefix}{len(files.keys())} different photometric filters were detected:")
        for pf, images in sorted(files.items()):
            pct = len(images) / float(len(input_paths)) * 100.0 if input_paths else 0.0
            print(f"{style.prefix} {pf}: {len(images)} files ({pct:.2f} %)")

        # Make sure there are no images with same (date, filter)
        print(f"{style.prefix}Making sure there are no images with the same date and filter...", end=" ")
        sys.stdout.flush()
        def get_dict():
            return collections.defaultdict(list)
        dates_counter = collections.defaultdict(get_dict)
        for pf, images in files.items():
            for img_path in images:
                dates_counter[img_dates[img_path]][pf].append(img_path)
        for date, pf_dict in dates_counter.items():
            for pf, images in pf_dict.items():
                if len(images) > 1:
                    msg = f"{date:.4f} ({utctime(date)}) in {pf}: {', '.join(images)}"
                    print("\n" + msg)
                    print(f"{style.prefix}Error. Two or more images taken in the same photometric filter seem to have the exact same date.")
                    print(style.error_exit_message)
                    return 1
        print("done.")

        # Discard excluded filters, if any
        if options.excluded_filters:
            print(f"{style.prefix}Discarding images taken in the photometric filters: {', '.join(options.excluded_filters)}")
            discarded = 0
            for pf in list(files.keys()):
                if pf in options.excluded_filters:
                    discarded += len(files[pf])
                    del files[pf]
            if not files:
                print()
                print(f"{style.prefix}Error. All images were discarded.")
                print(style.error_exit_message)
                return 1
            else:
                former_total = len(files) + discarded
                pct_disc = discarded / float(former_total) * 100.0
                pct_rem = len(files) / float(former_total) * 100.0
                print(f"{style.prefix}{discarded} images ({pct_disc:.2f} %) discarded, {len(files)} ({pct_rem:.2f} %) remain.")

        # --annuli must cover all detected filters
        if json_annuli:
            for pf in files.keys():
                if pf not in json_annuli.keys():
                    json_basename = os.path.basename(options.json_annuli)
                    print(f"{style.prefix}Error. Photometric parameters for the '{pf}' filter not listed in '{json_basename}'.")
                    print(style.error_exit_message)
                    return 1

        print(f"{style.prefix}Sources image: {sources_img_path}")

        # Global statistics across all filters
        global_stats = {
            'total_images': 0,
            'processed_images': 0,
            'failed_images': 0,
            'total_measurements': 0,
            'saturated': 0,
            'indef': 0,
            'snr_filtered': 0,
        }
        global_start_time = time.time()

        # ------------------------------------------------------------------
        # Do photometry by filter, parallelising per-image computation
        # ------------------------------------------------------------------
        for pf, images in sorted(files.items()):
            print(style.prefix)
            print(f"{style.prefix}Let's do photometry on the {len(images)} images taken in the {pf} filter.")

            if json_annuli:
                assert len(json_annuli[pf])
                for cand in json_annuli[pf]:
                    output_db.add_candidate_pparams(cand, pf)
                filter_annuli = json_annuli[pf][0]
                aperture = float(filter_annuli.aperture)
                annulus = float(filter_annuli.annulus)
                dannulus = float(filter_annuli.dannulus)
                print(f"{style.prefix}Using the parameters listed in the JSON file, which are:")
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")
            elif options.individual_fwhm and not fixed_annuli:
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
                fwhm = float(numpy.median(pfilter_fwhms))
                print("done.")
                aperture = fwhm * options.aperture
                annulus = fwhm * options.annulus
                dannulus = fwhm * options.dannulus
                print(f"{style.prefix}FWHM ({pf}) = {fwhm:.3f} pixels, therefore:")
                print(f"{style.prefix}Aperture radius = {fwhm:.3f} x {options.aperture:.2f} = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {fwhm:.3f} x {options.annulus:.2f} = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {fwhm:.3f} x {options.dannulus:.2f} = {dannulus:.3f} pixels")
                if dannulus < options.min:
                    dannulus = options.min
                    warnings.warn(style.prefix + DANNULUS_TOO_THIN_MSG % dannulus)
            else:
                aperture = float(options.aperture_pix)
                annulus = float(options.annulus_pix)
                dannulus = float(options.dannulus_pix)
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")

            # multiprocessing setup
            try:
                ctx = multiprocessing.get_context("fork")
            except ValueError:
                ctx = multiprocessing.get_context()
            manager = multiprocessing.Manager()
            MP_QUEUE = manager.Queue()
            MP_COUNTER = ctx.Value("i", 0)
            globals()["MP_QUEUE"] = MP_QUEUE
            globals()["MP_COUNTER"] = MP_COUNTER
            pool = ctx.Pool(
                int(max(1, options.ncores)),
                initializer=_init_pool,
                initargs=(MP_QUEUE, MP_COUNTER)
            )

            def fwhm_derived_params(img_):
                fwhm_ = get_fwhm(img_, options)
                ap_ = fwhm_ * options.aperture
                an_ = fwhm_ * options.annulus
                dn_ = fwhm_ * options.dannulus
                path = img_.path
                logging.debug("%s: FWHM-derived aperture: %.3f x %.2f = %.3f pixels", path, fwhm_, options.aperture, ap_)
                logging.debug("%s: FWHM-derived annulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.annulus, an_)
                logging.debug("%s: FWHM-derived dannulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.dannulus, dn_)
                return PhotometricParameters(ap_, an_, dn_)

            if not options.individual_fwhm:
                pparams = PhotometricParameters(aperture, annulus, dannulus)
                def qphot_params(_path):
                    return pparams
            else:
                def qphot_params(path_):
                    return fwhm_derived_params(fitsimage.FITSImage(path_))

            # dispatch work
            def map_args():
                for path in images:
                    yield (path, qphot_params(path), options)

            try:
                result = pool.map_async(parallel_photometry, map_args())

                # Enhanced progress display
                print(style.prefix)
                progress = _EnhancedProgress(len(images), label=f"{style.prefix}Processing", enabled=True)
                progress.update(0)

                # Track statistics in real-time
                filter_stats = {'valid': 0, 'saturated': 0, 'indef': 0, 'snr_filtered': 0, 'failed': 0}
                last_count = 0
                completed_count = 0

                while not result.ready():
                    time.sleep(0.3)
                    try:
                        current = int(MP_COUNTER.value)
                        if current != last_count:
                            completed_count = current
                            progress.update(current, stats=filter_stats)
                            last_count = current
                    except Exception:
                        pass

                    if logging_level < logging.WARNING:
                        progress.clear_line()

                # Ensure we process all remaining items
                result.get()
                pool.close()
                pool.join()

                progress.finish()
            except Exception:
                pool.terminate()
                pool.join()
                raise

            print(f"{style.prefix}Storing photometric measurements in the database...")
            sys.stdout.flush()

            # Collect all items from queue
            result_items = []
            while len(result_items) < int(MP_COUNTER.value):
                try:
                    result_items.append(MP_QUEUE.get_nowait())
                except Exception:
                    break

            # Also collect any from the Pool result
            if len(result_items) < len(images):
                result_items.extend(MP_QUEUE.get() for _ in range(len(images) - len(result_items)))

            total_items = len(result_items) if result_items else 1

            # Enhanced progress for database writes
            write_progress = _EnhancedProgress(total_items, label=f"{style.prefix}Saving", enabled=True)
            write_progress.update(0)

            # Reset statistics for this filter
            filter_stats = {'valid': 0, 'saturated': 0, 'indef': 0, 'snr_filtered': 0, 'failed': 0}
            warned_missing_gain = False

            def _store_one(index, db_image, img_qphot, error):
                nonlocal warned_missing_gain
                if error is not None:
                    filter_stats['failed'] += 1
                    write_progress.update(index + 1, stats=filter_stats)
                    return

                output_db.add_image(db_image)
                image_id = output_db.get_or_add_image_id(db_image)

                rows = []
                for object_id, object_phot in enumerate(img_qphot):
                    mag = getattr(object_phot, "mag", None)

                    if mag is None:
                        filter_stats['indef'] += 1
                        continue
                    elif mag in (float("inf"), float("-inf")):
                        filter_stats['saturated'] += 1
                        continue

                    gain_for_snr = db_image.gain if (db_image.gain is not None and db_image.gain > 0) else None
                    if gain_for_snr is None:
                        if not warned_missing_gain:
                            print(f"{style.prefix}Warning: missing/invalid GAIN; using 1.0 e-/ADU for SNR.")
                            warned_missing_gain = True
                        gain_for_snr = 1.0

                    snr_val = object_phot.snr(gain_for_snr)
                    if snr_val is None or snr_val < float(options.min_snr):
                        filter_stats['snr_filtered'] += 1
                        continue

                    filter_stats['valid'] += 1
                    rows.append((int(object_id), int(image_id), float(mag), float(snr_val)))

                if rows:
                    if hasattr(output_db, "add_photometry_bulk"):
                        output_db.add_photometry_bulk(rows)
                    else:
                        for s_id, img_id, mval, sval in rows:
                            output_db.add_photometry(s_id, db_image.unix_time, db_image.pfilter, mval, sval)

                write_progress.update(index + 1, stats=filter_stats)

                if logging_level < logging.WARNING:
                    write_progress.clear_line()

            # Store with optimized transaction handling
            if hasattr(output_db, "fast_transaction"):
                with output_db.fast_transaction():
                    for idx, (db_image, img_qphot, error) in enumerate(result_items):
                        _store_one(idx, db_image, img_qphot, error)
            else:
                for idx, (db_image, img_qphot, error) in enumerate(result_items):
                    _store_one(idx, db_image, img_qphot, error)

            write_progress.finish()
            try:
                manager.shutdown()
            except Exception:
                pass

            # Update global statistics
            global_stats['total_images'] += len(images)
            global_stats['processed_images'] += len(result_items) - filter_stats['failed']
            global_stats['failed_images'] += filter_stats['failed']
            global_stats['total_measurements'] += filter_stats['valid']
            global_stats['saturated'] += filter_stats['saturated']
            global_stats['indef'] += filter_stats['indef']
            global_stats['snr_filtered'] += filter_stats['snr_filtered']

            # Print filter summary
            print(style.prefix)
            print(f"{style.prefix}Photometry summary for {pf}:")
            print(f"{style.prefix}  Images processed: {len(result_items) - filter_stats['failed']}/{len(images)}")
            if filter_stats['failed'] > 0:
                print(f"{style.prefix}  Failed: {filter_stats['failed']}")
            print(f"{style.prefix}  Valid measurements: {filter_stats['valid']}")
            if filter_stats['saturated']:
                print(f"{style.prefix}  Saturated: {filter_stats['saturated']}")
            if filter_stats['indef']:
                print(f"{style.prefix}  INDEF: {filter_stats['indef']}")
            if filter_stats['snr_filtered']:
                print(f"{style.prefix}  Filtered (SNR?1): {filter_stats['snr_filtered']}")

        # After loop over filters
        print(f"{style.prefix}Gathering statistics about tables and indexes...", end=" ")
        sys.stdout.flush()
        output_db.analyze()
        print("done.")

        # Metadata (single-threaded, short transactions)
        output_db.date = time.time()
        output_db.author = getpass.getuser() if getpass else pwd.getpwuid(os.getuid())[0]
        output_db.hostname = socket.gethostname()

        md5 = hashlib.md5()
        md5.update(str(output_db.date).encode("utf-8"))
        md5.update(str(output_db.author).encode("utf-8"))
        md5.update(str(output_db.hostname).encode("utf-8"))
        output_db.id = md5.hexdigest()

    owner_writable(output_db_path, False)

    # Print global summary
    global_time = time.time() - global_start_time
    print(style.prefix)
    print(f"{style.prefix}{'=' * 60}")
    print(f"{style.prefix}PHOTOMETRY COMPLETE")
    print(f"{style.prefix}{'=' * 60}")

    def format_time(seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    print(f"{style.prefix}Total execution time: {format_time(global_time)}")
    print(f"{style.prefix}Images processed: {global_stats['processed_images']}/{global_stats['total_images']}")
    if global_stats['failed_images'] > 0:
        fail_pct = (global_stats['failed_images'] / global_stats['total_images']) * 100
        print(f"{style.prefix}Failed images: {global_stats['failed_images']} ({fail_pct:.1f}%)")

    success_rate = ((global_stats['processed_images'] / global_stats['total_images']) * 100
                    if global_stats['total_images'] > 0 else 0)
    print(f"{style.prefix}Success rate: {success_rate:.1f}%")

    if global_time > 0:
        rate = global_stats['processed_images'] / global_time
        print(f"{style.prefix}Processing rate: {rate:.2f} images/second")

    print(f"{style.prefix}")
    print(f"{style.prefix}Measurements:")
    print(f"{style.prefix}  Total valid: {global_stats['total_measurements']}")
    if global_stats['saturated'] > 0:
        print(f"{style.prefix}  Saturated: {global_stats['saturated']}")
    if global_stats['indef'] > 0:
        print(f"{style.prefix}  INDEF: {global_stats['indef']}")
    if global_stats['snr_filtered'] > 0:
        print(f"{style.prefix}  Filtered (SNR?1): {global_stats['snr_filtered']}")

    if len(sources_phot) > 0 and global_stats['processed_images'] > 0:
        avg_per_star = global_stats['total_measurements'] / len(sources_phot)
        avg_per_image = global_stats['total_measurements'] / global_stats['processed_images']
        print(f"{style.prefix}  Average per star: {avg_per_star:.1f}")
        print(f"{style.prefix}  Average per image: {avg_per_image:.1f}")

    print(f"{style.prefix}{'=' * 60}")
    print(f"{style.prefix}Output database: {output_db_path}")
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
