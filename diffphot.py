#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
diffphot.py ? Differential photometry (LEMON/Broeg-style) with SQLAlchemy Core.

Features:
  ? Per-filter differential photometry using an artificial comparison star.
  ? Optional LEMON-compat behavior:
      - mean baseline (instead of median),
      - allow missing comparison measurements,
      - per-epoch weight re-normalization.
  ? Comparison set trimming by worst-fraction of residual scatter, iteratively.
  ? Writes: stars, images, cmp_stars, light_curves, and a compat photometry VIEW/TABLE.
  ? Debug tracer: --debug-star (and optional --debug-filter).
  ? Console progress bar for the parallel curve build (auto-enabled on TTY).
  ? FULLY PARALLELIZED differential photometry computation.
  ? Comprehensive progress tracking and statistics.

CLI examples:
  yuzu diffphot in.db out.db
  yuzu diffphot in.db out.db --lemon-compat --debug-star 145 --debug-filter R -v
  yuzu diffphot in.db out.db --cores 8 --progress -vv
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import logging
import math
import multiprocessing
import os
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sqlalchemy import create_engine, text

# ---------------- style/default fallbacks ----------------
try:
    import style  # type: ignore
except (ImportError, ModuleNotFoundError):
    class _Style:
        prefix = ">> "
        logging_format = "%(message)s"
        error_exit_message = ""
    style = _Style()  # type: ignore

try:
    import defaults  # type: ignore
except (ImportError, ModuleNotFoundError):
    class _Defaults:
        ncores = multiprocessing.cpu_count()
        verbosity = 0
    defaults = _Defaults()  # type: ignore


