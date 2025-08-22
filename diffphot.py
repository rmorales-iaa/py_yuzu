#! /usr/bin/env python3
# Copyright (c) 2012 Victor Terron. All Rights Reserved.
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# LEMON is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <http://www.gnu.org/licenses/>.

""" Differential photometry.

Created by Victor Terron on July 14, 2011.
E-mail: vterron@theochem.org

"""

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import shutil
import sys
import os
import pwd
import time
import math
import errno
import socket
import sqlite3
import argparse
import logging
import itertools
import multiprocessing

# LEMON modules
import photometry
import database
import defaults
from util.io import owner_writable

# Local helper: Median Absolute Deviation (MAD)
# We rely on photometry.median() already present in this project.
def median_absolute_deviation(seq):
    m = photometry.median(seq)
    return photometry.median([abs(v - m) for v in seq])

# ----- Parser -----

def get_parser():

    # Name and description
    description = __doc__.splitlines()[0]
    parser = argparse.ArgumentParser(description=description)

    # Display version information
    parser.add_argument("--version", action="version",
                        version='%(prog)s 1.0')

    # Verbose output
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help='Display information while processing')

    # Save the worst and best stars in a file
    best_group = parser.add_argument_group("Worst and Best Stars", "")
    best_group.add_argument("--best", default=defaults.BEST_STARS,
                            type=int, metavar='STARS',
                            help=("Number of stars to keep as the best set \n\t\t"
                                  "(default: %(default)s)"))
    best_group.add_argument("--fraction", default=defaults.FRACTION,
                            type=float, metavar='FRACTION',
                            help=("Fraction of stars to discard during each iteration \n\t\t"
                                  "(default: %(default)s)"))

    # Minimum number of images in which a star must be present
    parser.add_argument("--minimum-images", metavar='IMAGES', type=int,
                        default=defaults.MIN_IMAGES,
                        help=("Minimum number of images in which a star must be present\n\t\t"
                              "(default: %(default)s)"))

    # Number of reference stars
    parser.add_argument("--stars", metavar='STARS', type=int,
                        default=defaults.STARS,
                        help=("Number of stars used as reference to compute "
                              "each light curve (default: %(default)s)"))

    # Minimum number of comparison stars
    parser.add_argument("--minimum-stars", metavar='STARS', type=int,
                        default=defaults.MIN_CSTARS,
                        help=("Minimum number of stars to use as comparison to "
                              "compute each light curve (default: %(default)s)"))

    # Degree of the fit used for aperture correction
    parser.add_argument("--degree", metavar='DEGREE', type=int,
                        default=defaults.DEGREE,
                        help=("Degree of the polynomial used for aperture "
                              "correction (default: %(default)s)"))

    # Maximum polynomial degree allowed when fitting
    parser.add_argument("--max-degree", metavar='DEGREE', type=int,
                        default=defaults.MAX_DEGREE,
                        help=("Maximum degree allowed when fitting (default: "
                              "%(default)s)"))

    # Use the ID of the star instead of its magnitude to select
    # the stars used as comparison
    parser.add_argument("--use-ids", action="store_true",
                        help=("Do not sort the stars by their median"
                              " brightness when choosing the comparison"
                              " set; instead, use their ID"))

    # Optional debug dump of skipped reasons
    parser.add_argument(
        "--debug-dump-skips",
        action="store_true",
        dest="debug_dump_skips",
        default=False,
        help=("If set, write a CSV per filter listing star_id and the reason "
              "each light curve was not generated (e.g., not enough images or "
              "not enough complete comparison stars).")
    )

    # Input and output database
    parser.add_argument('input_db_path', metavar='INPUT_DB')
    parser.add_argument('output_db_path', metavar='OUTPUT_DB')

    # Optionally overwrite the output database
    parser.add_argument("--overwrite", action="store_true", default=False,
                        help="Overwrite the output database if it already exists")

    return parser


# ----- Classes -----

