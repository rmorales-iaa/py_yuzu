#!/usr/bin/env python3
from __future__ import annotations

import atexit
import collections
import logging
import numpy
import operator
import os
import style
import sys
import tempfile
import subprocess
from pathlib import Path

# --- Lightweight optparse→argparse group shim --------------------------------
class OptionGroup:
    """
    Drop-in shim so old code that used optparse.OptionGroup keeps working
    with argparse. Usage is identical:

        grp = OptionGroup(parser, "Title", "Description")
        grp.add_option("--foo", type=int, default=3)

    Under the hood we just forward to parser.add_argument_group(...).
    """

    def __init__(self, parser, title: str, description: str | None = None):
        self._group = parser.add_argument_group(title=title, description=description)

    def add_option(self, *opts, **kwargs):
        # Accept both add_option("--flag", ...) and add_option(action/tuple)
        # but the common/expected use is a normal add_argument signature.
        return self._group.add_argument(*opts, **kwargs)


# ------------------------------------------------------------------------------
# Copyright (c) 2012 Victor Terron. All rights ...
# (license header unchanged)
# ------------------------------------------------------------------------------

description = """
Attempt to automatically find the optimal aperture radius for aperture
photometry in the context of time-series analysis. This is possible because of
the premise that, the better an aperture radius, the most stable the resulting
light curve (that is, the lowest its standard deviation) of the most constant
astronomical objects in the field will be. In this manner, as we approach the
optimal aperture radius the aforementioned standard deviation will get lower.

Ideally, we would compare two aperture radii by means of evaluating the light
curves of all the astronomical objects in the field, but this would be rather
impractical for large data sets (say a few hundreds of images with thousands of
objects). Therefore, what we do is to initially identify which are the most
constant objects so that only they have photometry done on and their light
curves generated, thus increasing performance many-fold. The most constant
objects are identified among those automatically detected, using SExtractor,
on the first image passed as an argument.

The range of the search space is specified in number of times the median FWHM
of the images in each photometric filter, while the step size is given in
pixels. The inner radius of the sky annulus and its width, although also
expressed in FWHMs, is the same for all the apertures. In future releases,
it should be possible to evaluate different sky annuli too.

The output of is a JSON file which lists all the aperture radii that, for each
photometric filter, were evaluated. This file can be passed to the 'photometry'
command with the --annuli option in order to use these optimal apertures.
"""

# LEMON modules
import customparser
import defaults
import diffphot
import fitsimage
import keywords
import json_parse
from juicer import miner
import photometry
import util

logger = logging.getLogger(__name__)


class NotEnoughImages(ValueError):
    pass


class NotEnoughConstantStars(ValueError):
    pass


parser = customparser.get_parser(description)
parser.usage = "%prog [OPTION]... SOURCES_IMG INPUT_IMGS... OUTPUT_JSON_FILE"

parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite output JSON file if it already exists",
)

# Pass-through of shared options from other modules (kept as-is for compatibility)
parser.add_argument("--margin", action="store", type=int, dest="margin",
                    default=defaults.margin, help=defaults.desc.get("margin", ""))
parser.add_argument("--gain", action="store", type=float, dest="gain", default=None,
                    help="CCD gain in e-/ADU. If given, do not read from header (--gaink).")
parser.add_argument("--cores", action="store", type=int, dest="ncores",
                    default=defaults.ncores, help=defaults.desc.get("ncores", ""))
parser.add_argument("-v", "--verbose", action="count", dest="verbose",
                    default=defaults.verbosity, help=defaults.desc.get("verbosity", ""))

# ---------------------------- Initial Photometry ------------------------------
qphot_group = OptionGroup(
    parser,
    "Initial Photometry",
    ("In what may seem sort of a recursive problem, we need to do an initial "
     "(aperture) photometry in order to detect all the stars in the field and "
     "determine which among them are most constant. The value of the photometry "
     "parameters (aperture and sky annuli) are defined in terms of the *median* "
     "FWHM of the images in each band.\n\n"
     "And yes, we are aware that all this means that the search for the optimal "
     "photometric aperture parameters is dependent upon an initial photometry "
     "whose parameters have to be, even in terms of the FWHM, initially specified. "
     "Kind of paradoxical, we know.")
)

