#!/usr/bin/env python3
from __future__ import annotations, division

from pathlib import Path

# Copyright (c) 2012 Victor Terron.
# All rights reserved.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

description = """
Use the algorithm described in (Broeg et al. 2005) for computing an optimal
artificial comparison star and generate the light curve of each astronomical
object in the different photometric filters. The main difference with respect
to the approach described in the paper is that the initial weights are given by
the median of the instrumental magnitudes of each star (so that we rely more on
the brighter ones, as they have a higher signal-to-noise ratio), instead of by
the instrumental errors derived from the characteristics of the CCD detector.

(Broeg et al. 2005) http://adsabs.harvard.edu/abs/2005AN....326..134B

Briefly, the approach followed to compute each light curve consists in (a)
identifying the most constant stars in the field, (b) computing an artificial
comparison star by taking the weighted mean of the instrumental magnitudes of
these constant stars, using weights inversely proportional to their standard
deviations and, lastly, (c) comparing the instrumental magnitude of our star to
that of the comparison star. In this last step we subtract the magnitude of the
star from that of the comparison star: in this manner, positive values mean
that the former is brighter than the latter.
"""

import argparse
import copy
import logging
import multiprocessing
import numpy
import os
import pwd
import random
import shutil
import socket
import sys
import time
import warnings

# LEMON modules
import util
import customparser
import database
import defaults
import snr
import style
from util.queue import Queue
from util.display import print_exception_traceback  # noqa: F401
from util.io import owner_writable, clean_tmp_files  # noqa: F401
from util.display import show_progress

class OptionGroup:
    """Compat wrapper that mimics optparse.OptionGroup using argparse groups."""

    def __init__(
        self,
        parser: argparse.ArgumentParser,
        title: str,
        description: str | None = None,
    ):
        self._group = parser.add_argument_group(title=title, description=description)

    def add_option(self, *opts, **kwargs):
        """Accept optparse-like kwargs and forward to argparse."""
        return self._group.add_argument(*opts, **kwargs)

    # Alias for convenience
    def add_argument(self, *args, **kwargs):
        return self.add_option(*args, **kwargs)


def percentage_change(old: float, new: float) -> float:
    """Relative change (handles negatives sensibly)."""
    if old < 0 and (new > 0 or old < new < 0):
        return (new - old) / float(abs(old))
    return (new - old) / float(old)


class Weights(numpy.ndarray):
    """Encapsulate the weights associated with some values."""

    def __new__(cls, coefficients, dtype=numpy.longdouble):
        if not len(coefficients):
            raise ValueError("arg is an empty sequence")
        w = numpy.asarray(coefficients, dtype=dtype).view(cls)
        w.values = numpy.array(coefficients)
        return w

    def __str__(self):
        coeffs_str = ", ".join(f"{x}" for x in self)
        return f"{self.__class__.__name__}({coeffs_str})"

    def rescale(self, key: int):
        """Exclude key-th coefficient and return rescaled Weights."""
        if len(self) == 1:
            raise ValueError("cannot rescale one-element instance")
        w = Weights(numpy.delete(self, key)).normalize()
        w.values = numpy.delete(self.values, key)
        return w

    @property
    def total(self):
        return numpy.sum(self)

    def normalize(self):
        w = Weights(self / self.total)
        w.values = self.values
        return w

    @classmethod
    def inversely_proportional(cls, values, dtype=numpy.longdouble):
        if not len(values):
            raise ValueError("'values' is an empty sequence")
        if not all(values):
            raise ValueError("'values' cannot contain zeros")
        values = numpy.array(values, dtype=dtype)
        w = cls(1 / values).normalize()
        w.values = numpy.array(values)
        return w

    def absolute_percent_change(self, other, minimum: float | None = None):
        if minimum is not None and minimum <= 0:
            raise ValueError("'minimum' must be a positive number")
        if len(self) != len(other):
            raise ValueError("both instances must be of the same size")

        coefficients = []
        for x, y in zip(self, other):
            if x and (not minimum or min(x, y) >= minimum):
                coefficients.append((x, y))

        if not coefficients:
            raise ValueError(
                "all coefficients were discarded ('minimum' too high?)", self
            )
        return max(abs(percentage_change(x, y)) for x, y in coefficients)

    @classmethod
    def random(cls, size: int):
        weights = cls([random.uniform(0, 1) for _ in range(size)])
        return weights.normalize()