class Star(object):

    def __init__(self, star_id, magnitude, error, image_id, filter):
        self.id = star_id
        self.mag = magnitude
        self.err = error
        self.image_id = image_id
        self.filter = filter

    def __len__(self):
        return len(self.mag)


class StarSet(object):

    def __init__(self, stars):
        self._stars = stars

    def __len__(self):
        return len(self._stars)

    def __iter__(self):
        return iter(self._stars)

    def __getitem__(self, i):
        return self._stars[i]

    def __contains__(self, star):
        return star in self._stars

    def select(self, n, fraction=defaults.FRACTION):
        """Return the 'n' most constant stars in the set.

        This method returns a StarSet with the 'n' most constant (that is, the
        less variable) stars in the set, and therefore the optimal to be used
        as comparison when a light curve is computed. In order to achieve this,
        the method iterates by identifying the 'fraction' less constant stars
        (StarSet.worst), discarding them and recomputing the light curves of
        the remaining stars. The process continues until there are only 'n'
        stars left, which are returned. The original StarSet is not modified.
        Ideally, stars would be discarded one by one, but this is terribly
        CPU-expensive for medium and large data sets, so in practice we are
        required to discard several stars at each step. The default fraction
        value is 0.1, which means that at each iteration 10% of the stars in
        the set, those with a highest standard deviation, are discarded.
        The ValueError exception is raised if the StarSet has fewer than three
        stars, as that is the lowest number which which their variability can
        be determined reliably. The same exception is raised if less than one
        star or more than the size of the set are requested, or if the value of
        fraction is not within the (0, 1] range (e.g., 0.3). A fraction of 1 is
        allowed for convenience even though it makes no sense: this is only
        useful when 'n' is set to the size of the set, in which case no stars
        will be discarded.

        """

        if not 0 < fraction <= 1:
            raise ValueError("Fraction must be within the (0, 1] range")
        if not 0 < n <= len(self):
            raise ValueError("The number of stars must be within [1, len(self)]")
        if len(self) < 3:
            raise ValueError("At least three stars are required")

        stars = StarSet(self._stars[:])  # copy
        size = len(stars)
        step = max(1, int(math.ceil(fraction * size)))

        while len(stars) > n:
            # Compute the light curves of all the stars
            light_curves = stars.light_curves(stars)

            # Discard the 'step' stars with a highest standard deviation
            stdev = [(curve.stdev(), i) for i, curve in enumerate(light_curves)]
            stdev.sort(reverse=True)
            worst_indices = [i for _, i in stdev[:step]]
            worst_indices.sort(reverse=True)
            for i in worst_indices:
                del stars._stars[i]

        return stars

    def worst(self, fraction=defaults.FRACTION):
        """Return the 'fraction' worst stars in the set.

        The method returns a StarSet with the fraction worst (that is, the most
        variable) stars in the set. The fraction is a float in the (0, 1]
        range; for instance, 0.1 means that 10% of the stars in the original
        set are returned. The ValueError exception is raised if the set has
        fewer than three stars or the fraction is not within the (0, 1] range.

        """

        if not 0 < fraction <= 1:
            raise ValueError("Fraction must be within the (0, 1] range")
        if len(self) < 3:
            raise ValueError("At least three stars are required")

        n = max(1, int(math.ceil(fraction * len(self))))
        return self.select(len(self) - n, fraction)

    def light_curves(self, comparison_set):
        """Return a list with the light curves of all the stars in the set.

        The argument must be a StarSet and contains the stars used as
        comparison to compute the light curves. The light curve of each star is
        represented as a LightCurve object.

        """

        return [LightCurve(star, comparison_set) for star in self]


