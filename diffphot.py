#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Differential photometry: build light curves by subtracting, for each star
and image, the weighted ensemble of the best (least-variable) comparison
stars observed in the same photometric filter.

Python 3 port of LEMON's `diffphot.py`, modernized to:
  - Use your provided database facade (SQLAlchemy LEMONSA-compatible).
  - Parallelize per-star computations with threads.
  - ALWAYS batch/parallelize DB writes with threads (no legacy single-thread path).
  - Preserve LEMON-style messages and behavior.

High-level steps:
  1) Copy the input database file to the output path (unless --overwrite).
  2) For each selected photometric filter:
     a) Load per-star photometry time series.
     b) Estimate frame zero-points and per-star residual scatter.
     c) Iteratively discard the worst fraction (highest scatter) to choose
        comparison stars; compute weights ~ 1/stdev^2.
     d) Build differential light curves for all stars.
     e) Store comparison-star sets and light curves in the DB.
"""

from __future__ import annotations

# stdlib
import collections
import concurrent.futures
import logging
import math
import os
import os.path
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from contextlib import contextmanager

# 3rd / project modules (provided by user environment)
import numpy

import customparser
import defaults
import style
import database

from util.display import show_progress
from util.io import owner_writable


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

description = """
Compute differential photometry from an existing LEMON database containing
instrumental magnitudes. For each photometric filter, the program selects
a robust set of non-variable comparison stars and, for every star, subtracts
the weighted ensemble magnitude frame by frame to produce a differential
light curve.
"""

parser = customparser.get_parser(description)
parser.usage = "%(prog)s [OPTION]... INPUT_DB OUTPUT_DB"

# General
parser.add_argument("--overwrite",
                    action="store_true",
                    dest="overwrite",
                    help="overwrite OUTPUT_DB if it already exists")

parser.add_argument("-v", "--verbose",
                    action="count",
                    dest="verbose",
                    default=defaults.verbosity,
                    help=defaults.desc.get("verbosity", ""))

parser.add_argument("--cores",
                    action="store",
                    type=int,
                    dest="ncores",
                    default=defaults.ncores,
                    help=defaults.desc.get("ncores", ""))

parser.add_argument("--db-workers",
                    action="store",
                    type=int,
                    dest="db_workers",
                    default=max(2, min(8, getattr(defaults, "ncores", 4))),
                    help="number of parallel DB writer threads [default: based on cores]")

parser.add_argument("--db-chunk",
                    action="store",
                    type=int,
                    dest="db_chunk",
                    default=32,
                    help="number of stars per DB write chunk (default: %(default)s)")

# Filter selection
filter_group = parser.add_argument_group(
    "Filter images for differential photometry",
    "Select which photometric filters to process."
)
filter_group.add_argument("--filter",
                          action="append",
                          type=str,
                          dest="filters",
                          default=None,
                          help=defaults.desc.get("pfilter", "photometric filter to consider (may be repeated)"))

filter_group.add_argument("--exclude",
                          action="append",
                          type=str,
                          dest="excluded_filters",
                          default=None,
                          help=("ignore these photometric filters. Opposite of --filter; "
                                "may be given multiple times."))

# Differential-photometry algorithm
algo = parser.add_argument_group("Algorithm",
                                 "Tune the selection of comparison stars and light-curve build.")
algo.add_argument("--worst-fraction",
                  action="store",
                  type=float,
                  dest="worst_fraction",
                  default=0.20,
                  help="fraction of the stars (highest scatter) to discard at each iteration [default: %(default).2f]")

algo.add_argument("--iterations",
                  action="store",
                  type=int,
                  dest="iterations",
                  default=3,
                  help="number of discard iterations [default: %(default)s]")

algo.add_argument("--min-cstars",
                  action="store",
                  type=int,
                  dest="min_cstars",
                  default=12,
                  help="minimum number of comparison stars to keep [default: %(default)s]")

algo.add_argument("--min-points",
                  action="store",
                  type=int,
                  dest="min_points",
                  default=5,
                  help="minimum number of valid points per star to be considered [default: %(default)s]")

algo.add_argument("--snr-cut",
                  action="store",
                  type=float,
                  dest="snr_cut",
                  default=1.0,
                  help="ignore measurements with SNR below this threshold [default: %(default).1f]")

customparser.clear_metavars(parser)


# -----------------------------------------------------------------------------
# Helpers for DB open
# -----------------------------------------------------------------------------
@contextmanager
def _open_db(path: str):
    """
    Open the database, supporting either `database.LEMONdB` (classic name)
    or `database.LEMONSA` (SQLAlchemy port). Closes cleanly on exit.
    """
    DBClass = getattr(database, "LEMONdB", None) or getattr(database, "LEMONSA", None)
    if DBClass is None:
        raise RuntimeError("No DB class found (expected LEMONdB or LEMONSA)")

    db = DBClass(path)
    try:
        yield db
    finally:
        if hasattr(db, "close_all"):
            db.close_all()
        elif hasattr(db, "close"):
            db.close()

# -----------------------------------------------------------------------------
# Types expected from DB facade
# -----------------------------------------------------------------------------

LightCurve = getattr(database, "LightCurve")
LightCurveItem = getattr(database, "LightCurveItem")


# -----------------------------------------------------------------------------
# Core math
# -----------------------------------------------------------------------------

def _median_ignore_nan(arr: Sequence[float]) -> float:
    vals = [x for x in arr if x is not None and not numpy.isnan(x)]
    return float(numpy.median(vals)) if vals else float("nan")


def _stdev_ignore_nan(arr: Sequence[float]) -> float:
    vals = [x for x in arr if x is not None and not numpy.isnan(x)]
    if len(vals) < 2:
        return float("inf")
    return float(numpy.std(vals, ddof=1))


def _weighted_mean(values: Sequence[float], weights: Sequence[float]) -> Optional[float]:
    vs, ws = [], []
    for v, w in zip(values, weights):
        if v is None or numpy.isnan(v) or w <= 0:
            continue
        vs.append(v)
        ws.append(w)
    if not vs or not ws:
        return None
    return float(numpy.average(vs, weights=ws))


def _collect_times(star_series: Dict[int, "DBStar"]) -> List[float]:
    ts = set()
    for st in star_series.values():
        for i in range(st.n):
            t = st.time(i)
            if t is None:
                continue
            ts.add(float(t))
    return sorted(ts)


def _build_zero_points(times: List[float],
                       star_series: Dict[int, "DBStar"],
                       snr_cut: float) -> Dict[float, float]:
    zps: Dict[float, float] = {}
    for t in times:
        mags = []
        for st in star_series.values():
            idx = st.idx_for_time(t)
            if idx is None or idx < 0:
                continue
            s = st.snr(idx)
            if (s is not None) and (s < snr_cut):
                continue
            mags.append(st.mag(idx))
        zps[t] = _median_ignore_nan(mags)
    return zps


def _per_star_residual_stdev(star_series: Dict[int, "DBStar"],
                             zps: Dict[float, float],
                             snr_cut: float,
                             min_points: int) -> Dict[int, float]:
    out: Dict[int, float] = {}
    for sid, st in star_series.items():
        resids = []
        for i in range(st.n):
            m = st.mag(i)
            t = st.time(i)
            if t is None:
                continue
            zp = zps.get(float(t))
            if zp is None or numpy.isnan(zp):
                continue
            s = st.snr(i)
            if (s is not None) and (s < snr_cut):
                continue
            if m is None or numpy.isnan(m):
                continue
            resids.append(m - zp)
        if len(resids) >= max(2, min_points):
            out[sid] = _stdev_ignore_nan(resids)
        else:
            out[sid] = float("inf")
    return out


def _select_comparison_stars(stdevs: Dict[int, float],
                             worst_fraction: float,
                             iterations: int,
                             min_cstars: int) -> List[int]:
    pool = [sid for sid, sd in stdevs.items() if math.isfinite(sd)]
    if not pool:
        return []
    pool.sort(key=lambda sid: stdevs[sid])  # best first

    for _ in range(max(0, int(iterations))):
        if len(pool) <= max(1, int(min_cstars)):
            break
        kdrop = max(1, int(math.floor(len(pool) * float(worst_fraction))))
        del pool[-kdrop:]  # drop worst
        if len(pool) <= max(1, int(min_cstars)):
            break

    if len(pool) > min_cstars:
        pool = pool[:min_cstars]
    return pool


def _weights_from_stdev(stdevs: Dict[int, float], cstars: List[int]) -> List[float]:
    eps = 1e-8
    vals = []
    for sid in cstars:
        sd = float(stdevs.get(sid, float("inf")))
        w = 1.0 / max(sd * sd, eps)
        vals.append(w)
    s = sum(vals) or 1.0
    return [w / s for w in vals]


def _build_light_curve_for_star(star_id: int,
                                pfilter: str,
                                star_series: Dict[int, "DBStar"],
                                cstars: List[int],
                                cweights: List[float],
                                snr_cut: float) -> Optional["LightCurve"]:
    target = star_series.get(star_id)
    if target is None:
        return None

    curve = LightCurve(pfilter)
    curve.cstars = list(cstars)
    curve.cweights = list(cweights)
    curve.cstdevs = []  # filled by caller

    for i in range(target.n):
        t = target.time(i)
        m = target.mag(i)
        s = target.snr(i)
        if t is None or m is None or numpy.isnan(m):
            continue
        if (s is not None) and (s < snr_cut):
            continue

        present_weights = []
        present_mags = []
        for sid, w in zip(cstars, cweights):
            st = star_series.get(sid)
            if st is None:
                continue
            idx = st.idx_for_time(t)
            if idx is None or idx < 0:
                continue
            cm = st.mag(idx)
            if cm is None or numpy.isnan(cm):
                continue
            present_mags.append(cm)
            present_weights.append(w)

        comp_mean = _weighted_mean(present_mags, present_weights)
        if comp_mean is None or numpy.isnan(comp_mean):
            continue

        diff_mag = float(m) - float(comp_mean)
        curve.append(LightCurveItem(float(t), diff_mag, None if s is None else float(s)))

    return curve if len(curve) > 0 else None


# -----------------------------------------------------------------------------
# main
# -----------------------------------------------------------------------------

def main(arguments=None):
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

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------
    if len(args) < 2:
        print("Usage:")
        print(parser.format_usage())
        return 2

    input_db_path = args[0]
    output_db_path = args[-1]

    if not Path(input_db_path).exists():
        print(f"{style.prefix}Error. Input database file not found: {input_db_path}")
        print(style.error_exit_message)
        return 1

    # Prepare output DB file
    if Path(output_db_path).exists():
        if options.overwrite:
            try:
                os.remove(output_db_path)
            except OSError:
                pass
        else:
            print(f"{style.prefix}Error. Output database already exists: {output_db_path}")
            print(style.error_exit_message)
            return 1

    print(f"{style.prefix}Making a copy of the input database...", end=" ")
    sys.stdout.flush()
    shutil.copy2(input_db_path, output_db_path)
    print("done.")

    # ------------------------------------------------------------------
    # Open DB
    # ------------------------------------------------------------------
    with _open_db(output_db_path) as db:

        # Determine filters to process
        pfilters = list(getattr(db, "pfilters", []))
        if not pfilters:
            print(f"{style.prefix}Error. No photometric filters found in the database.")
            print(style.error_exit_message)
            return 1

        if options.filters:
            selected = set(options.filters)
            pfilters = [pf for pf in pfilters if pf in selected]
        if options.excluded_filters:
            excluded = set(options.excluded_filters)
            pfilters = [pf for pf in pfilters if pf not in excluded]

        if not pfilters:
            print(f"{style.prefix}Error. No photometric filters to process after applying include/exclude.")
            print(style.error_exit_message)
            return 1

        print(f"{style.prefix}{len(pfilters)} photometric filter(s) will be processed: {', '.join(sorted(pfilters))}")

        # Star list
        star_ids: List[int] = list(getattr(db, "star_ids", []))
        if not star_ids:
            print(f"{style.prefix}Error. No stars found in the database.")
            print(style.error_exit_message)
            return 1

        # ------------------------------------------------------------------
        # Process each filter independently
        # ------------------------------------------------------------------
        for pf in sorted(pfilters):
            print(style.prefix)
            print(f"{style.prefix}Working on the '{pf}' filter.")
            sys.stdout.flush()

            # 1) Load per-star photometry for this filter
            print(f"{style.prefix}Loading photometry for {len(star_ids)} stars...", end=" ")
            sys.stdout.flush()
            star_series: Dict[int, "DBStar"] = {}
            for sid in star_ids:
                try:
                    st = db.get_photometry(int(sid), str(pf))
                except Exception as e:
                    logging.debug("get_photometry failed for star %r filter %s: %s", sid, pf, e)
                    st = None
                if st is not None and getattr(st, "n", 0) >= options.min_points:
                    star_series[int(sid)] = st
            print(f"done. ({len(star_series)} usable)")
            if not star_series:
                print(f"{style.prefix}Warning: no usable stars for {pf}. Skipping.")
                continue

            # 2) Build per-frame zero points and per-star scatter
            print(f"{style.prefix}Estimating per-frame zero-points (median across stars)...", end=" ")
            sys.stdout.flush()
            times = _collect_times(star_series)
            zps = _build_zero_points(times, star_series, float(options.snr_cut))
            print("done.")

            print(f"{style.prefix}Measuring residual scatter per star...", end=" ")
            sys.stdout.flush()
            stdevs = _per_star_residual_stdev(star_series, zps, float(options.snr_cut), int(options.min_points))
            print("done.")

            # 3) Select comparison stars (one ensemble for the filter)
            print(f"{style.prefix}Selecting comparison stars (iteratively discarding worst fraction = {options.worst_fraction:.2f})...", end=" ")
            sys.stdout.flush()
            cstars = _select_comparison_stars(stdevs,
                                              float(options.worst_fraction),
                                              int(options.iterations),
                                              int(options.min_cstars))
            if not cstars:
                print("\n" + f"{style.prefix}Error. Failed to select comparison stars for filter '{pf}'.")
                print(style.error_exit_message)
                return 1
            cweights = _weights_from_stdev(stdevs, cstars)
            cstdevs = [float(stdevs[sid]) for sid in cstars]
            print(f"done. ({len(cstars)} stars)")

            # 4) Build differential light curves for *all* stars (parallel compute)
            print(f"{style.prefix}Building differential light curves...", end=" ")
            sys.stdout.flush()

            def _worker(sid: int) -> Tuple[int, Optional["LightCurve"]]:
                curve = _build_light_curve_for_star(sid, str(pf), star_series,
                                                    cstars=cstars, cweights=cweights,
                                                    snr_cut=float(options.snr_cut))
                if curve is not None:
                    curve.cstdevs = list(cstdevs)
                return (sid, curve)

            curves: Dict[int, "LightCurve"] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(options.ncores))) as ex:
                for sid, curve in ex.map(_worker, star_series.keys()):
                    if curve is not None and len(curve) > 0:
                        curves[int(sid)] = curve

            print(f"done. ({len(curves)} light curves)")

            # 5) Store in DB ? ALWAYS parallel with threads
            print(f"{style.prefix}Storing comparison sets and light curves in the database...")
            sys.stdout.flush()

            curve_items = list(curves.items())
            chunk = max(1, int(options.db_chunk))
            chunks = [curve_items[i:i + chunk] for i in range(0, len(curve_items), chunk)]
            total = len(chunks)
            show_progress(0.0)

            def _store_chunk(items: List[Tuple[int, "LightCurve"]]) -> int:
                ok = 0
                # Avoid shared-transaction context when multithreading; rely on per-call sessions
                for sid, curve in items:
                    try:
                        db.add_light_curve(int(sid), curve)
                        ok += 1
                    except Exception as e:
                        logging.debug("add_light_curve failed for star %r in %s: %s", sid, pf, e)
                return ok

            done = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, int(options.db_workers))) as wex:
                futs = [wex.submit(_store_chunk, ch) for ch in chunks]
                for idx, fut in enumerate(concurrent.futures.as_completed(futs), 1):
                    try:
                        done += int(fut.result())
                    except Exception as e:
                        logging.debug("DB writer chunk failed for filter %s: %s", pf, e)
                    show_progress(100.0 * idx / float(total))
            if logging_level < logging.WARNING:
                print()
            logging.debug("Stored %d / %d light curves in %s", done, len(curves), pf)

        # End filters loop

    owner_writable(output_db_path, False)
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