qphot_group.add_option(
    "--aperture",
    action="store",
    type=float,
    dest="aperture",
    default=3.0,
    help="aperture radius, in number of times the median FWHM [default: %(default)s]",
)
qphot_group.add_option(
    "--annulus",
    action="store",
    type=float,
    dest="annulus",
    default=4.5,
    help="inner radius of the sky annulus, in times the median FWHM [default: %(default)s]",
)
qphot_group.add_option(
    "--dannulus",
    action="store",
    type=float,
    dest="dannulus",
    default=1.0,
    help="width of the sky annulus, in times the median FWHM [default: %(default)s]",
)

qphot_group.add_option(
    "--min-sky",
    action="store",
    type=float,
    dest="min",
    default=3.0,
    help=("the minimum width of the sky annulus, in pixels, regardless of the value "
          "derived from the FWHM. This option is intended to prevent small FWHMs "
          "from resulting in too thin an sky annulus, and applies to both the "
          "initial photometry and subsequent explorations of the search space "
          "[default = %(default)s]"),
)

# ------------------------------ Comparison stars ------------------------------
stats_group = OptionGroup(
    parser,
    "Comparison stars",
    ("Ideally, two candidate apertures would be compared by means of evaluating "
     "the light curves of all the stars: the premise here is that, the better "
     "the photometric parameters happen to be, the most stable the light curve "
     "of the constant stars will be. However, doing photometry and generating "
     "the light curves for each star and aperture would be extremely impractical "
     "for large data sets. Therefore, our approach is to compare the light curves "
     "of only a subset of the stars: those that are the most constant. Furthermore, "
     "not all of these light curves are used; instead, they are evaluated as a whole "
     "by taking the median of the standard deviation of the most stable among them.")
)

stats_group.add_option(
    "--constant",
    action="store",
    type=float,
    dest="nconstant",
    default=20,
    help=("the number of stars used in order to compare each set of photometric "
          "parameters. For each of these stars, its light curve will be generated "
          "using all the other as comparison (-n option at diffphot.py) "
          "[default: %(default)s]"),
)

stats_group.add_option(
    "--minimum-constant",
    action="store",
    type=int,
    dest="pminimum",
    default=5,
    help=("the minimum number of constant stars which must have had their light "
          "curves for the percentile of the standard deviations to be calculated. "
          "If fewer than this number of light curves are computed, the parameters "
          "are ignored. [default: %(default)s]"),
)
stats_group.add_option(
    "--minimum-images",
    action="store",
    type=int,
    dest="min_images",
    default=getattr(defaults, "MIN_IMAGES", 10),
    help="minimum number of images required for a star's light curve [default: %(default)s]",
)

# -------------------------------- Search space --------------------------------
search_group = OptionGroup(
    parser,
    "Search space",
    ("The number of apertures that will be evaluated is determined by the median "
     "FWHM of the images in each photometric filter, from which the lower and upper "
     "bounds of the candidate apertures are derived. The sky annulus, however, "
     "remains the same for all the candidate apertures that are evaluated.")
)

search_group.add_option(
    "--lower",
    action="store",
    type=float,
    dest="lower",
    default=0.5,
    help="lower bound of candidate aperture radii, as a multiple of the median FWHM [default = %(default)s]",
)

search_group.add_option(
    "--upper",
    action="store",
    type=float,
    dest="upper",
    default=4.5,
    help="upper bound of candidate aperture radii, as a multiple of the median FWHM [default = %(default)s]",
)

search_group.add_option(
    "--step",
    action="store",
    type=float,
    dest="step",
    default=1.0,
    help="increment (in pixels) between candidate apertures [default = %(default)s]",
)

search_group.add_option(
    "--sky",
    action="store",
    type=float,
    dest="sky",
    default=4.6,
    help="inner radius of the sky annulus, in FWHM units [default = %(default)s]",
)

search_group.add_option(
    "--width",
    action="store",
    type=float,
    dest="width",
    default=1.0,
    help="width of the sky annulus, in multiples of each candidate aperture [default = %(default)s]",
)

search_group.add_option(
    "--snr-percentile",
    action="store",
    type=float,
    dest="per",
    default=defaults.snr_percentile,
    help=defaults.desc.get("snr_percentile", ""),
)
search_group.add_option(
    "--mean",
    action="store_true",
    dest="mean",
    help=defaults.desc.get("mean", ""),
)

# ------------------------------ Stars eligibility -----------------------------
const_group = OptionGroup(
    parser,
    "Stars eligibility",
    ("Apart from a stable light curve, a star must not have saturated in any of the "
     "images if it is to be eligible as a constant star. Candidates must be below "
     "saturation in all images.")
)
const_group.add_option(
    "--maximum",
    action="store",
    type=int,
    dest="maximum",
    default=defaults.maximum,
    help=defaults.desc.get("maximum", ""),
)