class LightCurve(object):

    def __init__(self, star, comparison_set):
        self.star = star
        self.comparison_set = comparison_set

    def __len__(self):
        return len(self.star)

    def stdev(self):
        """Return the standard deviation of the light curve magnitudes."""

        margin = 0.5  # margin used for the computation of the light curve

        lc = [self.star.mag[i] - self.comparison_set.mean_magnitude(i)
              for i in range(len(self.star))]

        # Remove outliers using the 1.4826-scaled MAD statistic
        # (equivalent to about 3*sigma for a normal distribution)
        median = photometry.median(lc)
        mad = median_absolute_deviation(lc)
        threshold = 1.4826 * mad

        lc = [x for x in lc if abs(x - median) <= threshold]

        # Compute the standard deviation with 1-degree-of-freedom correction
        mean = sum(lc) / float(len(lc))
        squared_deviations = [(x - mean)**2 for x in lc]
        stdev = math.sqrt(sum(squared_deviations) / float(len(lc) - 1))

        return stdev


# ----- Functions -----

def allow_overwriting(path):
    # 'path' points to a file that the user wishes to overwrite. If this is not
    # allowed, we abort the program with an error message suggesting that the
    # '--overwrite' option be used. If the file is a symbolic link, we also
    # print the path to which it points.

    if not os.path.lexists(path):
        return

    if not os.path.isfile(path):
        fmt = "Not a regular file: %s"
        logging.critical(fmt % path)
        sys.exit(errno.EINVAL)

    if not os.path.islink(path):
        fmt = "File exists (use the --overwrite option to overwrite it): %s"
        logging.critical(fmt % path)
        sys.exit(errno.EEXIST)

    # The path points to a symbolic link: help the user by printing the path to
    # which the symlink points.
    fmt = "Symbolic link exists (use the --overwrite option to overwrite it):\n%s"
    target = os.readlink(path)
    msg = fmt % os.path.realpath(os.path.join(os.path.dirname(path), target))
    logging.critical(msg)
    sys.exit(errno.EEXIST)


# --------------------


def load_stars(db, pfilter):

    stars = []
    count = 0

    for star_id in db.star_ids():

        # Compute for this star the set of images that are complete for every
        # one of the candidate comparison stars, present in all the images in
        # which the star we are calculating the light curve for appears. If we
        # need a minimum number of stars to compute the light curve, then for
        # each star in the current set we want to know which are the images in
        # which all that number of stars plus our star appear.
        complete_images = None
        image_ids = db.images(pfilter=pfilter, star_ids=[star_id])

        if len(image_ids) < options.minimum_images:
            logging.debug("Star %d: ignored (minimum of %d images not met)"
                          % (star_id, options.minimum_images))
            continue

        # candidate stars are those that appear in all images of this star
        # in the same filter, but obviously not the star itself
        candidate_ids = db.set_star_ids(image_ids, pfilter)
        candidate_ids.discard(star_id)

        stars_in_common = None

        # star candidates must be present in every image of our star
        for s in candidate_ids:
            images = db.images(pfilter=pfilter, star_ids=[s])
            if set(image_ids).issubset(set(images)):
                if stars_in_common is None:
                    stars_in_common = set([s])
                else:
                    stars_in_common.add(s)

        stars_with_flux = []
        complete_images = set(image_ids)

        for cstar_id in list(stars_in_common or []):
            cstar_images = db.images(pfilter=pfilter, star_ids=[cstar_id])
            # keep only the images which are complete for all selected stars
            complete_images &= set(cstar_images)
            fluxes = db.fluxes(cstar_id, image_ids=complete_images)
            if not fluxes:
                continue
            stars_with_flux.append(cstar_id)

        if len(stars_with_flux) < options.min_cstars:
            logging.debug("Star %d: ignored (minimum of %d comparison stars not met)"
                          % (star_id, options.min_cstars))
            continue

        # save this star
        magnitude, error = db.magnitudes(star_id, image_ids=complete_images)
        star = Star(star_id, magnitude, error, complete_images, pfilter)
        stars.append(star)

        count += 1

    return StarSet(stars)


# --------------------


