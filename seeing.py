#!/usr/bin/env python3
from __future__ import annotations, division

from pathlib import Path
from util.display import print_exception_traceback
# Copyright (c) 2012 Victor Terron. All rights reserved.
# Institute of Astrophysics of Andalusia, IAA-CSIC
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
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

"""
This module runs SExtractor in parallel on all input FITS files, leveraging
multiple CPU cores. Images with too large FWHM or elongation are automatically
discarded (Gaussian fit + sigma cut). Finally, the 'best-seeing' image is
chosen among the non-discarded ones as the image whose number of sources is at
a chosen percentile and that has the lowest FWHM.
"""

import atexit
import logging
import math
import multiprocessing
import numpy
import argparse
import os
import scipy.stats
import shutil
import sys
import tempfile
import time

# LEMON modules
import util
import astromatic
import customparser
import defaults
import fitsimage
import keywords
import style

from util.queue import Queue
from util import memoize

class OptionGroup:
    """Thin wrapper to mimic optparse.OptionGroup on argparse."""
    def __init__(self, parser: argparse.ArgumentParser, title: str, description: str | None = None):
        self._group = parser.add_argument_group(title=title, description=description)

    def add_argument(self, *opts, **kwargs):
        return self._group.add_argument(*opts, **kwargs)


description = """
This module runs SExtractor on the input FITS files in parallel. Images with a
too large full width at half maximum (FWHM) or elongation (ratio of semi-major
to semi-minor axis) are discarded by fitting a Gaussian distribution to the
values and excluding those above a given sigma. The 'best-seeing' image is
then selected among the non-discarded images at a given sources percentile
with the best FWHM.
"""

# Global queue for worker results
queue = Queue()

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_IMGS... OUTPUT_DIR"

parser.add_argument(
    "--filename",
    action="store",
    type=str,
    dest="bseeingfn",
    default="best_seeing.fits",
    help=(
        "filename used to save the best-seeing FITS image into OUTPUT_DIR. "
        "If empty (''), the image keeps its original filename (with --suffix appended). "
        "[default: %(default)s]"
    ),
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
    "--snr-percentile",
    action="store",
    type=float,
    dest="per",
    default=defaults.snr_percentile,
    help=defaults.desc["snr_percentile"],
)

# Source detection filters
detect_group = OptionGroup(
    parser,
    "Source detection filters",
    "Filters applied to SExtractor detections before counting sources / computing FWHM.",
)
detect_group.add_argument(
    "--detect-min-snr",
    action="store",
    type=float,
    dest="detect_min_snr",
    default=None,
    help="minimum SNR for a detection to be kept (unset disables this filter)",
)
detect_group.add_argument(
    "--detect-max-elongation",
    action="store",
    type=float,
    dest="detect_max_elongation",
    default=None,
    help="maximum elongation (A/B) for a detection to be kept (unset disables this filter)",
)
detect_group.add_argument(
    "--detect-min-area",
    action="store",
    type=int,
    dest="detect_min_area",
    default=None,
    help="minimum ISOAREAF_IMAGE in pixels^2 (unset disables this filter)",
)
detect_group.add_argument(
    "--detect-keep-saturated",
    action="store_true",
    dest="detect_keep_saturated",
    default=False,
    help="keep saturated detections (default: discard them)",
)

parser.add_argument(
    "--mean",
    action="store_true",
    dest="mean",
    help=defaults.desc["mean"],
)

parser.add_argument(
    "--sources-percentile",
    action="store",
    type=float,
    dest="stars_per",
    default=75.0,
    help=(
        "Among the non-discarded images, consider those whose #sources is at this percentile, "
        "then choose the one with the best (lowest) FWHM. [default: %(default)s]"
    ),
)

parser.add_argument(
    "--suffix",
    action="store",
    type=str,
    dest="suffix",
    default="s",
    help="string appended to output images before the extension. [default: %(default)s]",
)

parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite any output file if it already exists",
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