# ---------------- CLI ----------------
description = """
Compute differential photometry (Broeg+05 artificial comparison star) from a raw photometry DB.

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

parser.add_argument("--overwrite", action="store_true",
                    help="overwrite OUTPUT_DB if it already exists")

parser.add_argument("--cores", type=int,
                    default=max(1, getattr(defaults, "ncores", multiprocessing.cpu_count())),
                    help="parallel workers for curve building")

parser.add_argument("--filter", action="append", dest="filters", default=None,
                    help="process only this photometric filter (name; may be repeated)")

parser.add_argument("--min-snr", type=float, default=1.0,
                    help="ignore photometric points with SNR below this threshold")

parser.add_argument("--max-cmp", type=int, default=20,
                    help="maximum number of comparison stars per target")

parser.add_argument("--min-cmp", type=int, default=5,
                    help="minimum number of valid comparison stars required per target time")

parser.add_argument("--robust", action="store_true",
                    help="use robust (MAD) dispersion instead of plain standard deviation")

parser.add_argument("--worst-fraction", type=float, default=0.25,
                    help="fraction of worst candidates to discard at each trimming step")

parser.add_argument("--lemon-compat", action="store_true",
                    help="LEMON-like ensemble: mean baseline, allow missing comp points, "
                         "per-epoch weight re-normalization")
parser.add_argument("--allow-missing", action="store_true",
                    help="allow missing comparison measurements and renormalize weights per epoch")

# Debug tracer
parser.add_argument("--debug-star", type=int, default=None,
                    help="star ID to trace (candidate pool, trimming iterations, final weights)")
parser.add_argument("--debug-filter", type=str, default=None,
                    help="optional filter name to limit debug trace")

# Progress bar control
parser.add_argument("--no-progress", action="store_true",
                    help="disable the live progress bar even if stdout is a TTY")
parser.add_argument("--progress", action="store_true",
                    help="force-enable the live progress bar even if stdout is not a TTY")

parser.add_argument("-v", "--verbose", action="count", default=getattr(defaults, "verbosity", 0),
                    help="increase output verbosity (repeat for more)")


# ---------------- Enhanced progress bar ----------------
class _Progress:
    """Enhanced progress bar with ETA and statistics."""

    def __init__(self, total: int, label: str = "", enabled: bool = True):
        self.total = max(1, int(total))
        self.label = label
        self.enabled = bool(enabled)
        self.count = 0
        self.start_time = time.time()
        self._last_update = 0
        self._last_render = ""

    def _term_width(self) -> int:
        try:
            return shutil.get_terminal_size(fallback=(80, 24)).columns
        except (OSError, AttributeError):
            return 80

    def _render_line(self) -> str:
        if self.total <= 0:
            pct = 100.0
            bar = "#" * 10
            return f"{self.label} [{bar}] 100% ({self.count}/{self.total})"

        pct = (self.count / self.total) * 100.0

        # Calculate ETA
        elapsed = time.time() - self.start_time
        if elapsed > 0 and self.count > 0 and self.count < self.total:
            rate = self.count / elapsed
            remaining = (self.total - self.count) / rate
            eta_min = int(remaining // 60)
            eta_sec = int(remaining % 60)
            eta_str = f"{eta_min:02d}:{eta_sec:02d}"
        else:
            eta_str = "00:00"

        # Build progress bar
        width = max(10, min(30, self._term_width() - 90))
        filled = int(round(width * min(1.0, max(0.0, self.count / max(1, self.total)))))
        bar = "#" * filled + "-" * (width - filled)

        # Calculate rate
        rate = self.count / elapsed if elapsed > 0 else 0

        return f"{self.label} [{bar}] {pct:5.1f}% | {self.count}/{self.total} | {rate:.1f}/s | ETA {eta_str}"

    def update(self, step: int = 1) -> None:
        """Update progress display."""
        if not self.enabled:
            return

        self.count = min(self.total, self.count + int(step))

        # Throttle updates to twice per second
        now = time.time()
        if now - self._last_update < 0.5 and self.count < self.total:
            return
        self._last_update = now

        line = self._render_line()
        try:
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        except (OSError, UnicodeEncodeError):
            pass
        self._last_render = line

    def finish(self) -> None:
        """Complete the progress bar."""
        if not self.enabled:
            return

        self.count = self.total
        elapsed = time.time() - self.start_time
        rate = self.total / elapsed if elapsed > 0 else 0

        width = max(10, min(30, self._term_width() - 90))
        bar = "#" * width

        line = f"{self.label} [{bar}] 100.0% | {self.total}/{self.total} | {rate:.1f}/s | Done!\n"

        try:
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        except (OSError, UnicodeEncodeError):
            pass
        self._last_render = ""

    def clear_line(self) -> None:
        """Clear the current progress line."""
        if not self.enabled:
            return
        width = self._term_width()
        try:
            sys.stdout.write("\r" + " " * width + "\r")
            sys.stdout.flush()
        except (OSError, UnicodeEncodeError):
            pass
        self._last_render = ""

    def print_above(self, text: str) -> None:
        """Print a message above the progress bar."""
        if not self.enabled:
            print(text)
            return
        self.clear_line()
        print(text)
        # redraw current bar
        if self.count < self.total and self._last_render:
            try:
                sys.stdout.write("\r" + self._last_render)
                sys.stdout.flush()
            except (OSError, UnicodeEncodeError):
                pass


# ---------------- math helpers ----------------
def _mad_sigma(x: np.ndarray) -> float:
    """Compute MAD-based robust standard deviation estimate."""
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    med = np.median(x)
    return float(1.4826 * np.median(np.abs(x - med)))


def _std_sigma(x: np.ndarray) -> float:
    """Compute standard deviation."""
    x = x[np.isfinite(x)]
    if x.size < 2:
        return float("inf")
    # population std; switch to ddof=1 if you want sample std
    return float(np.std(x, ddof=0))


def _sigma(x: np.ndarray, robust: bool) -> float:
    """Compute dispersion (robust or standard)."""
    return _mad_sigma(x) if robust else _std_sigma(x)


def _invvar_weight(sd: float) -> float:
    """Compute inverse-variance weight."""
    if not math.isfinite(sd) or sd <= 0.0:
        return 0.0
    return 1.0 / (sd * sd)


def _leave_one_out_sigmas(M: np.ndarray, robust: bool) -> np.ndarray:
    """Compute leave-one-out dispersions for each row in M."""
    if M.size == 0:
        return np.empty((0,), dtype=float)

    mask = np.isfinite(M)
    weights = np.ones(M.shape[0], dtype=float)

    sum_w = np.sum(weights[:, None] * mask, axis=0)
    sum_wm = np.nansum(M * weights[:, None], axis=0)

    sigmas = np.empty(M.shape[0], dtype=float)
    for i in range(M.shape[0]):
        row = M[i]
        mask_i = mask[i]

        sum_w_excl = sum_w - (weights[i] * mask_i)
        sum_wm_excl = sum_wm - (weights[i] * row)

        valid = mask_i & (sum_w_excl > 0)
        if valid.sum() < 2:
            sigmas[i] = float("inf")
            continue

        baseline = sum_wm_excl[valid] / sum_w_excl[valid]
        res = row[valid] - baseline
        sigmas[i] = _sigma(res, robust=robust)

    return sigmas


def _iterative_trim_and_weight_legacy(
    M: np.ndarray,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    want_trace: bool = False,
):
    """Legacy trimming/weighting: baseline = (nan)mean of all candidates."""
    trace: List[dict] = []

    if M.size == 0:
        return np.empty((0,), dtype=int), np.empty((0,)), np.empty((0,)), trace

    keep = np.arange(M.shape[0], dtype=int)
    wf = float(worst_fraction)
    wf = min(1.0, max(0.0, wf))
    max_cmp = max(1, int(max_cmp))
    step = 0

    def baseline(A: np.ndarray) -> np.ndarray:
        return np.nanmean(A, axis=0)

    def sigma_row(row: np.ndarray, E: np.ndarray) -> float:
        res = row - E
        res = res[np.isfinite(res)]
        if res.size < 2:
            return float("inf")
        return _mad_sigma(res) if robust else _std_sigma(res)

    while keep.size > max_cmp:
        E = baseline(M[keep, :])
        sig_full = np.array([sigma_row(M[k, :], E) for k in keep], dtype=float)

        if wf <= 0.0:
            n_drop = keep.size - max_cmp
        else:
            n_drop = int(math.ceil(keep.size * wf))
            n_drop = max(1, n_drop)
            if keep.size - n_drop < max_cmp:
                n_drop = keep.size - max_cmp

        order = np.argsort(sig_full)
        keep_sorted = keep[order]
        drop = keep_sorted[-n_drop:]
        keep = keep_sorted[:-n_drop]

        if want_trace:
            trace.append({
                "step": step,
                "kept_ids": keep.tolist(),
                "dropped_ids": drop.tolist(),
                "sigmas_kept": [float(s) for s in sig_full[order[:-n_drop]]],
                "sigmas_dropped": [float(s) for s in sig_full[order[-n_drop:]]],
            })
        step += 1

        if n_drop <= 0:
            break

    E = baseline(M[keep, :])
    sig_final = np.array([sigma_row(M[k, :], E) for k in keep], dtype=float)
    w = np.array([_invvar_weight(sd) for sd in sig_final], dtype=float)
    wsum = float(np.nansum(w))
    if wsum > 0:
        w /= wsum

    if want_trace:
        trace.append({
            "step": step,
            "final_keep": keep.tolist(),
            "final_sigmas": [float(s) for s in sig_final],
            "final_weights": [float(x) for x in w],
        })

    return keep, sig_final, w, trace


# ---------------- DB helpers ----------------
def _engine_for(path: Path):
    """Create SQLAlchemy engine for database."""
    path_obj = Path(path) if not isinstance(path, Path) else path
    return create_engine(f"sqlite+pysqlite:///{path_obj}", future=True)


def _ensure_schema(eng) -> None:
    """Ensure output database has required schema."""
    ddl = [
        """
        CREATE TABLE IF NOT EXISTS photometric_filters (
            id   INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL
        );
        """,
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
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key   TEXT NOT NULL,
            value BLOB,
            UNIQUE (key)
        );
        """,
    ]
    try:
        with eng.begin() as con:
            for stmt in ddl:
                con.execute(text(stmt))
    except Exception as e:
        logging.error(f"Failed to create database schema: {e}")
        raise