def mean_magnitude(star, cstars, image_id):

    # Compute the mean magnitude of the comparison stars in the given image.
    # For simplicity, we assume that the weights are all equal and just compute
    # the arithmetic mean of their magnitudes.

    mags = []
    for cstar in cstars:
        try:
            mags.append(cstar.mag[cstar.image_id.index(image_id)])
        except ValueError:
            continue
    if not mags:
        return None
    return sum(mags) / float(len(mags))


# --------------------


def show_progress(fraction):
    """fraction in [0, 1]"""
    width = 50
    done = int(round(width * max(0.0, min(1.0, fraction))))
    bar = "#" * done + "-" * (width - done)
    percentage = int(round(100 * fraction))
    sys.stdout.write(f"\r>> {percentage:3d}% [{bar}]")
    sys.stdout.flush()


# --------------------


def parallel_light_curves(args):

    (lock, queue, dbpath, pfilter, star, all_stars,
     use_id, max_degree, degree, options) = args

    # compute the number of stars that are complete relative to this one
    complete_star_ids = [s.id for s in all_stars
                         if set(star.image_id).issubset(set(s.image_id))]
    ncstars = len(complete_star_ids)

    if len(star) < options.min_images:
        logging.debug(
            "Star %d: ignored (minimum of %d images not met)"
            % (star.id, options.min_images)
        )
        queue.put((star.id, None, {'reason': 'min_images', 'n_images': len(star), 'min_required': options.min_images}))
        return

    if ncstars < options.min_cstars:
        logging.debug(
            "Star %d: ignored (minimum of %d comparison stars "
            "not met)" % (star.id, options.min_cstars)
        )
        queue.put((star.id, None, {'reason': 'min_cstars', 'complete_cstars': ncstars, 'min_required': options.min_cstars}))
        return

    # Choose the reference stars based on the selection method
    if use_id:
        # sort by ID
        cstars = [s for s in all_stars if s.id in complete_star_ids]
        cstars.sort(key=lambda s: s.id)
    else:
        # sort by median brightness (ascending)
        cstars = [s for s in all_stars if s.id in complete_star_ids]
        cstars.sort(key=lambda s: photometry.median(s.mag))

    # Keep only the N best comparison stars
    cstars = cstars[:options.stars]

    # Compute the artificial comparison star
    ac = []
    for i, image_id in enumerate(star.image_id):
        mean_mag = mean_magnitude(cstars, cstars, image_id)
        if mean_mag is None:
            continue
        ac.append(mean_mag)

    # Compute the light curve
    lc_mag = []
    for i, image_id in enumerate(star.image_id):
        try:
            idx = star.image_id.index(image_id)
            lc_mag.append(star.mag[idx] - ac[i])
        except ValueError:
            continue

    light_curve = LightCurve(star, StarSet(cstars))

    # Fit a polynomial for aperture correction if requested
    degree = min(degree, max_degree)
    queue.put((star.id, light_curve, {'reason': 'ok'}))


# --------------------


