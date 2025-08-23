#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
diffphot.py ? LEMON differential photometry (SQLAlchemy, thread-safe)

Port of LEMON's diffphot that keeps the familiar CLI / messages while using the
SQLAlchemy-backed, thread-safe database facade in `database.py`.

High level:
- Read stars/images/photometry from INPUT_DB
- For each filter, compute ensemble differential light curves:
    * Rank stars by raw-magnitude scatter (robust stdev)
    * Choose up to --max-cmp lowest-scatter comparison stars
    * For every target star, at each time, subtract the weighted mean of the
      comparison stars measured on that same image (excluding the target)
- Write results into OUTPUT_DB:
    * images (copied from input so times/filters exist)
    * stars (copied from input so ids align)
    * cmp_stars entries (for each target)
    * light_curves points (differential magnitudes)

Thread safety:
- All DB I/O goes through the SQLAlchemy facade (`database.LEMONSA` or `LEMONdB`)
  which uses scoped sessions (one per thread). Compute steps use ThreadPoolExecutor.

Note:
- This is an intentionally pragmatic port: it produces useful, ensemble-based
  differential curves with sensible defaults without reproducing every upstream
  heuristic. You can tune options like --max-cmp, --min-snr, etc.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import logging
import math
import os
import shutil
import sys
from collections import defaultdict, Counter
from pathlib import Path
from statistics import median

import numpy as np

import database
import style
import defaults

# ------------------------------------------------------------------------------
# Helpers to tolerate either class name and either curve class
# ------------------------------------------------------------------------------

DBClass = getattr(database, "LEMONdB", None) or getattr(database, "LEMONSA")
if DBClass is None:
    raise RuntimeError("database module must define LEMONdB or LEMONSA")

LightCurveClass = getattr(database, "LightCurveItem", None) or getattr(database, "LightCurve")

# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------

description = """
Compute differential photometry light curves from an existing photometry database.

Usage:
  yuzu diffphot INPUT_DB OUTPUT_DB [options]
"""

