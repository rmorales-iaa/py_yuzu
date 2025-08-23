#! /usr/bin/env python3
# Copyright (c) 2012 Victor Terron. All rights
# reserved. Institute of Astrophysics of Andalusia, IAA-CSIC
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

description = """
Use the algorithm described in (Broeg et al. 2005) for computing an optimal
artificial comparison star and generate the light curve of each astronomical
object in the different photometric filters. The main difference with respect
to the approach described in the paper is that the initial weights are given by
the median of the instrumental magnitudes of each star (so that we rely more on
the brighter ones, as they have a higher signal-to-noise ratio), instead of by
the instrumental errors derived from the characteristics of the CCD detector.

(Broeg et al. 2005) http://adsabs.harvard.edu/abs/2005AN....326..134B
"""

import copy
import logging
import os
import multiprocessing
import pwd
import numpy
import random
import shutil
import socket
import sys
import time
import argparse

# LEMON modules
import util
import customparser
import database
import defaults
import snr
import style
from util.io import owner_writable
from util.display import show_progress


def percentage_change(old, new):
    """Relative change between old and new."""
    if old < 0 and (new > 0 or old < new < 0):
        return (new - old) / float(abs(old))
    else:
        return (new - old) / float(old)


class Weights(numpy.ndarray):
    """Encapsulate weights associated with some values."""

    def __new__(cls, coefficients, dtype=numpy.longdouble):
        if not len(coefficients):
            raise ValueError("arg is an empty sequence")
        w = numpy.asarray(coefficients, dtype=dtype).view(cls)
        w.values = numpy.array(coefficients)
        return w

    def __str__(self):
        coeffs_str = ", ".join(["%s" % x for x in self])
        return "%s(%s)" % (self.__class__.__name__, coeffs_str)

    def rescale(self, key):
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

    def absolute_percent_change(self, other, minimum=None):
        if minimum is not None and minimum <= 0:
            raise ValueError("'minimum' must be a positive number")
        if len(self) != len(other):
            raise ValueError("both instances must be of the same size")

        coefficients = []
        for x, y in zip(self, other):
            if x and (not minimum or min(x, y) >= minimum):
                coefficients.append((x, y))

        if not coefficients:
            msg = "all coefficients were discarded ('minimum' too high?)"
            raise ValueError(msg, self)
        else:
            return max((abs(percentage_change(x, y)) for x, y in coefficients))

    @classmethod
    def random(cls, size):
        weights = cls([random.uniform(0, 1) for _ in range(size)])
        return weights.normalize()


class StarSet(object):
    """A collection of DBStars *with information for the exact same images*,
    all of them also taken in the same filter. Internally all photometric
    information is kept in a 3D (star, data, image) array."""

    def _add(self, index, star):
        if not len(star):
            raise ValueError("star cannot be empty")

        if not len(self.star_ids):
            self._star_ids.append(star.id)
            self.pfilter = star.pfilter
            self._unix_times = star._unix_times

            self._times_indexes = {}
            for time_index, unix_time in enumerate(self._unix_times):
                self._times_indexes[unix_time] = time_index

            self._phot_info[index] = star._phot_info[1:]
        else:
            if star.pfilter != self.pfilter:
                msg = "star with ID = %d has filter '%s', expected '%s'"
                raise ValueError(msg % (star.id, star.pfilter, self.pfilter))

            if star.id in self.star_ids:
                raise ValueError("star with ID = %d already in the set" % star.id)

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

        pogsonr = 100 ** 0.2
        return Weights.inversely_proportional(pogsonr ** mag_medians)

    def light_curve(self, weights, star, no_snr=False, _exclude_index=None):
        if len(weights) != len(self):
            raise ValueError("number of weights must match that of comparison stars")
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

        cstdevs = rweights.values
        args = self.pfilter, self.star_ids, rweights, cstdevs
        curve = database.LightCurve(*args, dtype=self.dtype)

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

    def broeg_weights(self, pct=0.01, max_iters=None, minimum=None):
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
            if weights[-2].absolute_percent_change(weights[-1], minimum=minimum) < pct:
                break

        return weights[-1]

    def worst(self, fraction, pct=0.01, max_iters=None, minimum=None):
        if not 0 < fraction <= 1:
            raise ValueError("'fraction' must be in the range (0,1]")
        if len(self) < 3:
            raise ValueError("at least three stars are needed")

        nstars = int(round(fraction * len(self))) or 1
        kwargs = dict(pct=pct, max_iters=max_iters, minimum=minimum)
        bweights = self.broeg_weights(**kwargs)
        return list(bweights.argsort()[:nstars])

    def best(self, n, fraction=0.1, pct=0.01, max_iters=None, minimum=None):
        if not 0 < fraction <= 1:
            raise ValueError("'fraction' must be in the range (0,1]")
        if len(self) < 3:
            raise ValueError("at least three stars are needed")
        if not 1 <= n <= len(self):
            raise ValueError("must ask for at least one star, at most the size of the set")

        set_ = copy.deepcopy(self)
        kwargs = dict(pct=pct, max_iters=max_iters, minimum=minimum)
        while len(set_) > max(n, 3):
            worst_indexes = set_.worst(fraction, **kwargs)
            del worst_indexes[(len(set_) - max(n, 3)) :]
            for index in sorted(worst_indexes, reverse=True):
                del set_[index]

        if len(set_) != n:
            worst_indexes = set_.worst(1.0, pct=pct, max_iters=max_iters)
            del worst_indexes[(len(set_) - n) :]
            for index in sorted(worst_indexes, reverse=True):
                del set_[index]

        assert len(set_) == n
        return set_


