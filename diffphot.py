#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
diffphot.py ? LEMON differential photometry (SQLAlchemy Core, thread-safe I/O)

- Reads raw photometry from INPUT_DB (SQLAlchemy Core engine)
- For each filter:
    · builds comparison-star sets **per target** using residual scatter
      (leave-one-out ensemble, iterative worst-fraction pruning)
    · builds differential light curves:
        target mag - weighted mean of comparisons at the same time
- Writes to OUTPUT_DB (SQLAlchemy Core):
    · ensures schema exists
    · copies stars & images
    · cmp_stars rows (per target/filter)
    · light_curves rows (differential points)
    · creates a compatibility `photometry` VIEW (or TABLE on old SQLite) mapped to `light_curves`

All DB writes are serialized to avoid SQLite locks. No database.LEMONdB sessions.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import logging
import math
import multiprocessing
import os
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
from sqlalchemy import create_engine, text

import style
import defaults

# ------------------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------------------

description = """
Compute differential photometry light curves from an existing photometry database.

Usage:
  yuzu diffphot INPUT_DB OUTPUT_DB [options]
""".strip()

parser = argparse.ArgumentParser(
    prog="diffphot",
    description=description,
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
parser.usage = "%(prog)s [OPTION]... INPUT_DB OUTPUT_DB"

parser.add_argument("input_db", help="LEMON database with raw photometry")
parser.add_argument("output_db", help="output LEMON database for differential light curves")

parser.add_argument("--overwrite", action="store_true", help="overwrite OUTPUT_DB if it already exists")
parser.add_argument(
    "--cores",
    type=int,
    default=max(1, getattr(defaults, "ncores", multiprocessing.cpu_count())),
    help="parallel workers for curve building",
)
parser.add_argument("--filter", action="append", dest="filters", default=None,
                    help="process only this photometric filter (may be repeated)")
parser.add_argument("--min-snr", type=float, default=1.0,
                    help="ignore photometric points with SNR below this threshold")
parser.add_argument("--max-cmp", type=int, default=20,
                    help="maximum number of comparison stars per target after pruning")
parser.add_argument("--min-cmp", type=int, default=5,
                    help="minimum number of valid comparison stars required per target time")
parser.add_argument("--robust", action="store_true",
                    help="use MAD-based robust scatter (else, std) when ranking residuals")
parser.add_argument("-v", "--verbose", action="count", default=getattr(defaults, "verbosity", 0),
                    help="increase output verbosity (repeat for more)")

# Internal heuristics (kept constant to avoid CLI sprawl)
_DROP_FRAC = 0.20          # drop worst fraction each pruning iteration
_MAX_ITER  = 5             # max pruning iters
_MIN_OVERLAP_FRAC = 0.60   # candidate must cover this fraction of target epochs
_MIN_POINTS_ABS   = 8      # and at least this many points

# ------------------------------------------------------------------------------
# Utilities (math)
# ------------------------------------------------------------------------------

def _robust_stdev(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    med = np.median(x)
    return 1.4826 * np.median(np.abs(x - med))

def _plain_stdev(x: np.ndarray) -> float:
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    return float(np.nanstd(x))

def _sigma_fn(robust: bool):
    return _robust_stdev if robust else _plain_stdev

def _weight_from_stdev(sd: float) -> float:
    if not math.isfinite(sd) or sd <= 0:
        return 0.0
    return 1.0 / (sd * sd)

# ------------------------------------------------------------------------------
# DB helpers (SQLAlchemy Core only)
# ------------------------------------------------------------------------------

def _engine_for(path: Path):
    # SQLite file; pragma tuning kept minimal
    return create_engine(f"sqlite+pysqlite:///{path}", future=True)

def _ensure_schema(eng):
    """Create the minimal schema needed for diffphot output if it doesn't exist."""
    ddl = [
        # photometric_filters
        """
        CREATE TABLE IF NOT EXISTS photometric_filters (
            id   INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        """,
        # stars
        """
        CREATE TABLE IF NOT EXISTS stars (
            id     INTEGER PRIMARY KEY,
            x      REAL NOT NULL,
            y      REAL NOT NULL,
            ra     REAL NOT NULL,
            dec    REAL NOT NULL,
            epoch  REAL NOT NULL,
            pm_ra  REAL,
            pm_dec REAL,
            imag   REAL NOT NULL
        );
        """,
        # images (UNIQUE(filter_id, unix_time) like photometry DB)
        """
        CREATE TABLE IF NOT EXISTS images (
            id        INTEGER PRIMARY KEY,
            path      TEXT NOT NULL,
            filter_id INTEGER,
            unix_time REAL,
            object    TEXT,
            airmass   REAL,
            gain      REAL,
            ra        REAL NOT NULL,
            dec       REAL NOT NULL,
            sources   INTEGER NOT NULL,
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            UNIQUE (filter_id, unix_time)
        );
        """,
        "CREATE INDEX IF NOT EXISTS img_by_filter_time ON images(filter_id, unix_time);",
        # light_curves (output of diffphot)
        """
        CREATE TABLE IF NOT EXISTS light_curves (
            id        INTEGER PRIMARY KEY,
            star_id   INTEGER NOT NULL,
            image_id  INTEGER NOT NULL,
            magnitude REAL NOT NULL,
            snr       REAL,
            FOREIGN KEY (star_id) REFERENCES stars(id),
            FOREIGN KEY (image_id) REFERENCES images(id),
            UNIQUE (star_id, image_id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS curve_by_star_image ON light_curves(star_id, image_id);",
        # cmp_stars (comparison star set and weights per target/filter)
        """
        CREATE TABLE IF NOT EXISTS cmp_stars (
            id        INTEGER PRIMARY KEY,
            star_id   INTEGER NOT NULL,
            filter_id INTEGER NOT NULL,
            cstar_id  INTEGER NOT NULL,
            stdev     REAL NOT NULL,
            weight    REAL NOT NULL,
            FOREIGN KEY (star_id)   REFERENCES stars(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            FOREIGN KEY (cstar_id)  REFERENCES stars(id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS cstars_by_star_filter ON cmp_stars(star_id, filter_id);",
        # metadata
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT NOT NULL,
            value BLOB,
            UNIQUE (key)
        );
        """,
    ]
    with eng.begin() as con:
        for stmt in ddl:
            con.execute(text(stmt))

def _list_filters(eng) -> List[str]:
    with eng.connect() as con:
        rows = con.execute(text("SELECT name FROM photometric_filters")).all()
    return sorted({str(r[0]) for r in rows if r[0] is not None})

def _copy_stars_images(in_eng, out_eng):
    """Copy STARS (preserving ids) and IMAGES (recreating rows) to the output DB."""
    # STARS
    with in_eng.connect() as cin:
        stars = cin.execute(text(
            "SELECT id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag "
            "FROM stars ORDER BY id ASC"
        )).all()

    with out_eng.begin() as cout:
        if stars:
            cout.execute(
                text(
                    "INSERT OR REPLACE INTO stars "
                    "(id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag) "
                    "VALUES (:id, :x, :y, :ra, :dec, :epoch, :pm_ra, :pm_dec, :imag)"
                ),
                [
                    dict(
                        id=int(sid),
                        x=float(x), y=float(y), ra=float(ra), dec=float(dec),
                        epoch=float(epoch),
                        pm_ra=None if pm_ra is None else float(pm_ra),
                        pm_dec=None if pm_dec is None else float(pm_dec),
                        imag=float(imag),
                    )
                    for sid, x, y, ra, dec, epoch, pm_ra, pm_dec, imag in stars
                ],
            )

    # IMAGES
    with in_eng.connect() as cin:
        imgs = cin.execute(text(
            "SELECT i.path, f.name, i.unix_time, i.object, i.airmass, i.gain, "
            "       i.ra, i.dec, i.sources "
            "FROM images AS i "
            "LEFT JOIN photometric_filters AS f ON f.id = i.filter_id "
            "ORDER BY f.name ASC, i.unix_time ASC"
        )).all()

    with out_eng.begin() as cout:
        # Ensure filters exist
        fltnames = sorted({str(r[1]) for r in imgs if r[1] is not None})
        for fn in fltnames:
            cout.execute(text("INSERT OR IGNORE INTO photometric_filters(name) VALUES (:n)"), {"n": fn})

        # Map name -> id
        fids = {r[0]: r[1] for r in cout.execute(text("SELECT name, id FROM photometric_filters")).all()}

        # Insert images; uniqueness is (filter_id, unix_time) so use OR IGNORE
        rows = []
        for path, fname, t, obj, am, g, ra, dec, src in imgs:
            if fname is None:
                continue
            rows.append(dict(
                path=str(path),
                filter_id=int(fids[str(fname)]),
                unix_time=None if t is None else float(t),
                object=obj,
                airmass=None if am is None else float(am),
                gain=None if g is None else float(g),
                ra=float(ra), dec=float(dec),
                sources=int(src or 0),
            ))
        if rows:
            cout.execute(
                text(
                    "INSERT OR IGNORE INTO images "
                    "(path, filter_id, unix_time, object, airmass, gain, ra, dec, sources) "
                    "VALUES (:path, :filter_id, :unix_time, :object, :airmass, :gain, :ra, :dec, :sources)"
                ),
                rows,
            )

def _collect_filter_data(in_eng, pfilter: str, min_snr: float) -> Tuple[List[float], Dict[int, List[Tuple[float, float, float]]]]:
    """Return (times, star_to_points) for this filter using SQL joins."""
    star_to_points: Dict[int, List[Tuple[float, float, float]]] = {}
    times_set = set()
    with in_eng.connect() as con:
        rows = con.execute(
            text(
                "SELECT p.star_id, i.unix_time, p.magnitude, p.snr "
                "FROM photometry AS p "
                "JOIN images AS i ON i.id = p.image_id "
                "JOIN photometric_filters AS f ON f.id = i.filter_id "
                "WHERE f.name = :fname "
                "ORDER BY p.star_id ASC, i.unix_time ASC"
            ),
            {"fname": str(pfilter)},
        ).all()

    for sid, t, m, snr in rows:
        if t is None or m is None or snr is None:
            continue
        m = float(m); snr = float(snr)
        if not math.isfinite(m) or snr < float(min_snr):
            continue
        sid_i = int(sid); t_f = float(t)
        times_set.add(t_f)
        star_to_points.setdefault(sid_i, []).append((t_f, m, snr))

    return sorted(times_set), star_to_points

def _resolve_image_id(out_eng, pfilter: str, unix_time: float) -> int | None:
    with out_eng.connect() as con:
        rid = con.execute(
            text(
                "SELECT i.id "
                "FROM images AS i JOIN photometric_filters AS f ON f.id = i.filter_id "
                "WHERE f.name = :fname AND i.unix_time = :t"
            ),
            {"fname": str(pfilter), "t": float(unix_time)},
        ).scalar_one_or_none()
    return None if rid is None else int(rid)

# ------------------------------------------------------------------------------
# LEMON-style comparison set per target (residual scatter + pruning)
# ------------------------------------------------------------------------------

def _align_to_target_times(
    target_times: List[float],
    pts: List[Tuple[float, float, float]],
) -> np.ndarray:
    """Return vector of magnitudes aligned to target_times; NaN where missing."""
    mp = {t: m for (t, m, _s) in pts}
    arr = np.full((len(target_times),), np.nan, dtype=float)
    for i, t in enumerate(target_times):
        val = mp.get(t)
        if val is not None and math.isfinite(val):
            arr[i] = float(val)
    return arr

def _loo_ensemble_median(A: np.ndarray, j: int) -> np.ndarray:
    """Leave-one-out median across columns (ignore NaNs). A is (T, K)."""
    if A.shape[1] <= 1:
        return np.full((A.shape[0],), np.nan)
    keep = [k for k in range(A.shape[1]) if k != j]
    return np.nanmedian(A[:, keep], axis=1)

def _compute_cmp_weights_for_target(
    target_id: int,
    star_to_points: Dict[int, List[Tuple[float, float, float]]],
    *,
    robust: bool,
    max_cmp: int,
) -> Tuple[List[int], List[float], List[float], List[float]]:
    """
    Returns (cstar_ids, weights, sigmas, target_times).
    - weights sum to 1 over the final set
    - sigmas are final residual scatters per comp
    - target_times is the sorted list of epochs for the target
    """
    t_pts = star_to_points.get(int(target_id), [])
    if not t_pts:
        return [], [], [], []

    tgt_times = [t for (t, _m, _s) in t_pts]
    T = len(tgt_times)
    min_needed = max(_MIN_POINTS_ABS, int(math.ceil(_MIN_OVERLAP_FRAC * T)))

    # Candidate list: all other stars with sufficient overlap on target epochs
    candidates: List[int] = []
    series: List[np.ndarray] = []
    for sid, pts in star_to_points.items():
        if sid == target_id:
            continue
        vec = _align_to_target_times(tgt_times, pts)
        if np.isfinite(vec).sum() >= min_needed:
            candidates.append(int(sid))
            series.append(vec)

    if not candidates:
        return [], [], [], tgt_times

    M = np.stack(series, axis=1)  # (T, C)
    C = M.shape[1]
    sig_fn = _sigma_fn(robust)

    # Preliminary reduction: keep best by raw (robust/std) scatter to lighten the matrix
    prelim = np.array([sig_fn(M[:, j]) for j in range(C)], dtype=float)
    order = np.argsort(prelim)  # small scatter first
    topk = min(max_cmp * 3, C) if max_cmp > 0 else C
    cols = order[:topk]
    M = M[:, cols]
    candidates = [candidates[j] for j in cols]

    # Iterative pruning by residual scatter using leave-one-out ensemble
    cur = list(range(M.shape[1]))
    for _ in range(_MAX_ITER):
        if len(cur) <= max(3, min(5, max_cmp)):  # don't over-shrink
            break
        # residual sigmas
        rsig = []
        for j in range(len(cur)):
            a = _loo_ensemble_median(M[:, cur], j)
            resid = M[:, cur[j]] - a
            rsig.append(sig_fn(resid))
        rsig = np.asarray(rsig, dtype=float)
        # drop worst fraction
        keep_n = max(3, int(round((1.0 - _DROP_FRAC) * len(cur))))
        keep_idx = np.argsort(rsig)[:keep_n]
        new_cur = [cur[k] for k in keep_idx]
        if len(new_cur) == len(cur):
            break
        cur = new_cur

    # Final sigma + 1/sigma^2 weights
    final_sig = []
    for j in range(len(cur)):
        a = _loo_ensemble_median(M[:, cur], j)
        resid = M[:, cur[j]] - a
        final_sig.append(sig_fn(resid))
    final_sig = np.asarray(final_sig, dtype=float)

    invvar = 1.0 / np.maximum(final_sig ** 2, 1e-12)
    w = invvar / float(np.nansum(invvar)) if np.isfinite(invvar).any() else invvar

    # Cap to max_cmp if still larger; renormalize
    if max_cmp > 0 and len(cur) > max_cmp:
        order2 = np.argsort(final_sig)[:max_cmp]  # best sigmas
        cur = [cur[k] for k in order2]
        w = w[order2]
        final_sig = final_sig[order2]
        s = float(np.sum(w))
        if s > 0:
            w = w / s

    cstar_ids = [candidates[j] for j in cur]
    weights = [float(x) for x in w.tolist()]
    sigmas  = [float(x) for x in final_sig.tolist()]
    return cstar_ids, weights, sigmas, tgt_times

# ------------------------------------------------------------------------------
# Differential photometry core (per-target)
# ------------------------------------------------------------------------------

def _build_one_curve(
    target_id: int,
    pfilter: str,
    star_to_points: Dict[int, List[Tuple[float, float, float]]],
    *,
    max_cmp: int,
    min_cmp: int,
    robust: bool,
) -> Tuple[int, dict]:
    """Return (target_id, {'cstars','cweights','cstdevs','points'})"""

    cstars, cweights, cstdevs, tgt_times = _compute_cmp_weights_for_target(
        target_id, star_to_points, robust=robust, max_cmp=max_cmp
    )

    if not cstars or not tgt_times:
        return int(target_id), {"cstars": [], "cweights": [], "cstdevs": [], "points": []}

    # Maps for fast lookup
    tgt_map = {t: (m, s) for (t, m, s) in star_to_points.get(target_id, [])}
    cmp_maps = {sid: {t: (m, s) for (t, m, s) in star_to_points.get(sid, [])} for sid in cstars}

    # Build points using per-target weights; re-normalize among comps present at each t
    w_arr = np.asarray(cweights, dtype=float)
    points: List[Tuple[float, float, float]] = []

    for t in tgt_times:
        tgt = tgt_map.get(t)
        if tgt is None:
            continue
        tgt_mag, tgt_snr = tgt

        mags, ww = [], []
        for sid, w in zip(cstars, w_arr):
            m = cmp_maps.get(sid, {}).get(t)
            if m is None:
                continue
            mags.append(m[0]); ww.append(float(w))

        if len(mags) < int(min_cmp):
            continue
        wsum = sum(ww)
        if wsum <= 0:
            continue
        wnorm = [w / wsum for w in ww]
        cmp_mean = float(np.dot(wnorm, np.asarray(mags, dtype=float)))
        diff_mag = float(tgt_mag - cmp_mean)
        points.append((float(t), diff_mag, float(tgt_snr) if tgt_snr is not None else None))

    return int(target_id), {
        "cstars": cstars,
        "cweights": [float(w) for w in w_arr.tolist()],
        "cstdevs": cstdevs,
        "points": points,
    }

# ------------------------------------------------------------------------------
# Compatibility layer: create photometry VIEW (or TABLE) from light_curves
# ------------------------------------------------------------------------------

def _sqlite_version_tuple(eng) -> Tuple[int, int, int]:
    with eng.connect() as con:
        v = con.execute(text("SELECT sqlite_version()")).scalar_one()
    parts: List[int] = []
    for tok in str(v).split("."):
        try:
            parts.append(int(tok))
        except Exception:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])  # type: ignore[return-value]

def _ensure_photometry_compat(out_eng) -> None:
    """
    If `photometry` doesn't exist but `light_curves` does, create a
    compatibility layer so code that expects `photometry` keeps working.

    Preference:
      1) Create a VIEW `photometry` selecting from `light_curves` with a ROW_NUMBER() id
         (requires SQLite >= 3.25).
      2) Else materialize a TABLE `photometry` and bulk-copy from `light_curves`.
    """
    with out_eng.connect() as con:
        has_photometry = bool(con.execute(
            text("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='photometry'")
        ).fetchone())
        has_light_curves = bool(con.execute(
            text("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name='light_curves'")
        ).fetchone())

    if has_photometry or not has_light_curves:
        return  # nothing to do

    major, minor, patch = _sqlite_version_tuple(out_eng)
    can_row_number = (major, minor, patch) >= (3, 25, 0)

    try:
        with out_eng.begin() as con:
            if can_row_number:
                # Create a compatibility VIEW with an id
                con.execute(text("DROP VIEW IF EXISTS photometry"))
                con.execute(text(
                    """
                    CREATE VIEW photometry AS
                    SELECT
                        ROW_NUMBER() OVER ()            AS id,
                        lc.star_id                      AS star_id,
                        lc.image_id                     AS image_id,
                        lc.magnitude                    AS magnitude,
                        COALESCE(lc.snr, 0.0)           AS snr
                    FROM light_curves lc
                    """
                ))
            else:
                # Materialize a real table with sane schema and indexes
                con.execute(text(
                    """
                    CREATE TABLE IF NOT EXISTS photometry (
                      id        INTEGER PRIMARY KEY AUTOINCREMENT,
                      star_id   INTEGER NOT NULL,
                      image_id  INTEGER NOT NULL,
                      magnitude REAL    NOT NULL,
                      snr       REAL    NOT NULL,
                      UNIQUE (star_id, image_id)
                    )
                    """
                ))
                # Populate from light_curves
                con.execute(text(
                    """
                    INSERT OR IGNORE INTO photometry (star_id, image_id, magnitude, snr)
                    SELECT lc.star_id, lc.image_id, lc.magnitude, COALESCE(lc.snr, 0.0)
                    FROM light_curves lc
                    """
                ))
                # Indexes for common lookups
                con.execute(text("CREATE INDEX IF NOT EXISTS phot_by_star_image ON photometry(star_id, image_id)"))
                con.execute(text("CREATE INDEX IF NOT EXISTS phot_by_image ON photometry(image_id)"))
    except Exception as e:
        # Non-fatal: leave DB as-is; downstream tools can read light_curves directly
        logging.warning("Failed to create photometry compatibility layer: %s", e)

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

    in_eng = _engine_for(in_path)
    out_eng = _engine_for(out_path)

    # Make sure the output DB has the required tables
    _ensure_schema(out_eng)

    # Copy stars & images first
    print(f"{style.prefix}Making a copy of the input database...", end="")
    sys.stdout.flush()
    _copy_stars_images(in_eng, out_eng)
    print("done.")

    # Determine filters
    filters = _list_filters(in_eng)
    if opts.filters:
        want = set(map(str, opts.filters))
        filters = [pf for pf in filters if pf in want]
    if not filters:
        print(f"{style.prefix}Error. No matching photometric filters found to process.")
        print(style.error_exit_message)
        return 1

    # Per-filter processing
    for pf in sorted(filters):
        print(style.prefix)
        print(f"{style.prefix}Processing filter {pf} ...")

        _times_all, star_to_points = _collect_filter_data(in_eng, pf, min_snr=float(opts.min_snr))
        nstars = len(star_to_points)
        print(f"{style.prefix}{nstars} stars with usable photometry.")
        if nstars == 0:
            continue

        # Parallel curve computation (compute only; all DB I/O is serialized)
        work_ids = sorted(star_to_points.keys())
        workers = max(1, getattr(defaults, "ncores", multiprocessing.cpu_count()))
        print(f"{style.prefix}Building differential light curves in parallel ({workers} workers).")
        results: List[Tuple[int, dict]] = []
        with cf.ThreadPoolExecutor(max_workers=max(1, int(workers))) as ex:
            futs = [
                ex.submit(
                    _build_one_curve,
                    sid, pf, star_to_points,
                    max_cmp=int(opts.max_cmp),
                    min_cmp=int(opts.min_cmp),
                    robust=bool(opts.robust),
                )
                for sid in work_ids
            ]
            for i, fut in enumerate(cf.as_completed(futs), 1):
                try:
                    results.append(fut.result())
                except Exception as e:
                    logging.debug("curve worker failed for a star: %s", e)
                if level <= logging.INFO and (i % max(1, len(work_ids)//10) == 0):
                    print(f"{style.prefix}  . {i}/{len(work_ids)}", flush=True)

        # Serialize DB writes (avoid locks)
        print(f"{style.prefix}Saving light curves to the database...")

        with out_eng.begin() as con:
            # ensure filter id exists in output DB
            con.execute(text("INSERT OR IGNORE INTO photometric_filters(name) VALUES (:n)"), {"n": str(pf)})
            f_id = con.execute(text("SELECT id FROM photometric_filters WHERE name=:n"), {"n": str(pf)}).scalar_one()

            # cmp_stars: for each star, replace its set for this filter
            by_star = {}
            for sid, payload in results:
                if not payload["cstars"]:
                    continue
                # Defensive: single normalization per (star, filter)
                wsum = sum(payload["cweights"])
                if wsum > 0:
                    norm_w = [w / wsum for w in payload["cweights"]]
                else:
                    norm_w = payload["cweights"]

                rows = []
                for c, w, sd in zip(payload["cstars"], norm_w, payload["cstdevs"]):
                    rows.append({"star_id": int(sid), "filter_id": int(f_id),
                                 "cstar_id": int(c), "stdev": float(sd), "weight": float(w)})
                by_star[int(sid)] = rows

            for sid, rows in by_star.items():
                con.execute(text("DELETE FROM cmp_stars WHERE star_id=:sid AND filter_id=:fid"),
                            {"sid": int(sid), "fid": int(f_id)})
                if rows:
                    con.execute(text(
                        "INSERT INTO cmp_stars (star_id, filter_id, cstar_id, stdev, weight) "
                        "VALUES (:star_id, :filter_id, :cstar_id, :stdev, :weight)"
                    ), rows)

            # light_curves: upsert by (star_id, image_id)
            lc_rows = []
            for sid, payload in results:
                pts = payload["points"]
                if not pts:
                    continue
                for (t, dm, snr) in pts:
                    iid = _resolve_image_id(out_eng, pf, float(t))
                    if iid is None:
                        continue
                    lc_rows.append({"star_id": int(sid), "image_id": int(iid),
                                    "magnitude": float(dm),
                                    "snr": None if snr is None else float(snr)})

            if lc_rows:
                con.execute(text(
                    "INSERT OR REPLACE INTO light_curves (star_id, image_id, magnitude, snr) "
                    "VALUES (:star_id, :image_id, :magnitude, :snr)"
                ), lc_rows)

        print(f"{style.prefix}{len({sid for sid, p in results if p['points']})} curves written for filter {pf}.")

    # Metadata & analyze (best-effort)
    try:
        with out_eng.begin() as con:
            con.execute(text(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (:k, :v)"
            ), [{"k": "date", "v": float(time.time())},
                {"k": "author", "v": os.getenv("USER") or os.getenv("USERNAME") or ""},
                {"k": "hostname", "v": socket.gethostname()}])
            rows = con.execute(text("SELECT value FROM metadata WHERE key in ('date','author','hostname') ORDER BY key")).all()
            md5 = hashlib.md5()
            for (v,) in rows:
                md5.update(str(v).encode("utf-8"))
            con.execute(text(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('id', :v)"
            ), {"v": md5.hexdigest()})
    except Exception:
        pass

    # Create compatibility photometry view/table
    _ensure_photometry_compat(out_eng)

    # ANALYZE to refresh stats
    print(f"{style.prefix}Analyzing database statistics...", end="")
    sys.stdout.flush()
    try:
        with out_eng.begin() as con:
            con.execute(text("ANALYZE"))
    except Exception:
        pass
    print("done.")
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