# FWHM group
fwhm_group = OptionGroup(
    parser,
    "Full width at half maximum",
    (
        "After SExtractor runs, the image FWHM is computed as the median or mean of stars "
        "not too close to the margins (see --mean and --margin) and a Gaussian is fit. "
        "Images exceeding mu + N*sigma are discarded."
    ),
)
fwhm_group.add_argument(
    "--fsigma",
    action="store",
    type=float,
    dest="fwhm_sigma",
    default=3.0,
    help="maximum allowed (mu + N*sigma) for FWHM. [default: %(default)s]",
)
fwhm_group.add_argument(
    "--fwhm_dir",
    action="store",
    type=str,
    dest="fwhm_dir",
    default="fwhm_discarded",
    help="subdirectory (under OUTPUT_DIR) for FWHM-discarded images. [default: %(default)s]",
)

# Elongation group
elong_group = OptionGroup(
    parser,
    "Elongation",
    (
        "After discarding by FWHM, elongation is treated analogously using the SExtractor "
        "ELONGATION parameter (A/B). Images with excessive elongation are discarded."
    ),
)
elong_group.add_argument(
    "--esigma",
    action="store",
    type=float,
    dest="elong_sigma",
    default=3.0,
    help="maximum allowed (mu + N*sigma) for elongation. [default: %(default)s]",
)
elong_group.add_argument(
    "--elong_dir",
    action="store",
    type=str,
    dest="elong_dir",
    default="elong_discarded",
    help="subdirectory (under OUTPUT_DIR) for elongation-discarded images. [default: %(default)s]",
)

# FITS keywords group
key_group = OptionGroup(parser, "FITS Keywords", keywords.group_description)
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
    help=(
        "keyword to write estimated FWHM of the image (consumed by later pipeline steps). "
        "[default: %(default)s]"
    ),
)

customparser.clear_metavars(parser)