def _list_filters(eng) -> List[str]:
    """List all photometric filters in database."""
    try:
        with eng.connect() as con:
            rows = con.execute(text("SELECT name FROM photometric_filters")).all()
        return sorted({str(r[0]) for r in rows if r[0] is not None})
    except Exception as e:
        logging.error(f"Failed to list filters: {e}")
        return []


def _copy_stars_images(in_eng, out_eng) -> None:
    """Copy stars and images from input to output database."""
    try:
        # Stars
        with in_eng.connect() as cin:
            stars = cin.execute(text(
                "SELECT id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag FROM stars ORDER BY id"
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

        # Images
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
            fltnames = sorted({str(fname) for _p, fname, *_rest in imgs if fname is not None})
            for fn in fltnames:
                cout.execute(text("INSERT OR IGNORE INTO photometric_filters(name) VALUES (:n)"),
                             {"n": str(fn)})

            # Map names to ids
            fids = {r[0]: r[1] for r in cout.execute(text(
                "SELECT name, id FROM photometric_filters"
            )).all()}

            # Insert images
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
                cout.execute(text(
                    "INSERT OR IGNORE INTO images "
                    "(path, filter_id, unix_time, object, airmass, gain, ra, dec, sources) "
                    "VALUES (:path, :filter_id, :unix_time, :object, :airmass, :gain, :ra, :dec, :sources)"
                ), rows)
    except Exception as e:
        logging.error(f"Failed to copy stars and images: {e}")
        raise


def _collect_filter_data(in_eng, pfilter: str, min_snr: float
                         ) -> Tuple[Dict[int, List[Tuple[float, float, float]]],
                                    Dict[int, List[float]]]:
    """
    Collect photometric data for a filter.
    Returns: (star_points, star_times) where star_points[star_id] = [(time, mag, snr), ...]
    """
    star_points: Dict[int, List[Tuple[float, float, float]]] = {}
    star_times: Dict[int, List[float]] = {}

    try:
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
            m = float(m)
            if not math.isfinite(m):
                continue
            snr = float(snr)
            if not math.isfinite(snr) or snr < float(min_snr):
                continue
            star_points.setdefault(int(sid), []).append((float(t), m, float(snr)))
            star_times.setdefault(int(sid), []).append(float(t))
    except Exception as e:
        logging.error(f"Failed to collect filter data for {pfilter}: {e}")
        raise

    return star_points, star_times


def _resolve_image_id(out_eng, pfilter: str, unix_time: float) -> Optional[int]:
    """Resolve image ID from filter name and unix time."""
    try:
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
    except Exception as e:
        logging.debug(f"Failed to resolve image ID for {pfilter} at {unix_time}: {e}")
        return None


# ---------------- ensemble building ----------------
def _matrix_for_target(
    target_id: int,
    star_points: Dict[int, List[Tuple[float, float, float]]],
    star_times: Dict[int, List[float]],
    allow_missing: bool = False,
):
    """
    Build photometry matrix for target star.
    Returns:
      y      : target magnitudes aligned to T              (shape [T])
      M      : candidate magnitudes aligned to T           (shape [C, T])
               (NaN where missing if allow_missing=True)
      cids   : candidate star IDs                          (len C)
      T      : list of times (target times)                (len T)
    """
    tgt_pts = star_points.get(target_id, [])
    if not tgt_pts:
        return np.empty((0,)), np.empty((0, 0)), [], []

    T = [t for (t, _m, _s) in tgt_pts]
    Tset = set(T)
    y = np.array([m for (_t, m, _s) in tgt_pts], dtype=float)
    if y.size == 0:
        return np.empty((0,)), np.empty((0, 0)), [], []

    cids: List[int] = []
    rows: List[np.ndarray] = []

    for sid, pts in star_points.items():
        if sid == target_id:
            continue
        m_by_t = {t: m for (t, m, _s) in pts}
        if not allow_missing:
            if not Tset.issubset(set(star_times.get(sid, []))):
                continue
            row = np.array([m_by_t[t] for t in T], dtype=float)
            if np.all(np.isfinite(row)):
                cids.append(int(sid))
                rows.append(row)
        else:
            row = np.array([m_by_t.get(t, np.nan) for t in T], dtype=float)
            if np.isfinite(row).sum() >= 2:
                cids.append(int(sid))
                rows.append(row)

    if not rows:
        return y, np.empty((0, len(T))), [], T
    M = np.vstack(rows)  # [C, T]
    return y, M, cids, T


def _iterative_trim_and_weight(
    M: np.ndarray,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    want_trace: bool = False,
    lemon_compat: bool = False,
):
    """
    Iteratively trim worst comparison stars and compute weights.
    Returns: (keep_indices, sigmas, weights, trace)
    """
    trace: List[dict] = []

    if lemon_compat:
        return _iterative_trim_and_weight_legacy(
            M, robust=robust, worst_fraction=worst_fraction, max_cmp=max_cmp, want_trace=want_trace
        )

    if M.size == 0:
        return np.empty((0,), dtype=int), np.empty((0,)), np.empty((0,)), trace

    C0 = M.shape[0]
    keep = np.arange(C0, dtype=int)

    wf = float(worst_fraction)
    wf = min(1.0, max(0.0, wf))
    max_cmp = max(1, int(max_cmp))
    step = 0

    while keep.size > max_cmp:
        sig_full = _leave_one_out_sigmas(M[keep, :], robust=robust)

        if wf <= 0.0:
            n_drop = keep.size - max_cmp
        else:
            n_drop = int(math.ceil(keep.size * wf))
            n_drop = max(1, n_drop)
            if keep.size - n_drop < max_cmp:
                n_drop = keep.size - max_cmp

        order = np.argsort(sig_full)
        keep_sorted = keep[order]
        drop = keep_sorted[-n_drop:]
        keep = keep_sorted[:-n_drop]

        if want_trace:
            trace.append({
                "step": step,
                "kept_ids": keep.tolist(),
                "dropped_ids": drop.tolist(),
                "sigmas_kept": [float(s) for s in sig_full[order[:-n_drop]]],
                "sigmas_dropped": [float(s) for s in sig_full[order[-n_drop:]]],
            })
        step += 1

        if n_drop <= 0:
            break

    sig_final = _leave_one_out_sigmas(M[keep, :], robust=robust)
    w = np.array([_invvar_weight(sd) for sd in sig_final], dtype=float)
    wsum = float(np.nansum(w))
    if wsum > 0:
        w /= wsum

    if want_trace:
        trace.append({
            "step": step,
            "final_keep": keep.tolist(),
            "final_sigmas": [float(s) for s in sig_final],
            "final_weights": [float(x) for x in w],
        })

    return keep, sig_final, w, trace


def _build_one_curve(
    target_id: int,
    pfilter: str,
    star_points: Dict[int, List[Tuple[float, float, float]]],
    star_times: Dict[int, List[float]],
    min_cmp: int,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    debug_star: Optional[int] = None,
    allow_missing: bool = False,
    lemon_compat: bool = False,
):
    """
    Build differential light curve for one target star.
    Returns (target_id, payload) where payload contains:
      'cstars', 'cweights', 'cstdevs', 'points', and optionally 'debug'
    """
    allow_missing = bool(allow_missing or lemon_compat)
    y, M, cids, T = _matrix_for_target(
        target_id, star_points, star_times, allow_missing=allow_missing
    )
    if y.size == 0 or M.size == 0 or len(cids) == 0:
        payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": []}
        if debug_star is not None and int(target_id) == int(debug_star):
            payload["debug"] = {
                "filter": pfilter, "target": int(target_id),
                "epochs": len(T), "candidates": [],
                "note": "no usable candidate matrix",
            }
        return int(target_id), payload

    want_trace = (debug_star is not None and int(target_id) == int(debug_star))
    keep_idx, sigmas, weights, trace = _iterative_trim_and_weight(
        M, robust=robust, worst_fraction=worst_fraction, max_cmp=int(max_cmp),
        want_trace=want_trace, lemon_compat=lemon_compat
    )
    if keep_idx.size == 0:
        payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": []}
        if want_trace:
            payload["debug"] = {
                "filter": pfilter, "target": int(target_id),
                "epochs": len(T), "candidates": cids, "trace": trace,
                "note": "kept set empty after trimming",
            }
        return int(target_id), payload

    sel_ids = [cids[i] for i in keep_idx.tolist()]
    sel_w   = weights.astype(float).tolist()
    sel_sd  = sigmas.astype(float).tolist()

    # Weighted differential curve
    Msel = M[keep_idx, :]  # [C, T]
    points: List[Tuple[float, float, float]] = []
    tgt_snr = [s for (_t, _m, s) in star_points.get(target_id, [])]

    if not allow_missing:
        if Msel.shape[0] >= int(min_cmp):
            cmp_mean = np.dot(weights, Msel)  # [T]
            diff = y - cmp_mean
            for j, t in enumerate(T):
                snr = tgt_snr[j] if j < len(tgt_snr) else None
                points.append((float(t), float(diff[j]), (None if snr is None else float(snr))))
    else:
        # per-epoch available comps; renormalize weights on the fly
        C = Msel.shape[0]
        if C >= int(min_cmp):
            for j, t in enumerate(T):
                col = Msel[:, j]  # [C]
                mask = np.isfinite(col)
                if mask.sum() < int(min_cmp):
                    continue
                subw = weights[mask]
                wsum = float(subw.sum())
                if wsum <= 0:
                    continue
                subw = subw / wsum
                cmp_mean_t = float(np.dot(subw, col[mask]))
                diff_mag = float(y[j] - cmp_mean_t)
                snr = tgt_snr[j] if j < len(tgt_snr) else None
                points.append((float(t), diff_mag, (None if snr is None else float(snr))))

    payload = {
        "cstars": sel_ids,
        "cweights": sel_w,
        "cstdevs": sel_sd,
        "points": points,
    }

    if want_trace:
        sig0 = _leave_one_out_sigmas(M, robust=robust)
        payload["debug"] = {
            "filter": pfilter,
            "target": int(target_id),
            "epochs": len(T),
            "n_candidates": len(cids),
            "candidates": [{"id": int(cid), "sigma0": float(sig0[i])} for i, cid in enumerate(cids)],
            "trace": trace,
            "final": {
                "cstars": sel_ids,
                "sigmas": sel_sd,
                "weights": sel_w,
                "sum_weights": float(sum(sel_w)),
            },
        }

    return int(target_id), payload


# ---------------- photometry VIEW/TABLE compat ----------------
def _sqlite_version_tuple(eng) -> Tuple[int, int, int]:
    """Get SQLite version as a tuple."""
    try:
        with eng.connect() as con:
            v = con.execute(text("SELECT sqlite_version()")).scalar_one()
        parts: List[int] = []
        for tok in str(v).split("."):
            try:
                parts.append(int(tok))
            except (ValueError, TypeError):
                parts.append(0)
        while len(parts) < 3:
            parts.append(0)
        return (parts[0], parts[1], parts[2])
    except Exception as e:
        logging.warning(f"Failed to get SQLite version, assuming (3, 0, 0): {e}")
        return (3, 0, 0)


def _ensure_photometry_compat(out_eng) -> None:
    """Create photometry compatibility view/table for backward compatibility."""
    try:
        with out_eng.connect() as con:
            has_photometry = bool(con.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='photometry' LIMIT 1"
            )).fetchone())
            has_lc = bool(con.execute(text(
                "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='light_curves' LIMIT 1"
            )).fetchone())

        if has_photometry or not has_lc:
            return

        major, minor, patch = _sqlite_version_tuple(out_eng)
        can_rownum = (major, minor, patch) >= (3, 25, 0)

        with out_eng.begin() as con:
            if can_rownum:
                con.execute(text("DROP VIEW IF EXISTS photometry"))
                con.execute(text(
                    """
                    CREATE VIEW photometry AS
                    SELECT
                        ROW_NUMBER() OVER ()  AS id,
                        lc.star_id             AS star_id,
                        lc.image_id            AS image_id,
                        lc.magnitude           AS magnitude,
                        COALESCE(lc.snr, 0.0)  AS snr
                    FROM light_curves lc
                    """
                ))
            else:
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
                con.execute(text(
                    """
                    INSERT OR IGNORE INTO photometry (star_id, image_id, magnitude, snr)
                    SELECT lc.star_id, lc.image_id, lc.magnitude, COALESCE(lc.snr, 0.0)
                    FROM light_curves lc
                    """
                ))
                con.execute(text("CREATE INDEX IF NOT EXISTS phot_by_star_image ON photometry(star_id, image_id)"))
                con.execute(text("CREATE INDEX IF NOT EXISTS phot_by_image ON photometry(image_id)"))
    except Exception as e:
        logging.warning(f"Failed to create photometry compatibility layer: {e}")