# --------------------------- Differential Photometry --------------------------
diffphot_group = OptionGroup(
    parser,
    "Differential Photometry",
    ("These options (same as in diffphot.py) determine how light curves are generated.")
)

diffphot_group.add_option(
    "--min-snr",
    action="store",
    type=float,
    dest="min_snr",
    default=1.0,
    help="ignore photometric points with SNR below this threshold [default: %(default)s]",
)
diffphot_group.add_option(
    "--min-cmp",
    action="store",
    type=int,
    dest="min_cmp",
    default=5,
    help="minimum number of comparison stars required per epoch [default: %(default)s]",
)
diffphot_group.add_option(
    "--max-cmp",
    action="store",
    type=int,
    dest="max_cmp",
    default=20,
    help="maximum number of comparison stars per target [default: %(default)s]",
)
diffphot_group.add_option(
    "--robust",
    action="store_true",
    dest="robust",
    default=False,
    help="use robust (MAD) dispersion instead of plain standard deviation",
)
diffphot_group.add_option(
    "--worst-fraction",
    action="store",
    type=float,
    dest="worst_fraction",
    default=0.25,
    help="fraction of worst candidates to discard at each trimming step [default: %(default)s]",
)

# -------------------------------- FITS keywords -------------------------------
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

customparser.clear_metavars(parser)


def check_run(function, *args) -> None:
    """
    Run the function and raise CalledProcessError for non-zero retcodes.
    """
    retcode = function(*args)
    if retcode:
        cmd = f"{function.__name__}{args}"
        raise subprocess.CalledProcessError(retcode, cmd)