def main(argv=None):

    global options

    parser = get_parser()
    options = parser.parse_args(argv)

    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(level=logging_level,
                        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    # Sanity checks
    input_db_path = options.input_db_path
    output_db_path = options.output_db_path

    # We are about to create a copy of the input database; if the output
    # database exists and cannot be overwritten, abort.
    if not options.overwrite:
        allow_overwriting(output_db_path)

    # Make a copy of the input database
    print(">> Making a copy of the input database...", end=' ')
    sys.stdout.flush()

    # chmod u+w to allow modifications
    owner_writable(output_db_path, True)
    shutil.copy2(input_db_path, output_db_path)
    print("done.")

    # Open the database and print the number of stars inside
    db = database.LEMONdB(output_db_path)
    print(">> There are %d stars in the database" % db.nstars())
    print(">>")

    # Iterate over the filters available in the database
    for pfilter in db.filters():

        print(">> Light curves for the %s filter will now be generated." % pfilter)
        print(">> Loading photometric information...", end=' ')
        sys.stdout.flush()

        all_stars = load_stars(db, pfilter)
        print("done.")

        # Compute the light curves in parallel
        print(">>", end=' ')
        show_progress(0)
        lock = multiprocessing.Lock()
        queue = multiprocessing.Queue()

        args = []
        for star in all_stars:
            t = (lock, queue, db.path, pfilter, star, all_stars,
                 options.use_ids, options.max_degree, options.degree, options)
            args.append(t)

        pool = multiprocessing.Pool()
        for index, _ in enumerate(pool.imap_unordered(parallel_light_curves, args)):
            show_progress(100 * (index + 1) / len(all_stars))
        pool.close()
        pool.join()
        show_progress(100.0)
        print()

        # Store the light curves in the database
        print(">> Storing the light curves in the database...")

        # Drain queue, compute stats, optionally write CSV
        items = [queue.get() for _ in range(queue.qsize())]
        produced = 0
        skipped_min_images = 0
        skipped_min_cstars = 0
        skipped_other = 0
        skipped_rows = []

        for index, result in enumerate(items):
            if isinstance(result, tuple) and len(result) == 3:
                star_id, curve, meta = result
            else:
                star_id, curve = result
                meta = {}

            if curve is None:
                reason = meta.get("reason")
                if reason == "min_images":
                    skipped_min_images += 1
                    logging.info("Star %d skipped: only %d images (min %d).",
                                 star_id, meta.get("n_images"), meta.get("min_required"))
                    skipped_rows.append((star_id, "min_images",
                                         meta.get("n_images"), meta.get("min_required")))
                elif reason == "min_cstars":
                    skipped_min_cstars += 1
                    logging.info("Star %d skipped: only %d complete comparison stars (min %d).",
                                 star_id, meta.get("complete_cstars"), meta.get("min_required"))
                    skipped_rows.append((star_id, "min_cstars",
                                         meta.get("complete_cstars"), meta.get("min_required")))
                else:
                    skipped_other += 1
                    logging.info("Star %d skipped: light curve not generated (unspecified).", star_id)
                    skipped_rows.append((star_id, "other", "", ""))
                continue

            logging.debug("Storing light curve for star %d in database" % star_id)
            db.add_light_curve(star_id, curve)
            logging.debug("Light curve for star %d successfully stored" % star_id)
            produced += 1
            show_progress(100 * (index + 1) / len(all_stars))
            if logging_level < logging.WARNING:
                print()
        else:
            total = len(items)
            logging.info("Summary for filter %s: total stars=%d, curves=%d, skipped[min_images]=%d, skipped[min_cstars]=%d, skipped[other]=%d",
                         pfilter, total, produced, skipped_min_images, skipped_min_cstars, skipped_other)
            if produced == 0:
                logging.warning("No light curves were generated for filter %s. Consider lowering thresholds, e.g.: --minimum-images 5 --minimum-stars 3", pfilter)
            if options.debug_dump_skips and total > 0:
                import csv, os
                out_csv = f"{os.path.abspath(options.output_db_path)}.{pfilter}.skips.csv"
                with open(out_csv, "w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["star_id", "reason", "observed_or_complete", "min_required"])
                    w.writerows(skipped_rows)
                logging.info("Wrote skip details to %s", out_csv)
            logging.info("Light curves for %s generated" % pfilter)
            logging.debug("Committing database transaction")
            db.commit()
            logging.info("Database transaction commited")
            show_progress(100.0)
            print()

        # Update statistics about tables and indexes
        print(">> Updating statistics about tables and indexes...", end=' ')
        sys.stdout.flush()
        try:
            db.analyze()
        except sqlite3.OperationalError as e:
            logging.warning("ANALYZE skipped (%s)", e)
        print("done.")

        # Update LEMONdB metadata
        db.date = time.time()
        db.author = pwd.getpwuid(os.getuid())[0]
        db.hostname = socket.gethostname()
        db.commit()

    owner_writable(output_db_path, False)  # chmod u-w
    print(">> You're done ^_^")
    return 0


if __name__ == "__main__":
    import sys as _sys
    _sys.exit(main(_sys.argv[1:]))