# ---------- helpers to allow partial time overlap and trim to common epochs ----------

def _trim_star_to_times(star, times_sorted):
    """Create a new DBStar that contains only the rows for `times_sorted`."""
    dtype = getattr(star, "dtype", numpy.longdouble)
    n = len(times_sorted)
    if n == 0:
        return None

    phot = numpy.empty((3, n), dtype=dtype)
    t_index = {}
    for i, ut in enumerate(times_sorted):
        try:
            idx = star._time_indexes[ut]
        except KeyError:
            return None
        phot[0, i] = ut
        phot[1, i] = star.mag(idx)
        phot[2, i] = star.snr(idx)
        t_index[ut] = i

    return database.DBStar(star.id, star.pfilter, phot, t_index, dtype)


def _simple_weighted_curve(trimmed_star, trimmed_comps):
    """
    Build a LightCurve using a simple inverse-std weighting of comparison stars.
    This is a robust fallback when Broeg's method cannot be applied (e.g., <3 comps).
    """
    nt = len(trimmed_star)
    if nt < 1:
        return None

    # Stack magnitudes & SNRs: shape (ncomp, nt)
    comp_mags = numpy.array([[c.mag(i) for i in range(nt)] for c in trimmed_comps], dtype=trimmed_star.dtype)
    comp_snrs = numpy.array([[c.snr(i) for i in range(nt)] for c in trimmed_comps], dtype=trimmed_star.dtype)

    # Compute per-star stddev across time; avoid zeros
    stds = comp_mags.std(axis=1)
    stds = numpy.where(stds == 0, numpy.nan, stds)
    inv = 1.0 / stds
    inv = numpy.where(numpy.isfinite(inv), inv, 1.0)  # replace NaN/inf with 1
    weights = inv / inv.sum()

    # cstdevs to store alongside weights (fallback: use stds with NaN->0)
    cstdevs = numpy.where(numpy.isfinite(stds), stds, 0.0)

    # Build LightCurve
    pfilter = trimmed_star.pfilter
    cstars = [c.id for c in trimmed_comps]
    curve = database.LightCurve(pfilter, cstars, weights, cstdevs, dtype=trimmed_star.dtype)

    # Per-epoch weighted comp magnitude, then differential
    smags = numpy.array([trimmed_star.mag(i) for i in range(nt)], dtype=trimmed_star.dtype)
    ssnrs = numpy.array([trimmed_star.snr(i) for i in range(nt)], dtype=trimmed_star.dtype)
    cmag_t = numpy.average(comp_mags, axis=0, weights=weights)

    # SNR: approximate using mean_snr over comps and difference_snr with star
    for i in range(nt):
        csnr = snr.mean_snr(comp_snrs[:, i], weights=weights)
        dsnr = snr.difference_snr(ssnrs[i], csnr)
        curve.add(trimmed_star.time(i), smags[i] - cmag_t[i], dsnr)

    return curve


# ----------------------- PARALLEL WORKER STATE (PER PROCESS) -----------------------

# These globals live only inside worker processes after _pool_init has run.
_WORK_STARS = None          # list[DBStar]
_WORK_TIMESETS = None       # list[set(unix_time)]
_WORK_OPTIONS = None        # argparse.Namespace

def _pool_init(stars, timesets, options):
    """Initializer for worker processes: install read-only data once per worker."""
    global _WORK_STARS, _WORK_TIMESETS, _WORK_OPTIONS
    _WORK_STARS = stars
    _WORK_TIMESETS = timesets
    _WORK_OPTIONS = options