def main(arguments: list[str] | None = None) -> int:
    """
    Main entry point (callable).
    """
    if arguments is None:
        arguments = sys.argv[1:]  # ignore argv[0], the script name

    (options, args) = parser.parse_args(args=arguments)

    # Logging level
    logging_level = logging.WARNING
    if getattr(options, "verbose", 0) == 1:
        logging_level = logging.INFO
    elif getattr(options, "verbose", 0) >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    # Need at least: SOURCES_IMG  INPUT_IMGS...  OUTPUT_JSON_FILE
    if len(args) < 2:
        parser.print_help()
        return 2

    sources_img_path = args[0]
    input_paths = list(set(args[1:-1]))
    output_json_path = args[-1]

    # Avoid accidental overwrite
    if Path(output_json_path).exists() and not options.overwrite:
        print(f"{style.prefix}Error. The output file '{output_json_path}' already exists.")
        print(style.error_exit_message)
        return 1

    print(f"{style.prefix}Examining the headers of the {len(input_paths)} FITS files given as input...")

    files = fitsimage.InputFITSFiles()
    for index, img_path in enumerate(input_paths):
        img = fitsimage.FITSImage(img_path)
        pfilter = img.pfilter(options.filterk)
        files[pfilter].append(img)

        percentage = (index + 1) / len(input_paths) * 100.0
        util.show_progress(percentage)

    # progress bar newline + spacer prefix
    print()
    print(style.prefix)

    # Initial photometry
    print(f"{style.prefix}Doing initial photometry with FWHM-derived apertures...")
    print(style.prefix)

    # Temporary LEMONdB for initial photometry
    kwargs = dict(prefix="photometry_", suffix=".LEMONdB")
    phot_db_handle, phot_db_path = tempfile.mkstemp(**kwargs)
    atexit.register(util.clean_tmp_files, phot_db_path)
    os.close(phot_db_handle)

    basic_args = [sources_img_path] + input_paths + [phot_db_path, "--overwrite"]

    phot_args = [
        "--maximum",
        options.maximum,
        "--margin",
        options.margin,
        "--cores",
        options.ncores,
        "--min-sky",
        options.min,
        "--objectk",
        options.objectk,
        "--filterk",
        options.filterk,
        "--datek",
        options.datek,
        "--timek",
        options.timek,
        "--expk",
        options.exptimek,
        "--coaddk",
        options.coaddk,
        "--gaink",
        options.gaink,
        "--fwhmk",
        options.fwhmk,
        "--airmk",
        options.airmassk,
    ]

    if options.gain:
        phot_args += ["--gain", options.gain]
    if options.uncimgk:
        phot_args += ["--uncimgk", options.uncimgk]

    # Verbosity passthrough
    for _ in range(getattr(options, "verbose", 0)):
        phot_args.append("-v")

    extra_args = [
        "--aperture",
        options.aperture,
        "--annulus",
        options.annulus,
        "--dannulus",
        options.dannulus,
    ]

    args_call = basic_args + phot_args + extra_args
    check_run(photometry.main, [str(a) for a in args_call])

    # Generate light curves for initial photometry
    print(style.prefix)
    print(f"{style.prefix}Generating light curves for initial photometry.")
    print(style.prefix)

    # Temporary DB for diffphot results
    kwargs = dict(prefix="diffphot_", suffix=".LEMONdB")
    diffphot_db_handle, diffphot_db_path = tempfile.mkstemp(**kwargs)
    atexit.register(util.clean_tmp_files, diffphot_db_path)
    os.close(diffphot_db_handle)

    diff_args = [
        phot_db_path,
        diffphot_db_path,
        "--overwrite",
        "--cores",
        options.ncores,
        "--min-snr",
        options.min_snr,
        "--min-cmp",
        options.min_cmp,
        "--max-cmp",
        0,
        "--worst-fraction",
        options.worst_fraction,
        "--broeg05-strict",
        "--robust",
    ]
    for _ in range(getattr(options, "verbose", 0)):
        diff_args.append("-v")

    check_run(diffphot.main, [str(a) for a in diff_args])
    print(style.prefix)

    # Prepare coordinates of constant stars per filter
    coordinates_files: dict = {}

    miner = mining.LEMONdBMiner(diffphot_db_path)
    for pfilter in miner.pfilters:
        msg = f"{style.prefix}Identifying the {int(options.nconstant)} most constant stars for the {pfilter} filter..."
        print(msg, end="")
        sys.stdout.flush()

        kwargs = dict(minimum=options.min_images)
        stars_stdevs = miner.sort_by_curve_stdev(pfilter, **kwargs)
        cstars = stars_stdevs[: int(options.nconstant)]

        if len(cstars) < int(options.pminimum):
            raise NotEnoughConstantStars(
                f"fewer than {options.pminimum} stars identified as constant in the "
                f"initial photometry for the {pfilter} filter"
            )
        else:
            logger.info("done.")
        if len(cstars) < int(options.nconstant):
            print(f"{style.prefix}But only {len(cstars)} stars were available. Using them all, anyway.")

        # Coordinates temp file (replace spaces with underscores)
        prefix = f"{str(pfilter).replace(' ', '_')}_"
        kwargs = dict(prefix=prefix, suffix=".coordinates")
        coords_fd, coordinates_files[pfilter] = tempfile.mkstemp(**kwargs)
        atexit.register(util.clean_tmp_files, coordinates_files[pfilter])

        for star_id, _ in cstars:
            ra, dec = miner.get_star(star_id)[2:4]
            os.write(coords_fd, f"{ra:.10f}\t{dec:.10f}\n".encode("utf-8"))
        os.close(coords_fd)

        print(f"{style.prefix}Star coordinates for {pfilter} temporarily saved to {coordinates_files[pfilter]}")

    # Evaluate candidate apertures
    evaluated_annuli = collections.defaultdict(list)

    for pfilter, coords_path in coordinates_files.items():
        print(style.prefix)
        print(f"{style.prefix}Finding the optimal photometric parameters for the {pfilter} filter.")

        if len(files[pfilter]) < int(options.min_images):
            raise NotEnoughImages(f"fewer than {options.min_images} images (--minimum-images) for {pfilter}")

        # Median FWHM for this filter
        print(f"{style.prefix}Calculating the median FWHM for this filter...", end="")
        sys.stdout.flush()

        pfilter_fwhms = []
        for img in files[pfilter]:
            img_fwhm = photometry.get_fwhm(img, options)
            logging.debug("%s: FWHM = %.3f", img.path, img_fwhm)
            pfilter_fwhms.append(img_fwhm)

        fwhm = float(numpy.median(pfilter_fwhms))
        logger.info(" done.")

        # Convert FWHM/params to pixels
        min_aperture = fwhm * float(options.lower)
        max_aperture = fwhm * float(options.upper)
        annulus = fwhm * float(options.sky)
        dannulus = fwhm * float(options.width)

        filter_apertures = numpy.arange(min_aperture, max_aperture, float(options.step))
        assert filter_apertures[0] == min_aperture

        print(f"{style.prefix}FWHM ({pfilter} passband) = {fwhm:.3f} pixels, therefore:")
        print(f"{style.prefix}Aperture radius, minimum = {fwhm:.3f} x {options.lower:.2f} = {min_aperture:.3f} pixels")
        print(f"{style.prefix}Aperture radius, maximum = {fwhm:.3f} x {options.upper:.2f} = {max_aperture:.3f} pixels")
        print(f"{style.prefix}Aperture radius, step = {float(options.step):.2f} pixels, which means that:")
        print(
            f"{style.prefix}Aperture radius, actual maximum = "
            f"{min_aperture:.3f} + {len(filter_apertures)} x {float(options.step):.2f} = "
            f"{max(filter_apertures):.3f} pixels"
        )
        print(f"{style.prefix}Sky annulus, inner radius = {fwhm:.3f} x {float(options.sky):.2f} = {annulus:.3f} pixels")
        print(f"{style.prefix}Sky annulus, width = {fwhm:.3f} x {float(options.width):.2f} = {dannulus:.3f} pixels")

        print(
            f"{style.prefix}{len(filter_apertures)} different apertures in the range "
            f"[{filter_apertures[0]:.2f}, {filter_apertures[-1]:.2f}] to be evaluated:"
        )

        # Evaluate each candidate aperture
        for index, aperture in enumerate(filter_apertures, start=0):
            print(style.prefix)

            # Temp DB for this aperture's photometry
            fd, aper_phot_db_path = tempfile.mkstemp(prefix="photometry_", suffix=".LEMONdB")
            atexit.register(util.clean_tmp_files, aper_phot_db_path)
            os.close(fd)

            paths = [img.path for img in files[pfilter]]
            basic_args = [sources_img_path] + paths + [aper_phot_db_path, "--overwrite"]

            extra_args = [
                "--filter",
                str(pfilter),
                "--coordinates",
                coords_path,
                "--aperture-pix",
                float(aperture),
                "--annulus-pix",
                float(annulus),
                "--dannulus-pix",
                float(dannulus),
            ]

            args_call = basic_args + phot_args + extra_args
            check_run(photometry.main, [str(a) for a in args_call])

            # Temp DB for this aperture's diffphot LC
            fd, aper_diff_db_path = tempfile.mkstemp(prefix="diffphot_", suffix=".LEMONdB")
            atexit.register(util.clean_tmp_files, aper_diff_db_path)
            os.close(fd)

            # Reuse diffphot args (swap paths)
            diff_args[0] = aper_phot_db_path
            diff_args[2] = aper_diff_db_path
            check_run(diffphot.main, [str(a) for a in diff_args])

            miner = mining.LEMONdBMiner(aper_diff_db_path)
            try:
                cstars = miner.sort_by_curve_stdev(pfilter, minimum=options.min_images)
            except mining.NoStarsSelectedError:
                print(f"{style.prefix}No constant stars for this aperture. Ignoring it...")
                continue

            assert len(cstars) <= int(options.nconstant)
            if len(cstars) < int(options.pminimum):
                print(
                    f"{style.prefix}Just {len(cstars)} constant stars, fewer than the allowed minimum "
                    f"of {int(options.pminimum)}, had their light curves calculated for this aperture. "
                    "Ignoring it..."
                )
                continue

            stdevs_median = float(numpy.median([x[1] for x in cstars]))
            params = (float(aperture), float(annulus), float(dannulus), float(stdevs_median))
            candidate = json_parse.CandidateAnnuli(*params)
            evaluated_annuli[pfilter].append(candidate)

            print(f"{style.prefix}Aperture = {float(aperture):.3f}, median stdev ({len(cstars)} stars) = {stdevs_median:.4f}")

            percentage = (index + 1) / len(filter_apertures) * 100.0
            print(f"{style.prefix}{pfilter} progress: {percentage:.2f} %")

        # Best candidate for this filter
        best_candidate = min(evaluated_annuli[pfilter], key=operator.attrgetter("stdev"))
        print(f"{style.prefix}Best aperture found at {best_candidate.aperture:.3f} pixels with stdev = {best_candidate.stdev:.4f}")

    print(style.prefix)
    print(f"{style.prefix}Saving the evaluated apertures to the '{output_json_path}' JSON file ...", end="")
    sys.stdout.flush()
    json_parse.CandidateAnnuli.dump(evaluated_annuli, output_json_path)
    logger.info(" done.")
    print(f"\n{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