# ---------------- debug output ----------------
def _fmtf(x: float, n: int = 6) -> str:
    """Format float with n decimal places."""
    try:
        return f"{float(x):.{n}f}"
    except (ValueError, TypeError):
        return str(x)


def _print_debug_report(pf: str, dbg: dict, progress: Optional[_Progress] = None) -> None:
    """Print detailed debug report for a target star."""
    pre = style.prefix
    def out(line: str):
        if progress:
            progress.print_above(line)
        else:
            print(line)

    out(pre)
    out(f"{pre}DEBUG ? filter={pf}  target={dbg.get('target')}  epochs={dbg.get('epochs')}")
    out(f"{pre}  n_candidates: {dbg.get('n_candidates', 0)}")
    cands = dbg.get("candidates") or []
    if cands:
        out(f"{pre}  initial candidates (id, sigma0):")
        for c in cands[:15]:
            out(f"{pre}    {c['id']:>6d}   ?0={_fmtf(c['sigma0'], 6)}")
        if len(cands) > 15:
            out(f"{pre}    ... ({len(cands)-15} more)")
    trace = dbg.get("trace") or []
    for step in trace:
        if "final_keep" in step:
            break
        kept = step.get("kept_ids", [])
        dropped = step.get("dropped_ids", [])
        out(f"{pre}  step {step.get('step')}: kept={len(kept)} dropped={len(dropped)}")
        if dropped:
            ds = ", ".join(str(i) for i in dropped[:10])
            out(f"{pre}    dropped indices: {ds}{' ...' if len(dropped)>10 else ''}")
    final = dbg.get("final") or {}
    f_ids = final.get("cstars") or []
    f_sd  = final.get("sigmas") or []
    f_w   = final.get("weights") or []
    out(f"{pre}  FINAL ({len(f_ids)} stars):")
    for sid, sd, w in zip(f_ids, f_sd, f_w):
        out(f"{pre}    cstar={sid:>6d}   ?={_fmtf(sd,6)}   w={_fmtf(w,6)}")
    out(f"{pre}  sum(weights) = {_fmtf(final.get('sum_weights', 0.0), 6)}")