def _select_comparisons_intersection_fast(idx):
    """Fast comparator selection using precomputed time sets in worker globals.

    Returns (trimmed_star, trimmed_comps) or (None, None)
    """
    star = _WORK_STARS[idx]
    base_times = _WORK_TIMESETS[idx]
    min_images = _WORK_OPTIONS.min_images
    max_cstars = _WORK_OPTIONS.ncstars

    # Compute overlaps with all other candidates (same filter in this batch)
    overlaps = []
    for j, cand in enumerate(_WORK_STARS):
        if j == idx:
            continue
        # same filter guaranteed by caller
        other_times = _WORK_TIMESETS[j]
        inter_size = len(base_times & other_times)
        if inter_size >= min_images:
            overlaps.append((j, inter_size))

    if not overlaps:
        return None, None

    # Sort by decreasing overlap size
    overlaps.sort(key=lambda t: t[1], reverse=True)

    # Try with up to max_cstars best candidates, reduce until intersection >= min_images
    max_try = min(len(overlaps), max_cstars)
    for k in range(max_try, 0, -1):
        common = set(base_times)
        for j, _ in overlaps[:k]:
            common &= _WORK_TIMESETS[j]
            if len(common) < min_images:
                break
        if len(common) >= min_images:
            times_sorted = sorted(common)
            trimmed_star = _trim_star_to_times(star, times_sorted)
            if trimmed_star is None:
                continue
            trimmed_comps = []
            ok = True
            for j, _ in overlaps[:k]:
                tc = _trim_star_to_times(_WORK_STARS[j], times_sorted)
                if tc is None or len(tc) != len(trimmed_star):
                    ok = False
                    break
                trimmed_comps.append(tc)
            if ok and trimmed_comps:
                return trimmed_star, trimmed_comps

    return None, None


def _compute_light_curve_for_star_idx(idx):
    """Worker: compute light curve for star index `idx`. Returns (star_id, curve_or_None)."""
    star = _WORK_STARS[idx]
    if len(star) < _WORK_OPTIONS.min_images:
        logging.debug("Star %d: ignored (minimum of %d images not met)",
                      star.id, _WORK_OPTIONS.min_images)
        return (star.id, None)

    trimmed_star, trimmed_comps = _select_comparisons_intersection_fast(idx)
    if trimmed_star is None or not trimmed_comps:
        logging.debug("Star %d: no feasible comparison set after time intersection", star.id)
        return (star.id, None)

    # If we have at least 3 comparison stars, try Broeg's algorithm
    if len(trimmed_comps) >= max(3, _WORK_OPTIONS.min_cstars):
        try:
            ncstars = min(len(trimmed_comps), _WORK_OPTIONS.ncstars)
            comparison_stars = StarSet(trimmed_comps)
            best_set = comparison_stars.best(
                ncstars,
                fraction=_WORK_OPTIONS.worst_fraction,
                pct=_WORK_OPTIONS.pct,
                minimum=_WORK_OPTIONS.wminimum,
                max_iters=_WORK_OPTIONS.max_iters,
            )
            cweights = best_set.broeg_weights(
                pct=_WORK_OPTIONS.pct,
                minimum=_WORK_OPTIONS.wminimum,
                max_iters=_WORK_OPTIONS.max_iters,
            )
            light_curve = best_set.light_curve(cweights, trimmed_star)
            logging.debug("Star %d: light curve generated (stdev=%.4f)", star.id, light_curve.stdev)
            return (star.id, light_curve)
        except Exception as e:
            logging.debug("Star %d: Broeg path failed (%s); falling back to simple weighting", star.id, e)

    # Fallback: simple inverse-std weighting
    try:
        light_curve = _simple_weighted_curve(trimmed_star, trimmed_comps)
        if light_curve is None:
            return (star.id, None)
        logging.debug("Star %d: light curve generated via fallback", star.id)
        return (star.id, light_curve)
    except Exception as e:
        logging.debug("Star %d: fallback failed (%s)", star.id, e)
        return (star.id, None)


# -------- argparse-based CLI --------

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_DB OUTPUT_DB"

# global options
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

# argument groups
curves_group = parser.add_argument_group("Light Curves", "")
curves_group.add_argument(
    "--minimum-images",
    action="store",
    type=int,
    dest="min_images",
    default=10,
    help=("minimum number of images in which a star must have been observed; "
          "otherwise its light curve for that filter is skipped "
          "[default: %(default)s]"),
)
curves_group.add_argument(
    "--stars",
    action="store",
    type=int,
    dest="ncstars",
    default=20,
    help=("number of complete stars to use as artificial comparison "
          "star [default: %(default)s]"),
)
curves_group.add_argument(
    "--minimum-stars",
    action="store",
    type=int,
    dest="min_cstars",
    default=8,
    help=("minimum number of comparison stars required; if fewer are available, "
          "the Broeg method is skipped (a fallback will be used) [default: %(default)s]"),
)