parser = argparse.ArgumentParser(
    prog="diffphot",
    description=description.strip(),
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.usage = "%(prog)s [OPTION]... INPUT_DB OUTPUT_DB"

parser.add_argument("input_db", help="LEMON database with raw photometry")
parser.add_argument("output_db", help="output LEMON database for differential light curves")

parser.add_argument("--overwrite", action="store_true", help="overwrite OUTPUT_DB if it already exists")
parser.add_argument(
    "--cores",
    type=int,
    default=max(1, min(8, getattr(defaults, "ncores", 1))),
    help="parallel workers for curve building",
)
parser.add_argument("--filter", action="append", dest="filters", default=None,
                    help="process only this photometric filter (may be repeated)")
parser.add_argument("--min-snr", type=float, default=1.0,
                    help="ignore photometric points with SNR below this threshold")
parser.add_argument("--max-cmp", type=int, default=20,
                    help="maximum number of comparison stars per target")
parser.add_argument("--min-cmp", type=int, default=5,
                    help="minimum number of valid comparison stars required per target time")
parser.add_argument("--robust", action="store_true",
                    help="use MAD-based robust scatter to rank comparison stars (else, std)")
parser.add_argument("-v", "--verbose", action="count", default=getattr(defaults, "verbosity", 0),
                    help="increase output verbosity (repeat for more)")

# ------------------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------------------

def _robust_stdev(x: np.ndarray) -> float:
    """Robust scatter estimator ~ 1.4826 * MAD (like upstream)."""
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    med = np.median(x)
    return 1.4826 * np.median(np.abs(x - med))

def _plain_stdev(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    return float(np.std(x))

def _weight_from_stdev(sd: float) -> float:
    if not math.isfinite(sd) or sd <= 0:
        return 0.0
    return 1.0 / (sd * sd)

def _copy_stars_images(db_in: DBClass, db_out: DBClass):
    """Ensure output DB has identical stars and image rows (times/filters)
    so light_curve insertions can resolve image_id via (filter, time)."""
    # Copy stars
    for sid in db_in.star_ids:
        s = db_in.get_star(sid)  # (id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag)
        db_out.add_star(*s)

    # Copy images per filter by re-adding with same attributes
    for pf in db_in.pfilters:
        # Pull image times + basic columns through photometry join to ensure coverage
        # We'll just scan photometry(image_id) -> images rows
        from sqlalchemy import select
        # Use db_in.session directly (scoped in its facade)
        with db_in.session() as s:
            # ORM classes from database module
            ImageRow = database.ImageRow
            PhotometricFilter = database.PhotometricFilter
            rows = s.execute(
                select(ImageRow.path, ImageRow.unix_time, ImageRow.object, ImageRow.airmass,
                       ImageRow.gain, ImageRow.ra, ImageRow.dec, ImageRow.sources)
                .join(PhotometricFilter, PhotometricFilter.id == ImageRow.filter_id)
                .where(PhotometricFilter.name == str(pf))
                .order_by(ImageRow.unix_time.asc())
            ).all()
        for path, t, obj, am, g, ra, dec, sources in rows:
            db_out.add_image(database.Image(path=path, pfilter=pf, unix_time=t, object=obj,
                                            airmass=am, gain=g, ra=ra, dec=dec, sources=int(sources or 0)))

# ------------------------------------------------------------------------------
# Differential photometry core
# ------------------------------------------------------------------------------

def _collect_filter_data(db: DBClass, pfilter: str, min_snr: float):
    """Return:
        times          : sorted unique Unix times for this filter
        star_to_points : dict[star_id] -> list of (t, mag, snr)
    """
    star_to_points = {}
    # Using public API; DBStar encapsulates time-ordered photometry for (star, filter)
    for sid in db.star_ids:
        star = db.get_photometry(sid, pfilter)  # DBStar
        if len(star) == 0:
            continue
        pts = []
        for i in range(len(star)):
            t = star.time(i)
            m = star.mag(i)
            s = star.snr(i)
            if s is None or s < min_snr or not math.isfinite(m):
                continue
            pts.append((t, m, s))
        if pts:
            star_to_points[sid] = pts
    # Gather times
    time_set = set()
    for pts in star_to_points.values():
        for t, _, _ in pts:
            time_set.add(float(t))
    times = sorted(time_set)
    return times, star_to_points

def _rank_comparison_stars(star_to_points, robust: bool):
    """Compute per-star scatter and return [(sid, stdev)] sorted ascending."""
    stdev_fn = _robust_stdev if robust else _plain_stdev
    ranking = []
    for sid, pts in star_to_points.items():
        mags = np.array([m for _, m, _ in pts], dtype=float)
        sd = stdev_fn(mags)
        ranking.append((sid, float(sd)))
    ranking.sort(key=lambda x: (x[1], x[0]))
    return ranking

def _build_cmp_pool(ranking, max_cmp: int):
    """Return the set of comparison-star IDs (global pool for this filter)."""
    pool = [sid for sid, sd in ranking if math.isfinite(sd)]
    return pool[:max_cmp] if max_cmp > 0 else pool

def _index_points_by_time(pts):
    """Return dict[time] -> (mag, snr) for a star."""
    d = {}
    for t, m, s in pts:
        d[float(t)] = (float(m), float(s))
    return d

def _build_one_curve(
    target_id: int,
    pfilter: str,
    times,
    star_to_points,
    cmp_pool,
    ranking_map,
    min_cmp: int,
) -> tuple[int, LightCurveClass]:
    """Compute one star's differential light curve."""
    # Prepare structures
    target_pts = _index_points_by_time(star_to_points.get(target_id, []))
    # Comparison weights from ranking map (lower sd => larger weight)
    # Convert stdev -> weight = 1/sd^2; normalize per-target per-time by available ones.
    curve = LightCurveClass(pfilter=pfilter, cstars=[], cweights=[], cstdevs=[])
    # Record chosen cmp stars + weights once (global, from ranking) so they go to cmp_stars table
    cstars = []
    cweights = []
    cstdevs = []
    for sid in cmp_pool:
        if sid == target_id:
            continue
        sd = ranking_map.get(sid, float("inf"))
        w = _weight_from_stdev(sd)
        if w <= 0:
            continue
        cstars.append(int(sid))
        cweights.append(float(w))
        cstdevs.append(float(sd))
    if not cstars:
        # no comparison stars ? return empty curve
        return target_id, curve

    # Normalize the *stored* weights (cmp_stars); per-time normalization re-done below after masking Nones
    ws = np.asarray(cweights, dtype=float)
    ws = ws / ws.sum() if ws.sum() > 0 else ws
    curve.cstars = list(cstars)
    curve.cweights = ws
    curve.cstdevs = np.asarray(cstdevs, dtype=float)

    # Build differential points
    # Strategy: at each time, from the comparison pool, take those with measurements at this time,
    # renormalize their weights, compute weighted mean, subtract from target magnitude.
    # If target has no measurement at t, skip t.
    # If fewer than min_cmp available comparisons at t, skip that time.
    # Keep original SNR of target point for the light curve row.
    cmp_maps = {sid: _index_points_by_time(star_to_points[sid]) for sid in curve.cstars if sid in star_to_points}

    for t in times:
        tgt = target_pts.get(t)
        if tgt is None:
            continue
        tgt_mag, tgt_snr = tgt
        # Collect available comparison mags at this time
        mags = []
        ww = []
        for sid, w in zip(curve.cstars, curve.cweights):
            m = cmp_maps.get(sid, {}).get(t)
            if m is None:
                continue
            mags.append(m[0])
            ww.append(float(w))
        if len(mags) < min_cmp:
            continue
        wsum = sum(ww)
        if wsum <= 0:
            continue
        wnorm = [w / wsum for w in ww]
        cmp_mean = float(np.dot(wnorm, np.asarray(mags, dtype=float)))
        diff_mag = float(tgt_mag - cmp_mean)
        curve.add(t, diff_mag, None if tgt_snr is None else float(tgt_snr))

    return target_id, curve

# ------------------------------------------------------------------------------
# main
# ------------------------------------------------------------------------------

def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    opts = parser.parse_args(argv)

    # logging
    level = logging.WARNING
    if opts.verbose == 1:
        level = logging.INFO
    elif opts.verbose and int(opts.verbose) >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format=getattr(style, "logging_format", "%(message)s"))

    in_path = Path(opts.input_db)
    out_path = Path(opts.output_db)

    # Overwrite
    if out_path.exists() and opts.overwrite:
        try:
            out_path.unlink()
        except OSError:
            pass

    # Open DBs
    db_in = DBClass(str(in_path))
    db_out = DBClass(str(out_path))

    # Copy stars & images (so light_curve points can resolve image_id by (filter, time))
    print(f"{style.prefix}Making a copy of the input database...", end="")
    sys.stdout.flush()
    _copy_stars_images(db_in, db_out)
    print("done.")

    # Determine filters to process
    filters = db_in.pfilters
    if opts.filters:
        want = set(map(str, opts.filters))
        filters = [pf for pf in filters if pf in want]
    if not filters:
        print(f"{style.prefix}Error. No matching photometric filters found to process.")
        print(style.error_exit_message)
        return 1

    # Per-filter processing
    for pf in filters:
        print(style.prefix)
        print(f"{style.prefix}Processing filter {pf} ...")

        # Collect raw photometry per star and the union of times
        times, star_to_points = _collect_filter_data(db_in, pf, min_snr=float(opts.min_snr))
        nstars = len(star_to_points)
        print(f"{style.prefix}{nstars} stars with usable photometry; {len(times)} timestamps.")

        if nstars == 0 or len(times) == 0:
            continue

        # Rank potential comparison stars
        ranking = _rank_comparison_stars(star_to_points, robust=bool(opts.robust))
        ranking_map = {sid: sd for sid, sd in ranking}
        cmp_pool = _build_cmp_pool(ranking, max_cmp=int(opts.max_cmp))

        # Parallel build of curves
        work_ids = sorted(star_to_points.keys())
        results = []
        print(f"{style.prefix}Building differential light curves in parallel ({opts.cores} workers)...")
        with cf.ThreadPoolExecutor(max_workers=max(1, int(opts.cores))) as ex:
            futs = [
                ex.submit(
                    _build_one_curve, sid, pf, times, star_to_points, cmp_pool, ranking_map, int(opts.min_cmp)
                )
                for sid in work_ids
            ]
            for i, fut in enumerate(cf.as_completed(futs), 1):
                try:
                    results.append(fut.result())
                except Exception as e:
                    logging.debug("curve worker failed: %s", e)
                # tiny progress pulse
                if level <= logging.INFO and (i % max(1, len(work_ids)//10) == 0):
                    print(f"{style.prefix}  ... {i}/{len(work_ids)}", flush=True)

        # Store
        print(f"{style.prefix}Saving light curves to the database...")
        saved = 0
        for sid, curve in results:
            if len(curve) == 0 or len(curve.cstars) == 0:
                continue
            db_out.add_light_curve(int(sid), curve)
            saved += 1
        print(f"{style.prefix}{saved} curves written for filter {pf}.")

    # Basic metadata on output DB (best-effort; not all keys may exist in your facade)
    import time, socket, getpass, hashlib
    try:
        db_out.date = time.time()
    except Exception:
        pass
    try:
        db_out.author = getpass.getuser()
    except Exception:
        pass
    try:
        db_out.hostname = socket.gethostname()
    except Exception:
        pass
    try:
        h = hashlib.md5()
        h.update(str(getattr(db_out, "date", "")).encode("utf-8"))
        h.update(str(getattr(db_out, "author", "")).encode("utf-8"))
        h.update(str(getattr(db_out, "hostname", "")).encode("utf-8"))
        db_out.id = h.hexdigest()
    except Exception:
        pass

    # ANALYZE
    print(f"{style.prefix}Analyzing database statistics...", end="")
    sys.stdout.flush()
    try:
        db_out.analyze()
    except Exception:
        pass
    print("done.")
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
