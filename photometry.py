#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Aperture photometry on FITS images. Detect sources on the first image using
SExtractor, then do photometry at those coordinates on the rest. Results are
stored in a LEMON database (instrumental magnitudes with zero-point 25 and SNR).

Python 3 port of LEMON's photometry.py that keeps the same CLI and messages but
modernises internals to use the provided modules and safe, batched DB writes.
- Uses qphot.run() from qphot.py
- Uses seeing.FITSeeingImage for FWHM and SExtractor catalog handling
- Groups images by photometric filter via fitsimage.FITSImage.pfilter()
- Stores rows with add_photometry_bulk()/fast_transaction() when available
- Backend-agnostic: works with either database.LEMONSA (SQLAlchemy) or LEMONdB
"""

from __future__ import division

# stdlib
import argparse
import atexit
import collections
import contextlib
import hashlib
import itertools
import logging
import multiprocessing
import os
import os.path
import pickle
import pwd
import shutil
import socket
import sys
import tempfile
import time
import warnings
from pathlib import Path
import getpass

import numpy

# project modules (provided by user)
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

from util.coords import load_coordinates, DD_to_HMS, DD_to_DMS
from util.display import show_progress, utctime
from util.io import clean_tmp_files, owner_writable
from util.log import func_catchall

# -----------------------------------------------------------------------------
# Constants
# -----------------------------------------------------------------------------

DANNULUS_TOO_THIN_MSG = (
    "Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels"
)

# Global multiprocessing queue set in main(); workers will .put() results here
queue = None  # type: ignore


# -----------------------------------------------------------------------------
# Backend-agnostic DB helpers
# -----------------------------------------------------------------------------

def _resolve_db_class():
    """Pick SQLAlchemy LEMONSA if present; otherwise fall back to sqlite3 LEMONdB."""
    DBClass = getattr(database, "LEMONSA", None) or getattr(database, "LEMONdB", None)
    if DBClass is None:
        raise AttributeError("database module exposes neither LEMONSA nor LEMONdB")
    return DBClass


@contextlib.contextmanager
def _open_db(path):
    """Context manager that works for both LEMONSA and LEMONdB."""
    DBClass = _resolve_db_class()
    db = DBClass(path)
    try:
        yield db
    finally:
        # Prefer graceful shutdown hooks if present
        if hasattr(db, "close_all"):
            try:
                db.close_all()
            except Exception:
                pass
        elif hasattr(db, "close"):
            try:
                db.close()
            except Exception:
                pass


@contextlib.contextmanager
def _fast_txn(db):
    """Use db.fast_transaction() if available, else no-op context."""
    if hasattr(db, "fast_transaction"):
        with db.fast_transaction():
            yield
    else:
        yield


def _store_candidate_params(db, pfilter, candidates):
    """
    Accepts the JSON annuli candidates and stores them in a backend-compatible way.
    - Legacy sqlite: db.add_candidate_pparams(cand, pfilter)
    - SQLAlchemy: either db.add_candidate_parameters_by_values(...) or a
      (pparams + link) pair
    """
    if not candidates:
        return
    for cand in candidates:
        ap = float(cand.aperture)
        an = float(cand.annulus)
        dn = float(cand.dannulus)
        st = getattr(cand, "stdev", None)
        stdev = float(0.0 if st is None else st)

        if hasattr(db, "add_candidate_pparams"):
            db.add_candidate_pparams(cand, pfilter)
            continue
        if hasattr(db, "add_candidate_parameters_by_values"):
            db.add_candidate_parameters_by_values(pfilter, ap, an, dn, stdev)
            continue
        # Fallback 2-step: ensure pparams row, then link
        if hasattr(db, "get_or_create_pparams") and hasattr(db, "add_candidate_parameters"):
            pid = db.get_or_create_pparams(ap, an, dn)
            db.add_candidate_parameters(pfilter, pid, stdev)


def _add_photometry_rows(db, rows, db_image=None):
    """
    Store photometry rows with best available method.
    rows: iterable of (star_id, image_id, magnitude, snr)
    If only the legacy per-row helper exists, falls back to it using db_image.
    """
    if not rows:
        return
    # Preferred bulk/many methods
    if hasattr(db, "add_photometry_bulk"):
        db.add_photometry_bulk(rows)
        return
    if hasattr(db, "add_photometry_many"):
        db.add_photometry_many(rows)
        return
    # Legacy per-row fallback (LEMONdB-style)
    if hasattr(db, "add_photometry") and db_image is not None:
        for s_id, _img_id, mval, sval in rows:
            db.add_photometry(int(s_id), db_image.unix_time, db_image.pfilter, float(mval), float(sval))
        return
    # Last resort: try direct connection if exposed (SQLite-style)
    if hasattr(db, "connection"):
        try:
            for s_id, img_id, mval, sval in rows:
                try:
                    db.connection.execute(
                        "INSERT OR REPLACE INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                        (int(s_id), int(img_id), float(mval), float(sval)),
                    )
                except Exception:
                    db.connection.execute(
                        "INSERT INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                        (int(s_id), int(img_id), float(mval), float(sval)),
                    )
            return
        except Exception:
            pass
    raise RuntimeError("No suitable method to insert photometry rows on this DB facade")


# -----------------------------------------------------------------------------
# Misc helpers (FWHM, SExtractor param file, parallel worker)
# -----------------------------------------------------------------------------

def _write_params_file(path):
    """Write a minimal SExtractor PARAMETERS file (ASCII list)."""
    lines = [
        "NUMBER",
        "X_IMAGE",
        "Y_IMAGE",
        "ALPHA_J2000",
        "DELTA_J2000",
        "MAG_BEST",
    ]
    logging.debug("Writing SExtractor .param file: %s", path)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
        f.write("\n")
    logging.debug("SExtractor .param file written")


def get_fwhm(img, options):
    """Return the FWHM of *img*.

    Try to read it from the header keyword given by --fwhmk; if not present,
    compute it using seeing.FITSeeingImage.fwhm() with the same options as the
    'seeing' command (provided module).
    """
    try:
        logging.debug("%s: reading FWHM from keyword '%s'", img.path, options.fwhmk)
        fwhm = img.read_keyword(options.fwhmk)
        logging.debug("%s: FWHM = %.3f (keyword '%s')", img.path, fwhm, options.fwhmk)
        return float(fwhm)
    except Exception:
        logging.debug("%s: keyword '%s' not found; computing FWHM", img.path, options.fwhmk)

    if not isinstance(img, seeing.FITSeeingImage):
        img = seeing.FITSeeingImage(img.path, options.maximum, options.margin, coaddk=options.coaddk)

    mode = "mean" if getattr(options, "mean", False) else "median"
    fwhm = img.fwhm(per=options.per, mode=mode)
    logging.debug("%s: FITSeeingImage.fwhm() -> %.3f (mode=%s, per=%s)", img.path, fwhm, mode, options.per)
    return float(fwhm)


def parallel_photometry(args):
    """Worker for Pool.map_async.

    Args is (fitsimage.FITSImage, database.PhotometricParameters, options).
    Runs qphot and places (database.Image, pparams, qphot.QPhot list) on the global queue.
    """
    image, pparams, options = args

    logging.debug("Doing photometry on %s", image.path)
    logging.debug(
        "%s: qphot aperture=%.3f annulus=%.3f dannulus=%.3f",
        image.path,
        pparams.aperture,
        pparams.annulus,
        pparams.dannulus,
    )

    maximum = image.saturation(options.maximum, coaddk=options.coaddk)
    logging.debug("%s: saturation level = %d ADUs'", image.path, maximum)

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
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=qphot.MissingFITSKeyword)
        img_qphot = qphot.run(*run_args, cbox=options.cbox)
    logging.debug("%s: qphot.run() returned %d records", image.path, len(img_qphot))

    pfilter = image.pfilter(options.filterk)

    unix_time = image.date(
        date_keyword=options.datek,
        time_keyword=options.timek,
        exp_keyword=options.exptimek,
    )
    object_ = image.read_keyword(options.objectk)
    airmass = image.read_keyword(options.airmassk)
    gain = options.gain or image.read_keyword(options.gaink)

    try:
        ra, dec = image.center_wcs()
    except Exception:
        # If WCS center is not available, fall back to header RA/DEC when present
        ra = float(getattr(image, "ra", 0.0) or 0.0)
        dec = float(getattr(image, "dec", 0.0) or 0.0)

    db_image = database.Image(image.path, pfilter, unix_time, object_, airmass, gain, ra, dec)

    queue.put((db_image, pparams, img_qphot))


# -----------------------------------------------------------------------------
# Parser (keep upstream CLI; help text pulled from defaults/keywords)
# -----------------------------------------------------------------------------

description = (
    defaults.photometry_description
    if hasattr(defaults, "photometry_description")
    else (
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
)

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... SOURCES_IMG INPUT_IMGS... OUTPUT_DB"

parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite output database if it already exists",
)

# Filter images for photometry
filter_group = parser.add_argument_group(
    "Filter images for photometry",
    "Select which images to include by date, time, and/or passband.",
)
filter_group.add_argument(
    "--from",
    action="store",
    dest="from_",
    default=None,
    metavar="DATE",
    help=defaults.desc.get("from_", ""),
)
filter_group.add_argument(
    "--to",
    action="store",
    dest="to",
    default=None,
    metavar="DATE",
    help=defaults.desc.get("to", ""),
)
filter_group.add_argument(
    "--date", action="store", dest="date", default=None, help=defaults.desc.get("date", "")
)
filter_group.add_argument(
    "--time", action="store", dest="time", default=None, help=defaults.desc.get("time", "")
)
filter_group.add_argument(
    "--filter",
    action="append",
    type=str,
    dest="filters",
    default=None,
    help=defaults.desc.get(
        "pfilter", "photometric filter to consider (may be repeated)"
    ),
)
filter_group.add_argument(
    "--exclude",
    action="append",
    type=str,
    dest="excluded_filters",
    default=None,
    help=(
        "ignore those FITS files taken in this photometric filter. This option is the opposite "
        "of --filter, and may be as well used multiple times in order to specify more than one "
        "photometric filter that must be discarded."
    ),
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
    help=defaults.desc.get("maximum", ""),
)
parser.add_argument(
    "--margin",
    action="store",
    type=int,
    dest="margin",
    default=defaults.margin,
    help=defaults.desc.get("margin", ""),
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
    help=defaults.desc.get("ncores", ""),
)
parser.add_argument(
    "-v",
    "--verbose",
    action="count",
    dest="verbose",
    default=defaults.verbosity,
    help=defaults.desc.get("verbosity", ""),
)

# FITS keyword options
key_group = parser.add_argument_group(
    "FITS Keywords", getattr(keywords, "group_description", "")
)
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
    "--airmassk",
    action="store",
    type=str,
    dest="airmassk",
    default=keywords.airmassk,
    help=keywords.desc["airmassk"],
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
    "--uncimgk",
    action="store",
    type=str,
    dest="uncimgk",
    default=keywords.uncimgk,
    help=keywords.desc["uncimgk"],
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
    "--fwhmk",
    action="store",
    type=str,
    dest="fwhmk",
    default=keywords.fwhmk,
    help=keywords.desc["fwhmk"],
)

# Coordinates
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

# Photometry radii (FWHM-scaled)
qphot_group = parser.add_argument_group(
    "Aperture Photometry (FWHM)", "Use FWHM-scaled aperture/annulus sizes."
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

# Photometry radii (pixels)
qphot_fixed = parser.add_argument_group(
    "Aperture Photometry (pixels)", "Specify exact sizes in pixels (use together)."
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

# FWHM computation
fwhm_group = parser.add_argument_group(
    "FWHM", "If header does not contain FWHM, compute it like 'seeing' would."
)
fwhm_group.add_argument(
    "--snr-percentile",
    action="store",
    type=float,
    dest="per",
    default=defaults.snr_percentile,
    help=defaults.desc.get("snr_percentile", ""),
)
fwhm_group.add_argument(
    "--mean", action="store_true", dest="mean", help=defaults.desc.get("mean", "")
)

customparser.clear_metavars(parser)


def _meta_put(db, key, value):
    """Store metadata as pickled bytes (works for LEMONSA & legacy)."""
    b = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    # Try the nicest API first, fall back to attribute or _meta_set
    if hasattr(db, "set_metadata"):
        return db.set_metadata(key, b)
    try:
        setattr(db, key, b)
        return
    except Exception:
        pass
    if hasattr(db, "_meta_set"):
        return db._meta_set(key, b)
    raise RuntimeError("No way to set metadata on this DB backend")

# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def main(arguments=None):
    """LEMON-compatible main(); backend-agnostic and safe for LEMONSA (no .commit())."""
    # --------- Local helpers to avoid touching the rest of the file ----------
    from contextlib import contextmanager

    @contextmanager
    def _open_db_local(path):
        DBClass = getattr(database, "LEMONSA", None) or getattr(database, "LEMONdB", None)
        if DBClass is None:
            raise RuntimeError("No DB class found (expected LEMONSA or LEMONdB)")
        db = DBClass(path)
        # Provide no-op commit for SQLAlchemy facade
        if not hasattr(db, "commit") or not callable(getattr(db, "commit", None)):
            db.commit = lambda: None  # type: ignore[attr-defined]
        try:
            yield db
        finally:
            if hasattr(db, "close_all") and callable(getattr(db, "close_all")):
                db.close_all()
            elif hasattr(db, "close") and callable(getattr(db, "close")):
                db.close()

    def _store_candidate_params_local(db, pfilter, candidates):
        if not candidates:
            return
        for cand in candidates:
            ap = getattr(cand, "aperture", None) if hasattr(cand, "aperture") else cand.get("aperture")
            an = getattr(cand, "annulus", None) if hasattr(cand, "annulus") else cand.get("annulus")
            dn = getattr(cand, "dannulus", None) if hasattr(cand, "dannulus") else cand.get("dannulus")
            st = getattr(cand, "stdev", None) if hasattr(cand, "stdev") else cand.get("stdev") if isinstance(cand, dict) else None
            if ap is None or an is None or dn is None:
                continue
            if hasattr(db, "add_candidate_pparams"):
                db.add_candidate_pparams(cand, pfilter)
                continue
            stdev = float(0.0 if st is None else st)
            if hasattr(db, "add_candidate_parameters_by_values"):
                db.add_candidate_parameters_by_values(pfilter, float(ap), float(an), float(dn), stdev)
            else:
                pid = db.get_or_create_pparams(float(ap), float(an), float(dn))
                db.add_candidate_parameters(pfilter, pid, stdev)

    # Globals used by workers / progress
    global queue

    # --------------- Parse args & logging -----------------
    if arguments is None:
        arguments = sys.argv[1:]
    (options, args) = parser.parse_known_args(list(map(str, arguments)))

    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose and int(options.verbose) >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(level=logging_level, format=getattr(style, "logging_format", "%(message)s"))

    # --------------- Inputs -----------------
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

    # Read JSON annuli proposals if provided
    json_annuli = {}
    if getattr(options, "json_annuli", None):
        json_annuli = json_parse.parse_annuli_file(options.json_annuli)

    # Decide fixed (pixels) vs FWHM-scaled radii
    fixed_annuli = (
        options.aperture_pix is not None
        and options.annulus_pix is not None
        and options.dannulus_pix is not None
    )

    # Validate radii
    radii = (
        (options.aperture_pix, options.annulus_pix, options.dannulus_pix)
        if fixed_annuli
        else (options.aperture, options.annulus, options.dannulus)
    )
    if min(radii) <= 0:
        print(f"{style.prefix}Error. The aperture, annulus and dannulus values must be positive numbers.")
        print(style.error_exit_message)
        return 1
    if (fixed_annuli and options.aperture_pix > options.annulus_pix) or (
        (not fixed_annuli) and options.aperture > options.annulus
    ):
        ap = options.aperture_pix if fixed_annuli else options.aperture
        an = options.annulus_pix if fixed_annuli else options.annulus
        print(f"{style.prefix}Error. The aperture radius ({ap:.2f}) must be <= inner radius of the sky annulus ({an:.2f})")
        print(style.error_exit_message)
        return 1

    # --------------- Coordinates: file or detect with SExtractor -----------------
    if options.coordinates:
        sources_coordinates = []
        for cargs in load_coordinates(options.coordinates):
            sources_coordinates.append(astromatic.Coordinates(*cargs))
        if not sources_coordinates:
            print(f"{style.prefix}Error. Coordinates file '{options.coordinates}' is empty.")
            print(style.error_exit_message)
            return 1
    else:
        print(f"{style.prefix}No --coordinates provided; detecting sources on the first image with SExtractor?")
        basename = os.path.basename(sources_img_path)
        root, extension = os.path.splitext(basename)
        tmp_fd, tmp_sources_img_path = tempfile.mkstemp(prefix=f"{root}_", suffix=extension)
        os.close(tmp_fd)
        shutil.copy2(sources_img_path, tmp_sources_img_path)
        owner_writable(tmp_sources_img_path, True)
        atexit.register(clean_tmp_files, tmp_sources_img_path)

        try:
            tmp_img = fitsimage.FITSImage(tmp_sources_img_path)
            tmp_img.delete_keyword(keywords.sex_catalog)
        except Exception:
            pass

        sources_img = seeing.FITSeeingImage(tmp_sources_img_path, sys.maxsize, options.margin, coaddk=options.coaddk)
        sources_coordinates = sources_img.coordinates
        if sources_img.ignored:
            ipct = sources_img.ignored / float(sources_img.total) * 100.0
            rpct = len(sources_img) / float(sources_img.total) * 100.0
            print(f"{style.prefix}{sources_img.ignored} detections ({ipct:.2f} %) within {options.margin} pixels of the edge were removed.")
            print(f"{style.prefix}There remain {len(sources_img)} sources ({rpct:.2f} %) on which to do photometry.")
        else:
            print(f"{style.prefix}Detected {len(sources_img)} sources on which to do photometry.")

    options.coordinates = sources_coordinates  # used by qphot / workers

    # --------------- Photometry on sources image -----------------
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
            warnings.warn(style.prefix + ("Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels" % sources_dannulus))
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
        None,  # no uncertainty image for the sources image
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

    # --------------- Initialize DB, insert stars & sources image metadata -----------------
    print(style.prefix)
    print(f"{style.prefix}Initializing output LEMONdB...", end=" ")
    sys.stdout.flush()

    with _open_db_local(output_db_path) as output_db:
        # Insert stars
        assert len(options.coordinates) == len(sources_phot)
        for id_, (object_coords, object_phot) in enumerate(zip(options.coordinates, sources_phot)):
            x, y = object_phot.x, object_phot.y
            ra0, dec0, pm_ra, pm_dec = object_coords
            imag = object_phot.mag
            output_db.add_star(id_, x, y, ra0, dec0, options.epoch, pm_ra, pm_dec, imag)
        output_db.commit()
        print("done.")

        # Sources image metadata
        path = sources_img.path
        pfilter = func_catchall(sources_img.pfilter, options.filterk)
        unix_time = func_catchall(
            sources_img.date, date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek
        )
        object_ = func_catchall(sources_img.read_keyword, options.objectk)
        airmass = func_catchall(sources_img.read_keyword, options.airmassk)
        gain = options.gain if options.gain else func_catchall(sources_img.read_keyword, options.gaink)
        ra0 = getattr(sources_img, "ra", None)
        dec0 = getattr(sources_img, "dec", None)
        try:
            ra0, dec0 = sources_img.center_wcs()
        except Exception:
            ra0 = float(ra0 or 0.0)
            dec0 = float(dec0 or 0.0)
        simg = database.Image(path, pfilter, unix_time, object_, airmass, gain, ra0, dec0)
        output_db.simage = simg
        output_db.commit()

        # --------------- Examine input files & group by filter -----------------
        print(f"{style.prefix}Examining the headers of the {len(input_paths)} FITS files given as input...")

        def get_date(img):
            return img.date(date_keyword=options.datek, time_keyword=options.timek, exp_keyword=options.exptimek)

        files = fitsimage.InputFITSFiles()
        img_dates = {}
        show_progress(0.0)
        for index, img_path in enumerate(sorted(input_paths)):
            img = fitsimage.FITSImage(img_path)
            pfilter = img.pfilter(options.filterk)
            files[pfilter].append(img_path)
            img_dates[img_path] = get_date(img)
            show_progress((index + 1) / float(len(input_paths)) * 100.0)
        print()

        print(f"{style.prefix}{len(files.keys())} different photometric filters were detected:")
        for pf, pfiles in sorted(files.items()):
            pct = (len(pfiles) / float(len(input_paths)) * 100.0) if input_paths else 0.0
            print(f"{style.prefix} {pf}: {len(pfiles)} files ({pct:.2f} %)")

        # Ensure no duplicate images with same (date, filter)
        print(f"{style.prefix}Making sure there are no images with the same date and filter...", end=" ")
        sys.stdout.flush()
        def _get_dict():
            return collections.defaultdict(list)
        dates_counter = collections.defaultdict(_get_dict)
        for pf, pfiles in files.items():
            for img_path in pfiles:
                dates_counter[img_dates[img_path]][pf].append(img_path)
        for date, pf_dict in dates_counter.items():
            for pf, pfiles in pf_dict.items():
                if len(pfiles) > 1:
                    msg = f"{date:.4f} ({utctime(date)}) in {pf}: {', '.join(pfiles)}"
                    print("\n" + msg)
                    print(f"{style.prefix}Error. Two or more images taken in the same photometric filter seem to have the exact same date.")
                    print(style.error_exit_message)
                    return 1
        print("done.")

        # Discard excluded filters if any
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
                former_total = discarded + sum(len(v) for v in files.values())
                pct_disc = discarded / float(former_total) * 100.0 if former_total else 0.0
                pct_rem = (sum(len(v) for v in files.values()) / float(former_total) * 100.0) if former_total else 0.0
                print(f"{style.prefix}{discarded} images ({pct_disc:.2f} %) discarded, {sum(len(v) for v in files.values())} ({pct_rem:.2f} %) remain.")

        # --annuli must cover all detected filters
        if json_annuli:
            for pf in files.keys():
                if pf not in json_annuli.keys():
                    json_basename = os.path.basename(options.json_annuli)
                    print(f"{style.prefix}Error. Photometric parameters for the '{pf}' filter not listed in '{json_basename}'.")
                    print(style.error_exit_message)
                    return 1

        print(f"{style.prefix}Sources image: {sources_img_path}")

        # --------------- Do photometry per filter (parallel per image) -----------------
        for pf, pf_images in sorted(files.items()):
            print(style.prefix)
            print(f"{style.prefix}Let's do photometry on the {len(pf_images)} images taken in the {pf} filter.")

            if json_annuli:
                assert len(json_annuli[pf])
                _store_candidate_params_local(output_db, pf, json_annuli[pf])
                filter_annuli = json_annuli[pf][0]
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
                for path in pf_images:
                    img_ = fitsimage.FITSImage(path)
                    img_fwhm = get_fwhm(img_, options)
                    logging.debug("%s: FWHM = %.3f", img_.path, img_fwhm)
                    pfilter_fwhms.append(img_fwhm)
                fwhm = numpy.median(pfilter_fwhms) if pfilter_fwhms else float("nan")
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
                    warnings.warn(style.prefix + ("Whoops! Sky annulus too thin, setting it to the minimum of %.2f pixels" % dannulus))
            else:
                aperture = options.aperture_pix
                annulus = options.annulus_pix
                dannulus = options.dannulus_pix
                print(f"{style.prefix}Aperture radius = {aperture:.3f} pixels")
                print(f"{style.prefix}Sky annulus, inner radius = {annulus:.3f} pixels")
                print(f"{style.prefix}Sky annulus, width = {dannulus:.3f} pixels")

            # Multiprocessing setup
            manager = multiprocessing.Manager()
            queue = manager.Queue()
            globals()["queue"] = queue
            pool = multiprocessing.Pool(options.ncores)

            def fwhm_derived_params(img_):
                fwhm_ = get_fwhm(img_, options)
                ap_ = fwhm_ * options.aperture
                an_ = fwhm_ * options.annulus
                dn_ = fwhm_ * options.dannulus
                path = img_.path
                logging.debug("%s: FWHM-derived aperture: %.3f x %.2f = %.3f pixels", path, fwhm_, options.aperture, ap_)
                logging.debug("%s: FWHM-derived annulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.annulus, an_)
                logging.debug("%s: FWHM-derived dannulus: %.3f x %.2f = %.3f pixels", path, fwhm_, options.dannulus, dn_)
                return database.PhotometricParameters(ap_, an_, dn_)

            if not options.individual_fwhm:
                pparams = database.PhotometricParameters(aperture, annulus, dannulus)
                def qphot_params(_):
                    return pparams
            else:
                qphot_params = fwhm_derived_params

            # Dispatch work
            def map_args():
                for path in pf_images:
                    img_ = fitsimage.FITSImage(path)
                    yield (img_, qphot_params(img_), options)

            result = pool.map_async(parallel_photometry, map_args())
            show_progress(0.0)
            while not result.ready():
                time.sleep(1)
                try:
                    show_progress(queue.qsize() / float(len(pf_images)) * 100.0)
                except NotImplementedError:
                    pass
                if logging_level < logging.WARNING:
                    print()
            # raise any worker exception
            result.get()
            pool.close()
            pool.join()
            show_progress(100.0)
            print()

            print(f"{style.prefix}Storing photometric measurements in the database...")
            sys.stdout.flush()
            show_progress(0.0)

            # Collect exactly one item per image
            result_items = [queue.get() for _ in range(len(pf_images))]

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
                        logging.warning("Skipping malformed photometry result")
                        show_progress(100.0 * (index + 1) / float(total))
                        if logging_level < logging.WARNING:
                            print()
                        continue

                    # Ensure image row exists and get image_id
                    output_db.add_image(db_image)
                    try:
                        image_id = output_db.get_or_add_image_id(db_image)
                    except Exception as e:
                        logging.warning("Could not resolve image_id for %r: %s", getattr(db_image, "path", db_image), e)
                        show_progress(100.0 * (index + 1) / float(total))
                        if logging_level < logging.WARNING:
                            print()
                        continue

                    # Build photometry rows
                    rows = []  # (star_id, image_id, magnitude, snr)
                    for object_id, object_phot in enumerate(img_qphot):
                        mag = getattr(object_phot, "mag", None)
                        if mag is None or mag in (float("inf"), float("-inf")):
                            continue
                        snr_val = object_phot.snr(db_image.gain)
                        if snr_val is None or snr_val <= 1:
                            continue
                        rows.append((int(object_id), int(image_id), float(mag), float(snr_val)))

                    if rows:
                        if hasattr(output_db, "add_photometry_bulk"):
                            output_db.add_photometry_bulk(rows)
                        else:
                            for s_id, img_id, mval, sval in rows:
                                output_db.add_photometry(s_id, db_image.unix_time, db_image.pfilter, mval, sval)

                    show_progress(100.0 * (index + 1) / float(total))
                    if logging_level < logging.WARNING:
                        print()

            # One fast transaction for speed if available
            if hasattr(output_db, "fast_transaction"):
                with output_db.fast_transaction():
                    _store_results(result_items)
            else:
                _store_results(result_items)

        # --------------- ANALYZE & metadata -----------------
        print(f"{style.prefix}Gathering statistics about tables and indexes...", end=" ")
        sys.stdout.flush()
        output_db.analyze()
        print("done.")

        # Metadata
        ts = time.time()
        try:
            author = __import__("getpass").getuser()
        except Exception:
            author = pwd.getpwuid(os.getuid())[0]
        hostname = socket.gethostname()

        _meta_put(output_db, "date", ts)
        _meta_put(output_db, "author", author)
        _meta_put(output_db, "hostname", hostname)

        md5 = hashlib.md5()
        md5.update(str(ts).encode("utf-8"))
        md5.update(author.encode("utf-8"))
        md5.update(hostname.encode("utf-8"))
        _meta_put(output_db, "id", md5.hexdigest())

    owner_writable(output_db_path, False)
    print(f"{style.prefix}You're done ^_^")
    return 0



if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