class StarSet(object):
    """A collection of DBStars *with info for the exact same images* (same filter)."""

    def _add(self, index, star):
        if not len(star):
            raise ValueError("star cannot be empty")

        if not len(self.star_ids):
            self._star_ids.append(star.id)
            self.pfilter = star.pfilter
            self._unix_times = star._unix_times

            # cache: unix_time -> index
            self._times_indexes = {t: i for i, t in enumerate(self._unix_times)}

            # 3D array: [star, datum(0=mag,1=snr), image_index]
            self._phot_info[index] = star._phot_info[1:]
        else:
            if star.pfilter != self.pfilter:
                raise ValueError(
                    "star with ID = %d has filter '%s', expected '%s'"
                    % (star.id, star.pfilter, self.pfilter)
                )
            if star.id in self.star_ids:
                raise ValueError(f"star with ID = {star.id} already in the set")

            for unix_time in self._unix_times:
                if unix_time not in star._time_indexes:
                    raise ValueError("stars must have info for the same Unix times")

            self._star_ids.append(star.id)
            self._phot_info[index] = star._phot_info[1:]

    def __init__(self, stars, dtype=numpy.longdouble):
        if not stars:
            raise ValueError("at least one star is needed")

        self.dtype = dtype
        self._star_ids = []
        self._phot_info = numpy.empty((len(stars), 2, len(stars[0])), dtype=self.dtype)
        for index, star in enumerate(stars):
            self._add(index, star)

    @property
    def star_ids(self):
        return self._star_ids

    def __len__(self):
        return self._phot_info.shape[0]

    @property
    def nimages(self):
        return len(self._unix_times)

    def __delitem__(self, index):
        self._phot_info = numpy.delete(self._phot_info, index, axis=0)
        del self._star_ids[index]
        assert len(self.star_ids) == len(self)

    def __getitem__(self, index):
        sphot_info = numpy.empty((3, self.nimages), dtype=self.dtype)
        sphot_info[0] = self._unix_times
        sphot_info[1] = self._phot_info[index][0]
        sphot_info[2] = self._phot_info[index][1]
        id_ = self._star_ids[index]
        return database.DBStar(
            id_, self.pfilter, sphot_info, self._times_indexes, self.dtype
        )

    def flux_proportional_weights(self):
        norm_mags = numpy.empty((len(self), self.nimages), dtype=self.dtype)
        for image_index in range(self.nimages):
            image_mags = self._phot_info[:, 0, image_index]
            norm_mags[:, image_index] = image_mags / image_mags.max()

        mag_medians = numpy.empty(len(self), dtype=self.dtype)
        for star_index in range(len(self)):
            mag_medians[star_index] = numpy.median(norm_mags[star_index])

        pogsonr = 100 ** 0.2  # 2.511886...
        return Weights.inversely_proportional(pogsonr ** mag_medians)

    def light_curve(self, weights, star, no_snr: bool = False, _exclude_index=None):
        if len(weights) != len(self):
            raise ValueError(
                "number of weights must match that of comparison stars"
            )
        if _exclude_index is not None and not 0 <= _exclude_index < len(self):
            raise IndexError("_exclude_index out of range")
        if not numpy.all(numpy.equal(star._unix_times, self._unix_times)):
            raise ValueError("Unix times of StarSet and DBStar do not match")

        if _exclude_index is None:
            rweights = weights
        else:
            values = numpy.array(weights.values)
            rweights = weights.rescale(_exclude_index)
            rweights = numpy.insert(rweights, _exclude_index, 0.0)
            rweights.values = values

        assert len(rweights) == len(self)
        assert hasattr(rweights, "values")

        cstdevs = rweights.values
        curve = database.LightCurve(
            self.pfilter, self.star_ids, rweights, cstdevs, dtype=self.dtype
        )

        for index, unix_time in enumerate(self._unix_times):
            cmags = self._phot_info[:, 0, index]
            csnrs = self._phot_info[:, 1, index]
            cmag = numpy.average(cmags, weights=rweights)
            csnr = None if no_snr else snr.mean_snr(csnrs, weights=rweights)

            smag = star.mag(index)
            ssnr = star.snr(index)

            dmag = smag - cmag
            dsnr = None if no_snr else snr.difference_snr(ssnr, csnr)
            curve.add(unix_time, dmag, dsnr)

        return curve

    def broeg_weights(
        self, pct: float = 0.01, max_iters: int | None = None, minimum: float | None = None
    ):
        if not len(self):
            raise ValueError("cannot work with an empty instance")
        if len(self) == 1:
            return Weights([1.0])
        if len(self) == 2:
            return Weights([0.5, 0.5])
        if self.nimages < 2:
            raise ValueError("at least two images are needed")

        weights = [self.flux_proportional_weights()]
        curves_stdevs = numpy.empty(len(self), dtype=self.dtype)

        for _ in range(max_iters or sys.getrecursionlimit()):
            for star_index in range(len(self)):
                star_curve = self.light_curve(
                    weights[-1], self[star_index], _exclude_index=star_index, no_snr=True
                )
                curves_stdevs[star_index] = star_curve.stdev

            if not all(curves_stdevs):
                break

            weights.append(Weights.inversely_proportional(curves_stdevs))
            if (
                weights[-2].absolute_percent_change(
                    weights[-1], minimum=minimum
                )
                < pct
            ):
                break

        return weights[-1]

    def worst(
        self,
        fraction: float,
        pct: float = 0.01,
        max_iters: int | None = None,
        minimum: float | None = None,
    ):
        if not 0 < fraction <= 1:
            raise ValueError("'fraction' must be in the range (0,1]")
        if len(self) < 3:
            raise ValueError("at least three stars are needed")

        nstars = int(round(fraction * len(self))) or 1
        bweights = self.broeg_weights(pct=pct, max_iters=max_iters, minimum=minimum)
        return list(bweights.argsort()[:nstars])

    def best(
        self,
        n: int,
        fraction: float = 0.1,
        pct: float = 0.01,
        max_iters: int | None = None,
        minimum: float | None = None,
    ):
        if not 0 < fraction <= 1:
            raise ValueError("'fraction' must be in the range (0,1]")
        if len(self) < 3:
            raise ValueError("at least three stars are needed")
        if not 1 <= n <= len(self):
            raise ValueError("must ask for at least one star, at most the set size")

        set_ = copy.deepcopy(self)

        kwargs = dict(pct=pct, max_iters=max_iters, minimum=minimum)
        while len(set_) > max(n, 3):
            worst_indexes = set_.worst(fraction, **kwargs)
            del worst_indexes[(len(set_) - max(n, 3)) :]
            for index in sorted(worst_indexes, reverse=True):
                del set_[index]

        assert len(set_) >= n
        if len(set_) != n:
            worst_indexes = set_.worst(1.0, pct=pct, max_iters=max_iters)
            del worst_indexes[(len(set_) - n) :]
            for index in sorted(worst_indexes, reverse=True):
                del set_[index]

        assert len(set_) == n
        return set_