broeg_group = parser.add_argument_group("Broeg's Algorithm", "")
broeg_group.add_argument(
    "--pct",
    action="store",
    type=float,
    dest="pct",
    default=0.01,
    help=("convergence threshold (percentage change) between last two weights "
          "[default: %(default)s]"),
)
broeg_group.add_argument(
    "--weights-threshold",
    action="store",
    type=float,
    dest="wminimum",
    default=0.0001,
    help=("minimum coefficient value considered when computing percentage change "
          "between Weights [default: %(default)s]"),
)
broeg_group.add_argument(
    "--max-iters",
    action="store",
    type=int,
    dest="max_iters",
    default=sys.getrecursionlimit(),
    help=("maximum iterations for Broeg algorithm "
          "[default: %(default)s]"),
)

best_group = parser.add_argument_group("Worst and Best Stars", "")
best_group.add_argument(
    "--worst-fraction",
    action="store",
    type=float,
    dest="worst_fraction",
    default=0.10,
    help=("fraction of stars discarded at each step when identifying the most "
          "constant stars; lower is more reliable, but more CPU-expensive "
          "[default: %(default)s]"),
)

# positional arguments
parser.add_argument("input_db_path", metavar="INPUT_DB")
parser.add_argument("output_db_path", metavar="OUTPUT_DB")

customparser.clear_metavars(parser)


def main(arguments=None):
    """Encapsulated main()."""
    if arguments is None:
        arguments = sys.argv[1:]
    options = parser.parse_args(args=arguments)

    # Logger level
    logging_level = logging.WARNING
    if options.verbose == 1:
        logging_level = logging.INFO
    elif options.verbose >= 2:
        logging_level = logging.DEBUG
    logging.basicConfig(format=style.LOG_FORMAT, level=logging_level)

    input_db_path = options.input_db_path
    output_db_path = options.output_db_path

    if not os.path.exists(input_db_path):
        print("%sError. Database '%s' does not exist." % (style.prefix, input_db_path))
        print(style.error_exit_message)
        return 1

    # Ensure output dir exists
    out_dir = os.path.dirname(output_db_path) or "."
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        print("%sError. Cannot create output directory '%s': %s" % (style.prefix, out_dir, e))
        print(style.error_exit_message)
        return 1

    if os.path.exists(output_db_path):
        if not options.overwrite:
            print("%sError. The output database '%s' already exists." % (
                style.prefix, output_db_path
            ))
            print(style.error_exit_message)
            return 1
        else:
            os.unlink(output_db_path)

    # Work on a copy of the input DB
    print("%sMaking a copy of the input database..." % style.prefix, end="")
    sys.stdout.flush()
    shutil.copy2(input_db_path, output_db_path)
    owner_writable(output_db_path, True)
    print("done.")

    with database.LEMONdB(output_db_path) as db:
        nstars = len(db)
        print("%sThere are %d stars in the database" % (style.prefix, nstars))

        # db.pfilters is a property returning Passband objects
        pfilters = list(db.pfilters)

        for pfilter in sorted(pfilters):
            print(style.prefix)
            print("%sLight curves for the %s filter will now be generated." % (
                style.prefix, pfilter
            ))
            print("%sLoading photometric information..." % style.prefix, end="")
            sys.stdout.flush()
            all_stars = [db.get_photometry(star_id, pfilter) for star_id in db.star_ids]
            print("done.")

            # Precompute time sets once (huge speedup) and keep data in workers via initializer.
            timesets = [set(s._time_indexes.keys()) for s in all_stars]

            # Generate & store light curves in parallel, using per-process globals to avoid
            # re-pickling the whole `all_stars` list for each task.
            print("%sGenerating and storing light curves..." % style.prefix)
            total = len(all_stars)
            stored = 0
            processed = 0
            show_progress(0.0)

            # Choose a larger chunksize to reduce IPC overhead for many stars
            chunksize = max(1, total // (options.ncores * 8) or 1)

            with multiprocessing.Pool(
                processes=options.ncores,
                initializer=_pool_init,
                initargs=(all_stars, timesets, options),
            ) as pool:
                for star_id, curve in pool.imap_unordered(_compute_light_curve_for_star_idx, range(total), chunksize=chunksize):
                    processed += 1
                    if curve is None:
                        logging.debug("Nothing for star %d; light curve not generated", star_id)
                    else:
                        db.add_light_curve(star_id, curve)
                        stored += 1

                    show_progress(100.0 * processed / max(1, total))
                    if logging_level < logging.WARNING:
                        print()

            print("%sStored %d/%d light curves for %s" % (style.prefix, stored, total, pfilter))
            print("%sCommitting database transaction" % style.prefix)
            db.commit()
            print("%sDatabase transaction committed" % style.prefix)

            show_progress(100.0)
            print()

        print("%sUpdating statistics about tables and indexes..." % style.prefix, end="")
        sys.stdout.flush()
        db.analyze()
        print("done.")

        # Update metadata
        db.date = time.time()
        db.author = pwd.getpwuid(os.getuid())[0]
        db.hostname = socket.gethostname()
        db.commit()

    owner_writable(output_db_path, False)
    print("%sYou're done ^_^" % style.prefix)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