# ---------------- Parallelized batch processing with progress ----------------
def _process_filter_batch(
    pfilter: str,
    targets: List[int],
    star_points: Dict[int, List[Tuple[float, float, float]]],
    star_times: Dict[int, List[float]],
    min_cmp: int,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    debug_star: Optional[int],
    allow_missing: bool,
    lemon_compat: bool,
) -> Tuple[List[Tuple[int, dict]], Dict[str, int]]:
    """
    Process a batch of target stars for a given filter.
    Returns (results, stats) where:
      - results: list of (target_id, payload) tuples
      - stats: dict with 'completed', 'failed', 'candidates', 'comparisons'
    """
    results = []
    stats = {'completed': 0, 'failed': 0, 'candidates': 0, 'comparisons': 0}

    for target_id in targets:
        try:
            result = _build_one_curve(
                target_id, pfilter, star_points, star_times,
                min_cmp, robust, worst_fraction, max_cmp,
                debug_star, allow_missing, lemon_compat
            )
            results.append(result)

            # Collect statistics
            _, payload = result
            n_candidates = len(payload.get('cstars', []))
            n_comparisons = len(payload.get('points', []))

            stats['completed'] += 1
            stats['candidates'] += n_candidates
            stats['comparisons'] += n_comparisons

        except Exception as e:
            logging.debug(f"Failed to build curve for star {target_id}: {e}")
            # Return empty result for this star
            payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": []}
            results.append((target_id, payload))
            stats['failed'] += 1

    return results, stats