# Global, process-shared queue used by workers
queue = Queue()


def parallel_light_curves(args):
    """Worker function for multiprocessing: compute one light curve."""
    star, all_stars, options = args
    logging.debug(
        "Star %d: photometry on %d images, enforced minimum of %d",
        star.id,
        len(star),
        options.min_images,
    )

    if len(star) < options.min_images:
        logging.debug(
            "Star %d: ignored (minimum of %d images not met)",
            star.id,
            options.min_images,
        )
        queue.put((star.id, None))
        return

    complete_for = star.complete_for(all_stars)
    logging.debug(
        "Star %d: %d complete stars, enforced minimum = %d",
        star.id,
        len(complete_for),
        options.min_cstars,
    )

    ncstars = min(len(complete_for), options.ncstars)
    if ncstars < options.min_cstars:
        logging.debug(
            "Star %d: ignored (minimum of %d comparison stars not met)",
            star.id,
            options.min_cstars,
        )
        queue.put((star.id, None))
        return

    logging.debug(
        "Star %d: will use %d complete stars (out of %d) as comparison",
        star.id,
        ncstars,
        len(complete_for),
    )
    logging.debug(
        "Star %d: fraction of worst stars to discard = %.4f",
        star.id,
        options.worst_fraction,
    )
    logging.debug(
        "Star %d: weights percentage change threshold = %.4f",
        star.id,
        options.pct,
    )
    logging.debug(
        "Star %d: minimum Weights coefficients = %.4f",
        star.id,
        options.wminimum,
    )
    logging.debug(
        "Star %d: maximum Broeg iterations: %d",
        star.id,
        options.max_iters,
    )

    complete_stars = StarSet(complete_for)
    comparison_stars = complete_stars.best(
        ncstars,
        fraction=options.worst_fraction,
        pct=options.pct,
        minimum=options.wminimum,
        max_iters=options.max_iters,
    )

    logging.debug(
        "Star %d: identified the %d best stars (out of %d)",
        star.id,
        len(comparison_stars),
        len(complete_stars),
    )
    logging.debug(
        "Star %d: best stars IDs: %s",
        star.id,
        [x.id for x in comparison_stars],
    )

    cweights = comparison_stars.broeg_weights(
        pct=options.pct, minimum=options.wminimum, max_iters=options.max_iters
    )

    logging.debug("Star %d: Broeg weights: %s", star.id, str(cweights))
    light_curve = comparison_stars.light_curve(cweights, star)
    logging.debug(
        "Star %d: light curve sucessfully generated (stdev = %.4f)",
        star.id,
        light_curve.stdev,
    )
    queue.put((star.id, light_curve))