class FITSeeingImage(fitsimage.FITSImage):
    """High-level interface to each FITS image's SExtractor catalog."""

    def __init__(
        self,
        path,
        maximum,
        margin,
        coaddk=keywords.coaddk,
        gaink: str = keywords.gaink,
        min_snr: float | None = None,
        max_elongation: float | None = None,
        min_area: int | None = None,
        keep_saturated: bool = False,
    ):
        super().__init__(path)
        self.margin = margin
        self.min_snr = min_snr
        self.max_elongation = max_elongation
        self.min_area = min_area
        self.keep_saturated = keep_saturated
        logging.debug(f"{self.path}: margin width: {self.margin} px")

        # Compute SATUR_LEVEL with effective coadds
        satur_level = self.saturation(maximum, coaddk=coaddk)
        options = dict(SATUR_LEVEL=str(satur_level))
        try:
            gain = float(self.read_keyword(gaink))
            if math.isfinite(gain) and gain > 0:
                options["GAIN"] = str(gain)
        except Exception:
            pass
        sex_md5sum = astromatic.sextractor_md5sum(options=options)
        logging.debug(f"{self.path}: SExtractor MD5 hash: {sex_md5sum}")

        try:
            try:
                self.catalog_path = self.read_keyword(keywords.sex_catalog)
            except KeyError:
                logging.debug("%s: keyword '%s' not found", self.path, keywords.sex_catalog)
                raise

            logging.debug(f"{self.path}: on-disk catalog {self.catalog_path}")
            if not Path(self.catalog_path).exists():
                logging.debug(f"{self.path}: on-disk catalog {self.catalog_path} does not exist")
                raise IOError

            try:
                img_sex_md5sum = self.read_keyword(keywords.sex_md5sum)
                logging.debug(
                    "%s: on-disk catalog SExtractor MD5 hash (%s): %s",
                    self.path, keywords.sex_md5sum, img_sex_md5sum,
                )
            except KeyError:
                logging.debug("%s: keyword '%s' not found", self.path, keywords.sex_md5sum)
                raise

            if img_sex_md5sum != sex_md5sum:
                logging.debug("%s: outdated catalog (SExtractor hashes do not match)", self.path)
                try:
                    os.unlink(self.catalog_path)
                    logging.debug("%s: outdated catalog removed from disk", self.path)
                except (IOError, OSError) as e:
                    logging.warning("%s: could not remove outdated catalog (%s)", self.path, e)
                finally:
                    raise ValueError

            logging.debug("%s: on-disk catalog exists and MD5 hashes match.", self.path)

        except (KeyError, IOError, ValueError):
            logging.debug(
                "%s: cannot reuse cached catalog; running SExtractor", self.path
            )
            with open(os.devnull, "wt") as fd:
                logging.info("%s: running SExtractor", self.path)
                self.catalog_path = astromatic.sextractor(
                    self.path, options=options, stdout=fd, stderr=fd
                )
                logging.debug("%s: SExtractor OK", self.path)
                try:
                    self.update_keyword(keywords.sex_catalog, str(self.catalog_path))
                    self.update_keyword(keywords.sex_md5sum, sex_md5sum)
                except (IOError, ValueError):
                    pass

    @property
    def catalog(self):
        """Return the SExtractor catalog (excluding border-margin sources)."""
        self._ignored_sources = 0
        self._filtered_sources = 0
        catalog = astromatic.Catalog(self.catalog_path)
        logging.info("Removing objects too close to edges of %s", self.path)
        logging.debug("Margin: %d px | Image size: (%d, %d)", self.margin, *self.size)

        kept = []
        for star in catalog:
            if (
                star.x < self.margin
                or star.x > (self.x_size - self.margin)
                or star.y < self.margin
                or star.y > (self.y_size - self.margin)
            ):
                logging.debug("Star at %.3f, %.3f ignored (too close to edges)", star.x, star.y)
                self._ignored_sources += 1
            elif not self.keep_saturated and star.saturated:
                logging.debug("Star at %.3f, %.3f ignored (saturated)", star.x, star.y)
                self._filtered_sources += 1
            elif self.min_snr is not None and star.snr < float(self.min_snr):
                logging.debug("Star at %.3f, %.3f ignored (SNR %.3f < %.3f)",
                              star.x, star.y, star.snr, float(self.min_snr))
                self._filtered_sources += 1
            elif self.max_elongation is not None and star.elongation > float(self.max_elongation):
                logging.debug("Star at %.3f, %.3f ignored (elong %.3f > %.3f)",
                              star.x, star.y, star.elongation, float(self.max_elongation))
                self._filtered_sources += 1
            elif self.min_area is not None and star.area < int(self.min_area):
                logging.debug("Star at %.3f, %.3f ignored (area %d < %d)",
                              star.x, star.y, star.area, int(self.min_area))
                self._filtered_sources += 1
            elif not (math.isfinite(star.snr) and math.isfinite(star.fwhm) and math.isfinite(star.elongation)):
                logging.debug("Star at %.3f, %.3f ignored (non-finite metrics)", star.x, star.y)
                self._filtered_sources += 1
            else:
                kept.append(star)
                logging.debug("Star at %.3f, %.3f OK", star.x, star.y)

        if __debug__:
            assert len(catalog) == len(kept) + self._ignored_sources + self._filtered_sources

        return astromatic.Catalog.from_sequence(kept)

    def __len__(self):
        return len(self.catalog)

    @property
    def ignored(self):
        try:
            return self._ignored_sources
        except AttributeError:
            _ = self.catalog
            return self._ignored_sources

    @property
    def filtered(self):
        try:
            return self._filtered_sources
        except AttributeError:
            _ = self.catalog
            return self._filtered_sources

    @property
    def total(self):
        return len(self) + self.ignored + self.filtered

    def __getitem__(self, key):
        return self.catalog[key]

    @property
    def coordinates(self):
        return self.catalog.get_sky_coordinates()

    def snr_percentile(self, per: float):
        snrs = [star.snr for star in self if not star.saturated]
        if not snrs:
            raise ValueError(f"no stars available to compute SNR percentile for '{self.path}'")
        return scipy.stats.scoreatpercentile(snrs, per)

    def fwhm(self, per=50, mode="median"):
        if mode not in ("median", "mean"):
            raise ValueError("'mode' must be 'median' or 'mean'")
        snr_thresh = self.snr_percentile(per)
        fwhms = [s.fwhm for s in self if (not s.saturated) and (s.snr >= snr_thresh)]
        if not fwhms:
            raise ValueError("no stars available to compute the FWHM")
        return numpy.median(fwhms) if mode == "median" else numpy.mean(fwhms)

    def elongation(self, per=50, mode="median"):
        if mode not in ("median", "mean"):
            raise ValueError("'mode' must be 'median' or 'mean'")
        snr_thresh = self.snr_percentile(per)
        elongs = [s.elongation for s in self if (not s.saturated) and (s.snr >= snr_thresh)]
        if not elongs:
            raise ValueError("no stars available to compute the elongation")
        return numpy.median(elongs) if mode == "median" else numpy.mean(elongs)