# ---------------- main ----------------
def main(argv: Optional[List[str]] = None) -> int:
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

    if not in_path.exists():
        print(f"{style.prefix}Error. Input database '{in_path}' does not exist.")
        print(getattr(style, "error_exit_message", ""))
        return 1

    if out_path.exists() and opts.overwrite:
        try:
            out_path.unlink()
        except OSError as e:
            print(f"{style.prefix}Error. Cannot remove existing output database: {e}")
            print(getattr(style, "error_exit_message", ""))
            return 1

    in_eng = _engine_for(in_path)
    out_eng = _engine_for(out_path)

    # Ensure schema in output
    _ensure_schema(out_eng)

    # Copy stars & images first
    print(f"{style.prefix}Making a copy of the input database...", end="", flush=True)
    try:
        _copy_stars_images(in_eng, out_eng)
        print(" done.")
    except Exception as e:
        print(f"\n{style.prefix}Error copying database: {e}")
        print(getattr(style, "error_exit_message", ""))
        return 1

    # Filters to process
    filters = _list_filters(in_eng)
    if opts.filters:
        want = set(map(str, opts.filters))
        filters = [pf for pf in filters if pf in want]
    if not filters:
        print(f"{style.prefix}Error. No matching photometric filters found to process.")
        print(getattr(style, "error_exit_message", ""))
        return 1

    dbg_star = opts.debug_star
    dbg_filter = (opts.debug_filter.strip() if isinstance(opts.debug_filter, str) and opts.debug_filter else None)
    allow_missing = bool(opts.allow_missing or opts.lemon_compat)

    # Decide progress visibility
    auto_tty = sys.stdout.isatty()
    show_progress = (opts.progress or (auto_tty and not opts.no_progress))

    # Global statistics across all filters
    global_start_time = time.time()
    global_stats = {
        'total_stars': 0,
        'total_curves': 0,
        'total_measurements': 0,
        'total_comparison_stars': 0,
        'filters_processed': 0,
        'failed_curves': 0,
    }

    for pf in sorted(filters):
        print(style.prefix)
        print(f"{style.prefix}Processing filter {pf} ...")

        star_points, star_times = _collect_filter_data(in_eng, pf, min_snr=float(opts.min_snr))
        nstars = len(star_points)
        print(f"{style.prefix}{nstars} stars with usable photometry.")

        global_stats['total_stars'] += nstars

        if nstars == 0:
            continue

        targets = sorted(star_points.keys())
        workers = max(1, int(opts.cores))
        print(f"{style.prefix}Building differential light curves in parallel ({workers} workers).")

        # Split targets into batches for parallel processing
        batch_size = max(1, len(targets) // (workers * 4))  # 4 batches per worker
        batches = [targets[i:i + batch_size] for i in range(0, len(targets), batch_size)]

        print(f"{style.prefix}Total targets: {len(targets)}, divided into {len(batches)} batches")
        print(f"{style.prefix}Batch size: ~{batch_size} stars per batch")

        progress = _Progress(total=len(targets), label=f"{style.prefix} progress", enabled=show_progress)

        results: List[Tuple[int, dict]] = []
        all_stats = {'completed': 0, 'failed': 0, 'candidates': 0, 'comparisons': 0}

        # Statistics tracking
        start_time = time.time()
        last_update_time = start_time

        # Use ProcessPoolExecutor for true parallelism (not limited by GIL)
        with cf.ProcessPoolExecutor(max_workers=workers) as executor:
            # Submit all batches
            futures = {}
            for batch_idx, batch in enumerate(batches):
                future = executor.submit(
                    _process_filter_batch,
                    pf, batch, star_points, star_times,
                    int(opts.min_cmp), bool(opts.robust),
                    float(opts.worst_fraction), int(opts.max_cmp),
                    dbg_star, allow_missing, bool(opts.lemon_compat)
                )
                futures[future] = (batch_idx, len(batch))

            # Collect results as they complete with detailed progress
            completed_batches = 0
            for future in cf.as_completed(futures):
                batch_idx, batch_size = futures[future]
                try:
                    batch_results, batch_stats = future.result()
                    results.extend(batch_results)

                    # Update aggregate statistics
                    for key in all_stats:
                        all_stats[key] += batch_stats[key]

                    completed_batches += 1
                    progress.update(len(batch_results))

                    # Print detailed progress every 2 seconds or when verbose
                    current_time = time.time()
                    if opts.verbose or (current_time - last_update_time >= 2.0):
                        elapsed = current_time - start_time
                        rate = all_stats['completed'] / elapsed if elapsed > 0 else 0

                        msg = (f"{style.prefix}  Batch {completed_batches}/{len(batches)} done | "
                               f"Stars: {all_stats['completed']}/{len(targets)} | "
                               f"Rate: {rate:.1f} stars/s | "
                               f"Avg candidates: {all_stats['candidates'] / max(1, all_stats['completed']):.1f}")

                        if show_progress:
                            progress.print_above(msg)
                        elif opts.verbose:
                            print(msg)

                        last_update_time = current_time

                except Exception as e:
                    logging.error(f"Batch {batch_idx} processing failed: {e}")
                    completed_batches += 1
                    progress.update(batch_size)
                    # Continue with other batches

        progress.finish()

        # Final statistics
        total_time = time.time() - start_time
        print(style.prefix)
        print(f"{style.prefix}Differential photometry completed for filter {pf}:")
        print(f"{style.prefix}  Total time: {total_time:.1f} seconds")
        print(f"{style.prefix}  Successfully processed: {all_stats['completed']} stars")
        if all_stats['failed'] > 0:
            print(f"{style.prefix}  Failed: {all_stats['failed']} stars")
        print(f"{style.prefix}  Average processing rate: {all_stats['completed'] / total_time:.1f} stars/second")
        print(f"{style.prefix}  Total comparison stars used: {all_stats['candidates']}")
        print(f"{style.prefix}  Average comparison stars per target: {all_stats['candidates'] / max(1, all_stats['completed']):.1f}")
        print(f"{style.prefix}  Total differential measurements: {all_stats['comparisons']}")
        if all_stats['completed'] > 0:
            print(f"{style.prefix}  Average measurements per star: {all_stats['comparisons'] / all_stats['completed']:.1f}")
        print(style.prefix)

        # Serialize writes to avoid SQLite contention
        write_start = time.time()
        print(f"{style.prefix}Saving light curves to the database...")

        # Count what we're about to write
        total_cmp_stars = sum(1 for _, p in results if p.get("cstars"))
        total_measurements = sum(len(p.get("points", [])) for _, p in results)
        print(f"{style.prefix}  Writing {total_cmp_stars} comparison star sets")
        print(f"{style.prefix}  Writing {total_measurements} light curve measurements")

        debug_payload: Optional[dict] = None
        try:
            with out_eng.begin() as con:
                # Filter id in the output DB
                con.execute(text("INSERT OR IGNORE INTO photometric_filters(name) VALUES (:n)"),
                            {"n": str(pf)})
                f_id = con.execute(text(
                    "SELECT id FROM photometric_filters WHERE name=:n"
                ), {"n": str(pf)}).scalar_one()

                # cmp_stars: replace per target/filter
                for sid, payload in results:
                    if dbg_star is not None and int(sid) == int(dbg_star):
                        if dbg_filter is None or str(dbg_filter) == str(pf):
                            debug_payload = payload.get("debug")

                    if payload.get("cstars"):
                        rows = []
                        for c, w, sd in zip(payload["cstars"], payload["cweights"], payload["cstdevs"]):
                            rows.append({"star_id": int(sid), "filter_id": int(f_id),
                                         "cstar_id": int(c), "stdev": float(sd), "weight": float(w)})
                        con.execute(text(
                            "DELETE FROM cmp_stars WHERE star_id=:sid AND filter_id=:fid"
                        ), {"sid": int(sid), "fid": int(f_id)})
                        if rows:
                            con.execute(text(
                                "INSERT INTO cmp_stars (star_id, filter_id, cstar_id, stdev, weight) "
                                "VALUES (:star_id, :filter_id, :cstar_id, :stdev, :weight)"
                            ), rows)

                # light_curves upsert
                lc_rows: List[dict] = []
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

            n_written = sum(1 for _sid, p in results if p["points"])
            write_time = time.time() - write_start

            # Update global statistics
            global_stats['filters_processed'] += 1
            global_stats['total_curves'] += n_written
            global_stats['total_measurements'] += total_measurements
            global_stats['total_comparison_stars'] += sum(len(p.get('cstars', [])) for _, p in results)
            global_stats['failed_curves'] += all_stats['failed']

            print(f"{style.prefix}{n_written} curves written for filter {pf}.")
            print(f"{style.prefix}Database write time: {write_time:.1f} seconds")
            if total_measurements > 0:
                print(f"{style.prefix}Write rate: {total_measurements / write_time:.0f} measurements/second")
        except Exception as e:
            logging.error(f"Failed to save results to database: {e}")
            print(f"{style.prefix}Error saving to database: {e}")
            print(getattr(style, "error_exit_message", ""))
            return 1

        # Debug report (after writes)
        if dbg_star is not None and (dbg_filter is None or str(dbg_filter) == str(pf)):
            if debug_payload:
                _print_debug_report(pf, debug_payload)
            else:
                print(f"{style.prefix}DEBUG ? star {dbg_star} has no trace for filter {pf} "
                      f"(no points or no candidates).")

    # metadata (best-effort)
    try:
        with out_eng.begin() as con:
            con.execute(text(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (:k, :v)"
            ), [{"k": "date", "v": float(time.time())},
                {"k": "author", "v": os.getenv("USER") or os.getenv("USERNAME") or ""},
                {"k": "hostname", "v": socket.gethostname()}])

            rows = con.execute(text(
                "SELECT value FROM metadata WHERE key in ('date','author','hostname') ORDER BY key"
            )).all()
            md5 = hashlib.md5()
            for (v,) in rows:
                md5.update(str(v).encode("utf-8"))
            con.execute(text(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES ('id', :v)"
            ), {"v": md5.hexdigest()})
    except Exception as e:
        logging.warning(f"Failed to write metadata: {e}")

    # photometry compat
    _ensure_photometry_compat(out_eng)

    # ANALYZE
    print(f"{style.prefix}Analyzing database statistics...", end="", flush=True)
    try:
        with out_eng.begin() as con:
            con.execute(text("ANALYZE"))
        print(" done.")
    except Exception as e:
        logging.warning(f"Failed to analyze database: {e}")
        print(" done (with warnings).")

    # Print global summary
    global_time = time.time() - global_start_time
    print(style.prefix)
    print(f"{style.prefix}{'=' * 60}")
    print(f"{style.prefix}DIFFERENTIAL PHOTOMETRY SUMMARY")
    print(f"{style.prefix}{'=' * 60}")

    def format_time(seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    print(f"{style.prefix}Total execution time: {format_time(global_time)} ({global_time/60:.1f} minutes)")
    print(f"{style.prefix}Filters processed: {global_stats['filters_processed']}")
    print(f"{style.prefix}Stars analyzed: {global_stats['total_stars']}")
    print(f"{style.prefix}Light curves created: {global_stats['total_curves']}")
    if global_stats['failed_curves'] > 0:
        print(f"{style.prefix}Failed curves: {global_stats['failed_curves']}")
    print(f"{style.prefix}Total differential measurements: {global_stats['total_measurements']}")
    print(f"{style.prefix}Total comparison stars used: {global_stats['total_comparison_stars']}")

    if global_stats['total_curves'] > 0:
        avg_measurements = global_stats['total_measurements'] / global_stats['total_curves']
        avg_comparisons = global_stats['total_comparison_stars'] / global_stats['total_curves']
        print(f"{style.prefix}Average measurements per light curve: {avg_measurements:.1f}")
        print(f"{style.prefix}Average comparison stars per target: {avg_comparisons:.1f}")

    if global_time > 0:
        print(f"{style.prefix}Overall processing rate: {global_stats['total_stars'] / global_time:.1f} stars/second")

    print(f"{style.prefix}Output database: {out_path}")
    print(f"{style.prefix}{'=' * 60}")
    print(style.prefix)
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