# ----------------- Parser -----------------
parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_DB OUTPUT_DB"

parser.add_argument(
    "--overwrite",
    action="store_true",
    dest="overwrite",
    help="overwrite output database if it already exists",
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

# Positionals
parser.add_argument("input_db", help="input LEMON database")
parser.add_argument("output_db", help="output LEMON database")

# Groups
curves_group = OptionGroup(parser, "Light Curves", "")
curves_group.add_option(
    "--minimum-images",
    action="store",
    type=int,
    dest="min_images",
    default=10,
    help=(
        "minimum number of images a star must have been observed in "
        "[default: %(default)s]"
    ),
)
curves_group.add_option(
    "--stars",
    action="store",
    type=int,
    dest="ncstars",
    default=20,
    help=(
        "number of complete stars to use as artificial comparison "
        "[default: %(default)s]"
    ),
)
curves_group.add_option(
    "--minimum-stars",
    action="store",
    type=int,
    dest="min_cstars",
    default=8,
    help=(
        "minimum number of stars to compute the artificial comparison "
        "[default: %(default)s]"
    ),
)

broeg_group = OptionGroup(parser, "Broeg's Algorithm", "")
broeg_group.add_option(
    "--pct",
    action="store",
    type=float,
    dest="pct",
    default=0.01,
    help=(
        "convergence threshold for weights as percent change "
        "[default: %(default)s]"
    ),
)
broeg_group.add_option(
    "--weights-threshold",
    action="store",
    type=float,
    dest="wminimum",
    default=0.0001,
    help=(
        "minimum coefficient considered when comparing Weights "
        "[default: %(default)s]"
    ),
)
broeg_group.add_option(
    "--max-iters",
    action="store",
    type=int,
    dest="max_iters",
    default=sys.getrecursionlimit(),
    help="maximum iterations of the algorithm [default: %(default)s]",
)

best_group = OptionGroup(parser, "Worst and Best Stars", "")
best_group.add_option(
    "--worst-fraction",
    action="store",
    type=float,
    dest="worst_fraction",
    default=0.10,
    help=(
        "fraction of stars discarded each step when finding best stars "
        "[default: %(default)s]"
    ),
)

customparser.clear_metavars(parser)
logger = logging.getLogger(__name__)


def main(arguments=None):
    """Main entry point."""
    if arguments is None:
        arguments = sys.argv[1:]

    options = parser.parse_args(args=arguments)

    # Logging setup
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    input_db_path = options.input_db
    output_db_path = options.output_db

    if options.min_cstars > options.ncstars:
        print(
            f"{style.prefix}Error. The value of --minimum-stars must be <= --stars."
        )
        print(style.error_exit_message)
        return 1

    if not Path(input_db_path).exists():
        print(f"{style.prefix}Error. Database '{input_db_path}' does not exist.")
        print(style.error_exit_message)
        return 1

    if Path(output_db_path).exists():
        if not options.overwrite:
            print(
                f"{style.prefix}Error. The output database '{output_db_path}' "
                f"already exists."
            )
            print(style.error_exit_message)
            return 1
        os.unlink(output_db_path)

    # Copy DB (work on a writable copy)
    print(f"{style.prefix}Making a copy of the input database...", end="")
    sys.stdout.flush()
    shutil.copy2(input_db_path, output_db_path)
    owner_writable(output_db_path, True)  # chmod u+w
    print(" done.")

    with database.LEMONdB(output_db_path) as db:
        nstars = len(db)
        print(f"{style.prefix}There are {nstars} stars in the database")

        for pfilter in sorted(db.pfilters):
            print(style.prefix)
            print(
                f"{style.prefix}Light curves for the {pfilter} filter will now be generated."
            )
            print(f"{style.prefix}Loading photometric information...", end="")
            sys.stdout.flush()
            all_stars = [db.get_photometry(star_id, pfilter) for star_id in db.star_ids]
            print(" done.")

            # Parallel light-curve computation
            pool = multiprocessing.Pool(options.ncores)
            map_async_args = ((star, all_stars, options) for star in all_stars)
            result = pool.map_async(parallel_light_curves, map_async_args)

            show_progress(0.0)
            while not result.ready():
                time.sleep(1)
                show_progress(queue.qsize() / float(len(all_stars)) * 100.0)
                if logging_level < logging.WARNING:
                    print()
            result.get()  # reraise if any worker failed
            show_progress(100.0)
            print()

            # Store results
            print(f"{style.prefix}Storing the light curves in the database...")
            show_progress(0.0)
            qsize = queue.qsize()
            light_curves = (queue.get() for _ in range(qsize))
            for index, (star_id, curve) in enumerate(light_curves):
                if curve is None:
                    logging.debug(
                        "Nothing for star %d; light curve could not be generated",
                        star_id,
                    )
                    continue
                logging.debug("Storing light curve for star %d in database", star_id)
                db.add_light_curve(star_id, curve)
                logging.debug(
                    "Light curve for star %d successfully stored", star_id
                )

                show_progress(100.0 * (index + 1) / float(len(all_stars)))
                if logging_level < logging.WARNING:
                    print()
            else:
                logging.info("Light curves for %s generated", pfilter)
                logging.debug("Committing database transaction")
                db.commit()
                logging.info("Database transaction committed")
                show_progress(100.0)
                print()

        print(
            f"{style.prefix}Updating statistics about tables and indexes...", end=""
        )
        sys.stdout.flush()
        db.analyze()
        print(" done.")

        # Update metadata
        db.date = time.time()
        db.author = pwd.getpwuid(os.getuid())[0]
        db.hostname = socket.gethostname()
        db.commit()

    owner_writable(output_db_path, False)  # chmod u-w
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