def parallel_sextractor(args):
    """Run SExtractor and compute FWHM and elongation for a FITS image."""
    path, options = args
    mode = "mean" if options.mean else "median"

    try:
        # Work on a temp copy so we can write header updates safely
        fd, output_path = tempfile.mkstemp(prefix=f"{Path(path).name}_", suffix=".fits")
        os.close(fd)
        shutil.copy2(path, output_path)
        util.owner_writable(output_path, True)  # chmod u+w

        image = FITSeeingImage(
            output_path,
            options.maximum,
            options.margin,
            coaddk=options.coaddk,
            min_snr=options.detect_min_snr,
            max_elongation=options.detect_max_elongation,
            min_area=options.detect_min_area,
            keep_saturated=options.detect_keep_saturated,
        )
        fwhm = image.fwhm(per=options.per, mode=mode)
        logging.debug("%s: FWHM = %.3f", path, fwhm)
        elong = image.elongation(per=options.per, mode=mode)
        logging.debug("%s: Elongation = %.3f", path, elong)
        nstars = len(image)
        logging.debug("%s: %d sources detected", path, nstars)

        queue.put((path, output_path, fwhm, elong, nstars))

    except fitsimage.NonStandardFITS:
        logging.info("%s ignored (non-standard FITS)", path)
        return
    except ValueError as e:
        logging.info("%s ignored (%s)", path, str(e))


def main(arguments=None):
    """CLI entry point."""
    if arguments is None:
        arguments = sys.argv[1:]

    (options, args) = parser.parse_args(args=arguments)

    # Logger level
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    # Need at least one input and an output directory
    if len(args) < 2:
        parser.print_help()
        return 2

    input_paths = args[:-1]
    output_dir = args[-1]

    # Ensure output directory exists
    util.determine_output_dir(output_dir)
    fwhm_dir = Path(output_dir) / options.fwhm_dir
    elong_dir = Path(output_dir) / options.elong_dir

    print(f"{style.prefix}{len(input_paths)} paths given as input; detecting sources...")
    print(f"{style.prefix}Running SExtractor on all the FITS images...")

    # Run in parallel
    pool = multiprocessing.Pool(options.ncores)
    map_async_args = ((path, options) for path in input_paths if Path(path).is_file())
    result = pool.map_async(parallel_sextractor, map_async_args)

    util.show_progress(0.0)
    while not result.ready():
        time.sleep(1)
        total = max(1, len(input_paths))
        util.show_progress(queue.qsize() / total * 100.0)
        if logging_level < logging.WARNING:
            print()

    result.get()  # re-raise exceptions if any
    util.show_progress(100.0)
    print()

    all_images = set()
    fwhm_discarded = set()
    elong_discarded = set()
    seeing_tmp_paths: dict[str, str] = {}

    fwhms: dict[str, float] = {}
    elongs: dict[str, float] = {}
    nstars: dict[str, int] = {}

    for _ in range(queue.qsize()):
        path, output_tmp_path, fwhm, elong, stars = queue.get()
        all_images.add(path)
        seeing_tmp_paths[path] = output_tmp_path
        atexit.register(util.clean_tmp_files, output_tmp_path)
        fwhms[path] = fwhm
        elongs[path] = elong
        nstars[path] = stars

    if not all_images:
        print(f"{style.prefix}Error. No FITS images were detected.")
        print(style.error_exit_message)
        return 1

    # Discard by FWHM (Gaussian fit)
    print(f"{style.prefix}Fitting a Gaussian distribution to the FWHMs...", end=" ")
    sys.stdout.flush()
    logging.debug("Fitting Gaussian to %d FWHMs", len(fwhms))
    mu, sigma = scipy.stats.norm.fit(list(fwhms.values()))
    print("done.")
    sys.stdout.flush()

    print(f"{style.prefix}FWHMs mean = %.3f, sigma = %.3f pixels" % (mu, sigma))
    maximum_fwhm = mu + (options.fwhm_sigma * sigma)
    logging.debug(
        "Max allowed FWHM = %.3f + %.1f x %.3f = %.3f px",
        mu, options.fwhm_sigma, sigma, maximum_fwhm,
    )
    print(
        f"{style.prefix}Discarding images with FWHM > %.3f + %.1f x %.3f = %.3f pixels..."
        % (mu, options.fwhm_sigma, sigma, maximum_fwhm)
    )

    for path, fwhm in sorted(fwhms.items()):
        if fwhm > maximum_fwhm:
            fwhm_discarded.add(path)
            logging.debug("%s discarded (FWHM = %.3f > %.3f)", path, fwhm, maximum_fwhm)
            print(f"{style.prefix}{path} discarded (FWHM = %.3f)" % fwhm)

    logging.info("Images discarded by FWHM: %d", len(fwhm_discarded))
    if not fwhm_discarded:
        print(f"{style.prefix}No images were discarded because of their FWHM. Hooray!")
    else:
        discarded_fraction = len(fwhm_discarded) / len(all_images) * 100.0
        nleft = len(all_images) - len(fwhm_discarded)
        print(
            f"{style.prefix}{len(fwhm_discarded)} FITS images (%.2f %%) discarded, {nleft} remain"
            % discarded_fraction
        )

    # Discard by elongation
    print(f"{style.prefix}Fitting a Gaussian distribution to the elongations...", end=" ")
    sys.stdout.flush()
    mu, sigma = scipy.stats.norm.fit(list(elongs.values()))
    print("done.")
    sys.stdout.flush()

    print(f"{style.prefix}Elongation mean = %.3f, sigma = %.3f" % (mu, sigma))
    maximum_elong = mu + (options.elong_sigma * sigma)
    logging.debug(
        "Max allowed elongation = %.3f + %.1f x %.3f = %.3f",
        mu, options.elong_sigma, sigma, maximum_elong,
    )
    print(
        f"{style.prefix}Discarding images with elongation > %.3f + %.1f x %.3f = %.3f ..."
        % (mu, options.elong_sigma, sigma, maximum_elong)
    )

    for path, elong in sorted(elongs.items()):
        if path in fwhm_discarded:
            logging.debug("%s ignored (already discarded by FWHM)", path)
            continue
        if elong > maximum_elong:
            elong_discarded.add(path)
            logging.debug("%s discarded (elongation = %.3f > %.3f)", path, elong, maximum_elong)
            print(f"{style.prefix}{path} discarded (elongation = %.3f)" % elong)

    logging.info("Images discarded by elongation: %d", len(elong_discarded))
    if not elong_discarded:
        print(f"{style.prefix}No images were discarded because of their elongation. Yay!")
    else:
        initial_size = len(all_images) - len(fwhm_discarded)
        discarded_fraction = len(elong_discarded) / max(1, initial_size) * 100.0
        nleft = initial_size - len(elong_discarded)
        print(
            f"{style.prefix}{len(elong_discarded)} FITS images (%.2f %%) discarded, {nleft} remain"
            % discarded_fraction
        )

    # Pick best-seeing among most populated
    print(
        f"{style.prefix}Identifying images whose #sources is at the %.2f percentile..."
        % options.stars_per,
        end=" ",
    )
    sys.stdout.flush()
    for path in fwhm_discarded.union(elong_discarded):
        if path in nstars:
            del nstars[path]
        reason = "FWHM" if path in fwhm_discarded else "elongation"
        logging.debug("%s ignored (discarded by %s)", path, reason)

    min_nstars = scipy.stats.scoreatpercentile(list(nstars.values()), options.stars_per)
    print("done.")

    print(
        f"{style.prefix}Sources percentile threshold = %d; taking images with at least this many..."
        % int(min_nstars),
        end=" ",
    )
    sys.stdout.flush()
    most_populated_images = [path for path, stars in nstars.items() if stars >= min_nstars]
    logging.debug(
        "There are %s images with #sources at the %.2f percentile",
        len(most_populated_images), options.stars_per,
    )
    logging.debug("Identifying image with lowest FWHM")
    print("done.")

    print(
        f"{style.prefix}Finding the image with the lowest FWHM among these {len(most_populated_images)} images...",
        end=" ",
    )
    sys.stdout.flush()
    best_seeing = min(most_populated_images, key=lambda p: fwhms[p])
    logging.debug("Best-seeing image: %s", best_seeing)
    logging.debug("Best-seeing FWHM = %.3f | elongation = %.3f | sources = %d",
                  fwhms[best_seeing], elongs[best_seeing], nstars[best_seeing])
    assert best_seeing not in fwhm_discarded
    assert best_seeing not in elong_discarded
    print("done.")

    print(
        f"{style.prefix}Best-seeing image = {best_seeing}, with {nstars[best_seeing]} sources "
        f"and a FWHM of %.3f pixels" % fwhms[best_seeing]
    )

    # Create discard subdirs only if needed
    if fwhm_discarded:
        util.determine_output_dir(fwhm_dir, quiet=True)
    if elong_discarded:
        util.determine_output_dir(elong_dir, quiet=True)

    # Copy all images to output dir (or subdirs if discarded)
    processed = 0
    for path in sorted(all_images):
        root, ext = os.path.splitext(Path(path).name)
        output_filename = root + options.suffix + ext

        if path in fwhm_discarded:
            output_path = Path(fwhm_dir) / output_filename
            history_msg1 = f"Image discarded by LEMON on {util.utctime()}"
            history_msg2 = "[Discarded] FWHM = %.3f px, max allowed = %.3f" % (fwhms[path], maximum_fwhm)

        elif path in elong_discarded:
            output_path = Path(elong_dir) / output_filename
            history_msg1 = f"Image discarded by LEMON on {util.utctime()}"
            history_msg2 = "[Discarded] Elongation = %.3f, max allowed = %.3f" % (elongs[path], maximum_elong)

        elif path == best_seeing:
            filename = output_filename if not options.bseeingfn else options.bseeingfn
            output_path = Path(output_dir) / filename
            history_msg1 = "Image identified by LEMON as the 'best-seeing' one"
            history_msg2 = (
                "FWHM = %.3f | Elongation = %.3f | Sources: %d (at %.2f percentile)"
                % (fwhms[path], elongs[path], nstars[path], options.stars_per)
            )
        else:
            output_path = Path(output_dir) / output_filename
            history_msg1 = "Image FWHM = %.3f" % fwhms[path]
            history_msg2 = "Image elongation = %.3f" % elongs[path]

        if Path(output_path).exists() and not options.overwrite:
            print(
                "%sError. Output FITS file '%s' already exists. You need to use --overwrite."
                % (style.prefix, output_path)
            )
            print(style.error_exit_message)
            return 1

        src = seeing_tmp_paths[path]
        shutil.move(src, output_path)
        util.owner_writable(output_path, True)

        output_img = fitsimage.FITSImage(output_path)
        output_img.add_history(history_msg1)
        output_img.add_history(history_msg2)

        comment = "Margin = %d, SNR percentile = %.3f" % (options.margin, options.per)
        output_img.update_keyword(options.fwhmk, fwhms[path], comment=comment)

        print(f"{style.prefix}FITS image {path} saved to {output_path}")
        processed += 1

    print(f"{style.prefix}A total of {processed} images was saved to '{output_dir}'.")
    print(f"{style.prefix}We're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())

logger = logging.getLogger(__name__)
