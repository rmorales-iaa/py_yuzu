#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
diffphot.py ? Differential photometry (LEMON/Broeg-style) with SQLAlchemy Core.

Features:
  ? Per-filter differential photometry using an artificial comparison star.
  ? Comparison set trimming by worst-fraction of residual scatter, iteratively.
  ? Writes: stars, images, cmp_stars, light_curves, and diagnostics.
  ? Debug tracer: --debug-star (and optional --debug-filter).
  ? Console progress bar for the parallel curve build (auto-enabled on TTY).
  ? FULLY PARALLELIZED differential photometry computation.
  ? Comprehensive progress tracking and statistics.

CLI examples:
  yuzu diffphot in.db out.db
  yuzu diffphot in.db out.db --broeg05-strict --debug-star 145 --debug-filter R -v
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

parser.add_argument("--max-cmp", type=int, default=5,
                    help="maximum number of comparison stars per target; use 0 to keep all passing candidates")

parser.add_argument("--min-cmp", type=int, default=5,
                    help="minimum number of valid comparison stars required per target time")

parser.add_argument("--robust", action="store_true",
                    help="use robust (MAD) dispersion instead of plain standard deviation")

parser.add_argument("--broeg05-strict", action="store_true",
                    help="use a strict Broeg+05-style artificial comparison star: complete comparison "
                         "stars only and inverse-sigma stability weights")

parser.add_argument("--broeg-convergence", type=float, default=1e-3,
                    help="relative weight-change threshold for strict Broeg+05 iterative convergence")

parser.add_argument("--broeg-max-iters", type=int, default=50,
                    help="maximum strict Broeg+05 weight-convergence iterations")

parser.add_argument("--weight-mode", choices=("inverse-variance", "inverse-sigma", "equal"),
                    default="inverse-variance",
                    help="how comparison-star stability sigmas are converted into synthetic-star weights")

parser.add_argument("--worst-fraction", type=float, default=0.25,
                    help="fraction of worst candidates to discard at each trimming step")

parser.add_argument("--min-candidate-epoch-fraction", type=float, default=1.0,
                    help="minimum fraction of target epochs covered by a comparison candidate")

parser.add_argument("--min-candidate-snr", type=float, default=0.0,
                    help="minimum median SNR required for a comparison candidate")

parser.add_argument("--max-candidate-sigma", type=float, default=float("inf"),
                    help="maximum initial leave-one-out comparison-star scatter in magnitudes")

parser.add_argument("--max-candidate-mag-diff", type=float, default=float("inf"),
                    help="maximum absolute instrumental-magnitude difference between target and comparison star")

parser.add_argument("--max-candidate-distance", type=float, default=float("inf"),
                    help="maximum detector-plane distance in pixels between target and comparison star")

parser.add_argument("--detrend-airmass", action="store_true",
                    help="after synthetic-star differential photometry, remove a robust linear airmass trend")

parser.add_argument("--diagnostics", action="store_true",
                    help="print per-filter precision diagnostics; per-star diagnostics are always written")

parser.add_argument("--precision-mode", action="store_true",
                    help="run the reproducible millimagnitude profile: strict Broeg+05, robust scatter, "
                         "complete high-SNR ensembles, and a precision acceptance report")
parser.add_argument("--lemon-mode", action="store_true",
                    help="run an upstream-LEMON-like profile: complete comparison stars, "
                         "Broeg+05 inverse-sigma weights, no extra candidate SNR gate, "
                         "and classic trimming defaults")
parser.add_argument("--precision-goal-rms", type=float, default=0.001,
                    help="RMS goal in magnitudes used by --precision-mode")
parser.add_argument("--precision-min-snr", type=float, default=1100.0,
                    help="minimum target and comparison median SNR for --precision-mode")
parser.add_argument("--precision-min-epochs", type=int, default=20,
                    help="minimum epochs for a curve to be assessed by --precision-mode")
parser.add_argument("--precision-max-excess-noise", type=float, default=1.5,
                    help="maximum observed/predicted scatter ratio for --precision-mode")
parser.add_argument("--fail-precision-gate", action="store_true",
                    help="return failure when --precision-mode produces no curve meeting its acceptance gate")

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
        self._interactive = bool(enabled and sys.stdout.isatty())
        self.count = 0
        self.start_time = time.time()
        self._last_update = 0
        self._last_render = ""
        self._last_logged_pct = -1

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
        if self._interactive:
            try:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
            except (OSError, UnicodeEncodeError):
                pass
        else:
            pct_int = int((self.count / max(1, self.total)) * 100.0)
            if self.count >= self.total or pct_int >= self._last_logged_pct + 5:
                print(line, flush=True)
                self._last_logged_pct = pct_int
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

        if self._interactive:
            try:
                sys.stdout.write("\r" + line)
                sys.stdout.flush()
            except (OSError, UnicodeEncodeError):
                pass
        else:
            print(line.rstrip(), flush=True)
        self._last_render = ""

    def clear_line(self) -> None:
        """Clear the current progress line."""
        if not self._interactive:
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
            print(text, flush=True)
            return
        if not self._interactive:
            print(text, flush=True)
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


def _sigma_weight(sd: float, mode: str) -> float:
    """Convert a comparison-star scatter into an ensemble weight coefficient."""
    if mode == "equal":
        return 1.0
    if not math.isfinite(sd) or sd <= 0.0:
        return 0.0
    if mode == "inverse-sigma":
        return 1.0 / sd
    return _invvar_weight(sd)


def _normalize_weights(w: np.ndarray) -> np.ndarray:
    """Return finite, non-negative weights normalized to one."""
    w = np.asarray(w, dtype=float)
    w[~np.isfinite(w)] = 0.0
    w[w < 0.0] = 0.0
    wsum = float(np.nansum(w))
    if wsum > 0:
        return w / wsum
    if w.size:
        return np.ones(w.size, dtype=float) / float(w.size)
    return w


def _max_relative_weight_change(old: np.ndarray, new: np.ndarray) -> float:
    """Maximum relative change between two normalized weight vectors."""
    old = np.asarray(old, dtype=float)
    new = np.asarray(new, dtype=float)
    mask = old > 0.0
    if not np.any(mask):
        return float("inf")
    return float(np.max(np.abs(new[mask] - old[mask]) / old[mask]))


def _flux_initial_weights(M: np.ndarray) -> np.ndarray:
    """Initial Broeg-like weights from median relative flux per comparison star."""
    if M.size == 0:
        return np.empty((0,), dtype=float)
    flux = np.power(10.0, -0.4 * M)
    flux[~np.isfinite(flux)] = np.nan
    median_flux = np.nanmedian(flux, axis=1)
    return _normalize_weights(median_flux)


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


def _broeg_iterative_weights(
    M: np.ndarray,
    robust: bool,
    weight_mode: str,
    convergence: float,
    max_iters: int,
    want_trace: bool = False,
):
    """Broeg+05-style iterative synthetic comparison star weights."""
    trace: List[dict] = []
    if M.size == 0:
        return np.empty((0,), dtype=float), np.empty((0,), dtype=float), trace

    nstars = M.shape[0]
    if nstars == 1:
        return np.ones(1, dtype=float), np.zeros(1, dtype=float), trace

    weights = _flux_initial_weights(M)
    if weights.size != nstars or not np.isfinite(weights).any() or float(weights.sum()) <= 0.0:
        weights = np.ones(nstars, dtype=float) / float(nstars)

    convergence = max(0.0, float(convergence))
    max_iters = max(1, int(max_iters))
    sigmas = np.full(nstars, float("inf"), dtype=float)

    for step in range(max_iters):
        for i in range(nstars):
            row = M[i, :]
            mask_i = np.isfinite(row)
            other = np.arange(nstars) != i
            other_w = weights[other]
            other_M = M[other, :]
            finite = np.isfinite(other_M)
            denom = np.sum(other_w[:, None] * finite, axis=0)
            numer = np.nansum(other_M * other_w[:, None], axis=0)
            valid = mask_i & (denom > 0.0)
            if valid.sum() < 2:
                sigmas[i] = float("inf")
                continue
            cmp_star = numer[valid] / denom[valid]
            sigmas[i] = _sigma(row[valid] - cmp_star, robust=robust)

        new_weights = _normalize_weights(np.array([_sigma_weight(sd, weight_mode) for sd in sigmas], dtype=float))
        change = _max_relative_weight_change(weights, new_weights)
        if want_trace:
            trace.append({
                "step": step,
                "broeg_sigmas": [float(s) for s in sigmas],
                "broeg_weights": [float(w) for w in new_weights],
                "max_relative_change": float(change),
            })
        weights = new_weights
        if math.isfinite(change) and change <= convergence:
            break

    return weights, sigmas, trace


def _median_snr_for_star(star_id: int, star_points: Dict[int, List[Tuple[float, float, float]]]) -> float:
    """Median SNR of one star over finite measurements."""
    vals = [float(s) for (_t, _m, s) in star_points.get(int(star_id), []) if s is not None and math.isfinite(float(s))]
    if not vals:
        return float("nan")
    return float(np.median(np.asarray(vals, dtype=float)))


def _median_mag_error_for_star(star_id: int, star_points: Dict[int, List[Tuple[float, float, float]]]) -> float:
    """Photon-noise magnitude error estimate from median SNR."""
    snr = _median_snr_for_star(int(star_id), star_points)
    if not math.isfinite(snr) or snr <= 0.0:
        return float("nan")
    return float(1.0857362047581296 / snr)


def _quality_filter_candidates(
    target_id: int,
    M: np.ndarray,
    cids: List[int],
    star_points: Dict[int, List[Tuple[float, float, float]]],
    star_info: Optional[Dict[int, Tuple[float, float, float]]],
    robust: bool,
    min_epoch_fraction: float,
    min_candidate_snr: float,
    max_candidate_sigma: float,
    max_candidate_mag_diff: float,
    max_candidate_distance: float,
):
    """Reject comparison candidates with poor coverage, SNR, or initial stability."""
    stats = {
        "initial": int(len(cids)),
        "kept": 0,
        "rejected_epoch_fraction": 0,
        "rejected_snr": 0,
        "rejected_sigma": 0,
        "rejected_mag_diff": 0,
        "rejected_distance": 0,
    }
    if M.size == 0 or not cids:
        return M, cids, np.empty((0,), dtype=float), stats

    min_epoch_fraction = min(1.0, max(0.0, float(min_epoch_fraction)))
    min_candidate_snr = max(0.0, float(min_candidate_snr))
    max_candidate_sigma = float(max_candidate_sigma)
    max_candidate_mag_diff = float(max_candidate_mag_diff)
    max_candidate_distance = float(max_candidate_distance)
    target_meta = (star_info or {}).get(int(target_id))

    sig0 = _leave_one_out_sigmas(M, robust=robust)
    keep: List[int] = []
    for i, cid in enumerate(cids):
        row = M[i, :]
        epoch_fraction = float(np.isfinite(row).sum()) / float(max(1, row.size))
        median_snr = _median_snr_for_star(int(cid), star_points)
        sigma0 = float(sig0[i]) if i < sig0.size else float("inf")

        if epoch_fraction < min_epoch_fraction:
            stats["rejected_epoch_fraction"] += 1
            continue
        if min_candidate_snr > 0.0 and (not math.isfinite(median_snr) or median_snr < min_candidate_snr):
            stats["rejected_snr"] += 1
            continue
        if math.isfinite(max_candidate_sigma) and sigma0 > max_candidate_sigma:
            stats["rejected_sigma"] += 1
            continue
        if target_meta is not None and math.isfinite(max_candidate_mag_diff):
            cand_meta = (star_info or {}).get(int(cid))
            if cand_meta is not None and abs(float(cand_meta[2]) - float(target_meta[2])) > max_candidate_mag_diff:
                stats["rejected_mag_diff"] += 1
                continue
        if target_meta is not None and math.isfinite(max_candidate_distance):
            cand_meta = (star_info or {}).get(int(cid))
            if cand_meta is not None:
                dx = float(cand_meta[0]) - float(target_meta[0])
                dy = float(cand_meta[1]) - float(target_meta[1])
                if math.hypot(dx, dy) > max_candidate_distance:
                    stats["rejected_distance"] += 1
                    continue
        keep.append(i)

    stats["kept"] = int(len(keep))
    if not keep:
        return np.empty((0, M.shape[1])), [], np.empty((0,), dtype=float), stats
    keep_arr = np.asarray(keep, dtype=int)
    return M[keep_arr, :], [cids[i] for i in keep], sig0[keep_arr], stats


def _predicted_diff_sigma(
    target_id: int,
    comparison_ids: List[int],
    comparison_weights: List[float],
    star_points: Dict[int, List[Tuple[float, float, float]]],
) -> float:
    """Expected differential magnitude scatter from target and synthetic-star SNR."""
    target_err = _median_mag_error_for_star(int(target_id), star_points)
    comp_terms: List[float] = []
    for cid, weight in zip(comparison_ids, comparison_weights):
        err = _median_mag_error_for_star(int(cid), star_points)
        if math.isfinite(err):
            comp_terms.append((float(weight) * err) ** 2)
    comp_err = math.sqrt(math.fsum(comp_terms)) if comp_terms else float("nan")
    if math.isfinite(target_err) and math.isfinite(comp_err):
        return float(math.sqrt(target_err * target_err + comp_err * comp_err))
    return target_err


def _series_stats(values: List[float]) -> dict:
    """RMS/MAD/P95 diagnostics for one differential light curve."""
    arr = np.asarray([float(v) for v in values if math.isfinite(float(v))], dtype=float)
    if arr.size == 0:
        return {"rms": float("nan"), "mad": float("nan"), "p95_abs": float("nan")}
    cen = float(np.median(arr))
    res = arr - cen
    return {
        "rms": float(np.sqrt(np.mean(res * res))),
        "mad": _mad_sigma(res),
        "p95_abs": float(np.percentile(np.abs(res), 95)),
    }


def _precision_assessment(diag: dict, goal_rms: float, min_snr: float,
                          min_epochs: int, max_excess_noise: float) -> Tuple[int, int, str]:
    """Classify a curve against the millimagnitude acceptance gate.

    Eligibility deliberately excludes faint, short, or incomplete curves.  A
    failed gate is therefore diagnostic evidence, not a claim that a variable
    astronomical source is bad data.
    """
    if int(diag.get("points", 0)) < max(2, int(min_epochs)):
        return 0, 0, "too_few_epochs"
    median_snr = float(diag.get("median_snr", float("nan")))
    if not math.isfinite(median_snr) or median_snr < float(min_snr):
        return 0, 0, "target_snr"

    rms = float(diag.get("rms", float("nan")))
    if not math.isfinite(rms):
        return 1, 0, "invalid_rms"
    if rms > float(goal_rms):
        return 1, 0, "rms"

    excess = float(diag.get("excess_noise_ratio", float("nan")))
    if math.isfinite(excess) and excess > float(max_excess_noise):
        return 1, 0, "excess_noise"
    return 1, 1, "pass"


def _robust_linear_airmass_detrend(
    points: List[Tuple[float, float, float]],
    airmass_by_time: Optional[Dict[float, Optional[float]]],
):
    """Remove a sigma-clipped linear airmass term while preserving median level."""
    if not airmass_by_time or len(points) < 5:
        return points, {"enabled": False, "slope": 0.0, "nfit": 0}

    xs: List[float] = []
    ys: List[float] = []
    idxs: List[int] = []
    for i, (t, mag, _snr) in enumerate(points):
        am = airmass_by_time.get(float(t))
        if am is None:
            continue
        try:
            x = float(am)
            y = float(mag)
        except (TypeError, ValueError):
            continue
        if math.isfinite(x) and math.isfinite(y):
            xs.append(x)
            ys.append(y)
            idxs.append(i)

    if len(xs) < 5 or (max(xs) - min(xs)) <= 1e-6:
        return points, {"enabled": False, "slope": 0.0, "nfit": len(xs)}

    x = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    x0 = float(np.median(x))
    xx = x - x0
    mask = np.ones(x.size, dtype=bool)
    slope = 0.0
    intercept = float(np.median(y))
    for _ in range(3):
        if mask.sum() < 5:
            break
        coeff = np.polyfit(xx[mask], y[mask], 1)
        slope = float(coeff[0])
        intercept = float(coeff[1])
        res = y - (slope * xx + intercept)
        sig = _mad_sigma(res[mask])
        if not math.isfinite(sig) or sig <= 0:
            break
        mask = np.abs(res) <= (4.0 * sig)

    corrected = list(points)
    for i, xv in zip(idxs, x):
        t, mag, snr = corrected[i]
        corrected[i] = (t, float(mag) - slope * (float(xv) - x0), snr)
    return corrected, {"enabled": True, "slope": slope, "intercept": intercept, "x0": x0, "nfit": int(mask.sum())}


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
            stdev     REAL,
            weight    REAL NOT NULL,
            FOREIGN KEY (star_id)   REFERENCES stars(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            FOREIGN KEY (cstar_id)  REFERENCES stars(id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS cstars_by_star_filter ON cmp_stars(star_id, filter_id);",
        """
        CREATE TABLE IF NOT EXISTS diffphot_diagnostics (
            id                       INTEGER PRIMARY KEY,
            star_id                  INTEGER NOT NULL,
            filter_id                INTEGER NOT NULL,
            epochs                   INTEGER NOT NULL,
            points                   INTEGER NOT NULL,
            candidate_initial        INTEGER NOT NULL,
            candidate_kept           INTEGER NOT NULL,
            candidate_rejected       INTEGER NOT NULL,
            rejected_epoch_fraction  INTEGER NOT NULL,
            rejected_snr             INTEGER NOT NULL,
            rejected_sigma           INTEGER NOT NULL,
            rejected_mag_diff        INTEGER NOT NULL DEFAULT 0,
            rejected_distance        INTEGER NOT NULL DEFAULT 0,
            final_cmp                INTEGER NOT NULL,
            rms                      REAL,
            mad                      REAL,
            p95_abs                  REAL,
            median_snr               REAL,
            predicted_sigma          REAL,
            excess_noise_ratio       REAL,
            weight_mode              TEXT NOT NULL,
            detrend_airmass          INTEGER NOT NULL,
            airmass_slope            REAL,
            airmass_nfit             INTEGER NOT NULL,
            precision_eligible       INTEGER NOT NULL DEFAULT 0,
            precision_pass           INTEGER NOT NULL DEFAULT 0,
            precision_failure         TEXT,
            created_unix             REAL NOT NULL,
            FOREIGN KEY (star_id) REFERENCES stars(id),
            FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
            UNIQUE (star_id, filter_id)
        );
        """,
        "CREATE INDEX IF NOT EXISTS diag_by_filter_rms ON diffphot_diagnostics(filter_id, rms);",
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
            existing = {
                str(row[1])
                for row in con.execute(text("PRAGMA table_info(diffphot_diagnostics)")).all()
            }
            extra_columns = {
                "rejected_mag_diff": "INTEGER NOT NULL DEFAULT 0",
                "rejected_distance": "INTEGER NOT NULL DEFAULT 0",
                "predicted_sigma": "REAL",
                "excess_noise_ratio": "REAL",
                "precision_eligible": "INTEGER NOT NULL DEFAULT 0",
                "precision_pass": "INTEGER NOT NULL DEFAULT 0",
                "precision_failure": "TEXT",
            }
            for name, decl in extra_columns.items():
                if name not in existing:
                    con.execute(text(f"ALTER TABLE diffphot_diagnostics ADD COLUMN {name} {decl}"))
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


def _collect_star_info(eng) -> Dict[int, Tuple[float, float, float]]:
    """Return star_id -> (x, y, instrumental magnitude) metadata."""
    out: Dict[int, Tuple[float, float, float]] = {}
    try:
        with eng.connect() as con:
            rows = con.execute(text("SELECT id, x, y, imag FROM stars")).all()
        for sid, x, y, imag in rows:
            out[int(sid)] = (float(x), float(y), float(imag))
    except Exception as e:
        logging.debug(f"Failed to collect star metadata: {e}")
    return out


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


def _collect_filter_airmass(in_eng, pfilter: str) -> Dict[float, Optional[float]]:
    """Return airmass keyed by exposure Unix time for one filter."""
    out: Dict[float, Optional[float]] = {}
    try:
        with in_eng.connect() as con:
            rows = con.execute(
                text(
                    "SELECT i.unix_time, i.airmass "
                    "FROM images AS i "
                    "JOIN photometric_filters AS f ON f.id = i.filter_id "
                    "WHERE f.name = :fname AND i.unix_time IS NOT NULL"
                ),
                {"fname": str(pfilter)},
            ).all()
        for t, am in rows:
            out[float(t)] = None if am is None else float(am)
    except Exception as e:
        logging.debug(f"Failed to collect airmass data for {pfilter}: {e}")
    return out


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
):
    """
    Build photometry matrix for target star.
    Returns:
      y      : target magnitudes aligned to T              (shape [T])
      M      : candidate magnitudes aligned to T           (shape [C, T])
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
        if not Tset.issubset(set(star_times.get(sid, []))):
            continue
        row = np.array([m_by_t[t] for t in T], dtype=float)
        if np.all(np.isfinite(row)):
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
    weight_mode: str,
    broeg_strict: bool,
    broeg_convergence: float,
    broeg_max_iters: int,
    want_trace: bool = False,
):
    """
    Iteratively trim worst comparison stars and compute weights.
    Returns: (keep_indices, sigmas, weights, trace)
    """
    trace: List[dict] = []

    if M.size == 0:
        return np.empty((0,), dtype=int), np.empty((0,)), np.empty((0,)), trace

    C0 = M.shape[0]
    keep = np.arange(C0, dtype=int)

    wf = float(worst_fraction)
    wf = min(1.0, max(0.0, wf))
    max_cmp = int(max_cmp)
    if max_cmp <= 0:
        max_cmp = keep.size
    else:
        max_cmp = max(1, max_cmp)
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

    if broeg_strict:
        w, sig_final, broeg_trace = _broeg_iterative_weights(
            M[keep, :],
            robust=robust,
            weight_mode=weight_mode,
            convergence=broeg_convergence,
            max_iters=broeg_max_iters,
            want_trace=want_trace,
        )
        if want_trace:
            trace.extend({"broeg": item} for item in broeg_trace)
    else:
        sig_final = _leave_one_out_sigmas(M[keep, :], robust=robust)
        w = _normalize_weights(np.array([_sigma_weight(sd, weight_mode) for sd in sig_final], dtype=float))

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
    airmass_by_time: Optional[Dict[float, Optional[float]]],
    star_info: Optional[Dict[int, Tuple[float, float, float]]],
    min_cmp: int,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    weight_mode: str,
    min_candidate_epoch_fraction: float,
    min_candidate_snr: float,
    max_candidate_sigma: float,
    max_candidate_mag_diff: float,
    max_candidate_distance: float,
    detrend_airmass: bool,
    broeg_strict: bool,
    broeg_convergence: float,
    broeg_max_iters: int,
    debug_star: Optional[int] = None,
):
    """
    Build differential light curve for one target star.
    Returns (target_id, payload) where payload contains:
      'cstars', 'cweights', 'cstdevs', 'points', and optionally 'debug'
    """
    y, M, cids, T = _matrix_for_target(target_id, star_points, star_times)
    if y.size == 0 or M.size == 0 or len(cids) == 0:
        payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": [],
                   "diagnostics": {"candidate_initial": len(cids), "candidate_kept": 0}}
        if debug_star is not None and int(target_id) == int(debug_star):
            payload["debug"] = {
                "filter": pfilter, "target": int(target_id),
                "epochs": len(T), "candidates": [],
                "note": "no usable candidate matrix",
            }
        return int(target_id), payload

    M, cids, sig0_kept, quality = _quality_filter_candidates(
        int(target_id), M, cids, star_points, star_info, robust=robust,
        min_epoch_fraction=min_candidate_epoch_fraction,
        min_candidate_snr=min_candidate_snr,
        max_candidate_sigma=max_candidate_sigma,
        max_candidate_mag_diff=max_candidate_mag_diff,
        max_candidate_distance=max_candidate_distance,
    )
    if M.size == 0 or len(cids) == 0:
        payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": [],
                   "diagnostics": {"candidate_initial": quality["initial"], "candidate_kept": 0,
                                   "candidate_rejected": quality["initial"]}}
        if debug_star is not None and int(target_id) == int(debug_star):
            payload["debug"] = {
                "filter": pfilter, "target": int(target_id),
                "epochs": len(T), "candidates": [],
                "quality": quality,
                "note": "all candidates rejected by quality filters",
            }
        return int(target_id), payload

    want_trace = (debug_star is not None and int(target_id) == int(debug_star))
    keep_idx, sigmas, weights, trace = _iterative_trim_and_weight(
        M, robust=robust, worst_fraction=worst_fraction, max_cmp=int(max_cmp),
        weight_mode=weight_mode, broeg_strict=broeg_strict,
        broeg_convergence=broeg_convergence, broeg_max_iters=broeg_max_iters,
        want_trace=want_trace
    )
    if keep_idx.size == 0:
        payload = {"cstars": [], "cweights": [], "cstdevs": [], "points": [],
                   "diagnostics": {"candidate_initial": quality["initial"],
                                   "candidate_kept": quality["kept"],
                                   "candidate_rejected": quality["initial"] - quality["kept"]}}
        if want_trace:
            payload["debug"] = {
                "filter": pfilter, "target": int(target_id),
                "epochs": len(T), "candidates": cids, "trace": trace,
                "quality": quality,
                "note": "kept set empty after trimming",
            }
        return int(target_id), payload

    sel_ids = [cids[i] for i in keep_idx.tolist()]
    sel_w   = weights.astype(float).tolist()
    sel_sd  = sigmas.astype(float).tolist()
    if len(sel_sd) == 1 and (not math.isfinite(float(sel_sd[0])) or float(sel_sd[0]) == 0.0):
        sel_sd = [None]

    # Weighted differential curve
    Msel = M[keep_idx, :]  # [C, T]
    points: List[Tuple[float, float, float]] = []
    tgt_snr = [s for (_t, _m, s) in star_points.get(target_id, [])]

    if Msel.shape[0] >= int(min_cmp):
        cmp_mean = np.dot(weights, Msel)  # [T]
        diff = y - cmp_mean
        for j, t in enumerate(T):
            snr = tgt_snr[j] if j < len(tgt_snr) else None
            points.append((float(t), float(diff[j]), (None if snr is None else float(snr))))

    detrend_info = {"enabled": False, "slope": 0.0, "nfit": 0}
    if detrend_airmass and points:
        points, detrend_info = _robust_linear_airmass_detrend(points, airmass_by_time)

    diff_stats = _series_stats([p[1] for p in points])
    median_target_snr = _median_snr_for_star(int(target_id), star_points)
    diagnostics = {
        "epochs": int(len(T)),
        "points": int(len(points)),
        "candidate_initial": int(quality["initial"]),
        "candidate_kept": int(quality["kept"]),
        "candidate_rejected": int(quality["initial"] - quality["kept"]),
        "rejected_epoch_fraction": int(quality["rejected_epoch_fraction"]),
        "rejected_snr": int(quality["rejected_snr"]),
        "rejected_sigma": int(quality["rejected_sigma"]),
        "rejected_mag_diff": int(quality["rejected_mag_diff"]),
        "rejected_distance": int(quality["rejected_distance"]),
        "final_cmp": int(len(sel_ids)),
        "rms": float(diff_stats["rms"]),
        "mad": float(diff_stats["mad"]),
        "p95_abs": float(diff_stats["p95_abs"]),
        "median_snr": float(median_target_snr),
        "predicted_sigma": float(_predicted_diff_sigma(int(target_id), sel_ids, sel_w, star_points)),
        "weight_mode": str(weight_mode),
        "broeg_strict": bool(broeg_strict),
        "detrend_airmass": bool(detrend_info.get("enabled", False)),
        "airmass_slope": float(detrend_info.get("slope", 0.0)),
        "airmass_nfit": int(detrend_info.get("nfit", 0)),
    }
    if math.isfinite(diagnostics["rms"]) and math.isfinite(diagnostics["predicted_sigma"]) and diagnostics["predicted_sigma"] > 0:
        diagnostics["excess_noise_ratio"] = float(diagnostics["rms"] / diagnostics["predicted_sigma"])
    else:
        diagnostics["excess_noise_ratio"] = float("nan")

    payload = {
        "cstars": sel_ids,
        "cweights": sel_w,
        "cstdevs": sel_sd,
        "points": points,
        "diagnostics": diagnostics,
    }

    if want_trace:
        sig0 = sig0_kept if sig0_kept.size == len(cids) else _leave_one_out_sigmas(M, robust=robust)
        payload["debug"] = {
            "filter": pfilter,
            "target": int(target_id),
            "epochs": len(T),
            "n_candidates": len(cids),
            "candidates": [{"id": int(cid), "sigma0": float(sig0[i])} for i, cid in enumerate(cids)],
            "quality": quality,
            "trace": trace,
            "final": {
                "cstars": sel_ids,
                "sigmas": sel_sd,
                "weights": sel_w,
                "sum_weights": float(sum(sel_w)),
                "weight_mode": str(weight_mode),
            },
            "diagnostics": diagnostics,
        }

    return int(target_id), payload


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
        if "broeg" in step:
            b = step["broeg"]
            out(f"{pre}  broeg iter {b.get('step')}: max_dw={_fmtf(b.get('max_relative_change', 0.0), 6)}")
            continue
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
    airmass_by_time: Optional[Dict[float, Optional[float]]],
    star_info: Optional[Dict[int, Tuple[float, float, float]]],
    min_cmp: int,
    robust: bool,
    worst_fraction: float,
    max_cmp: int,
    weight_mode: str,
    min_candidate_epoch_fraction: float,
    min_candidate_snr: float,
    max_candidate_sigma: float,
    max_candidate_mag_diff: float,
    max_candidate_distance: float,
    detrend_airmass: bool,
    broeg_strict: bool,
    broeg_convergence: float,
    broeg_max_iters: int,
    debug_star: Optional[int],
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
                target_id, pfilter, star_points, star_times, airmass_by_time, star_info,
                min_cmp, robust, worst_fraction, max_cmp, weight_mode,
                min_candidate_epoch_fraction, min_candidate_snr, max_candidate_sigma,
                max_candidate_mag_diff, max_candidate_distance,
                detrend_airmass, broeg_strict, broeg_convergence, broeg_max_iters,
                debug_star
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

    user_set_max_cmp = any(arg == "--max-cmp" or arg.startswith("--max-cmp=") for arg in argv)
    user_set_min_cmp = any(arg == "--min-cmp" or arg.startswith("--min-cmp=") for arg in argv)
    weight_mode = str(opts.weight_mode)
    robust = bool(opts.robust)
    max_cmp = int(opts.max_cmp)
    min_candidate_epoch_fraction = float(opts.min_candidate_epoch_fraction)
    max_candidate_mag_diff = float(opts.max_candidate_mag_diff)
    max_candidate_distance = float(opts.max_candidate_distance)
    broeg_strict = bool(opts.broeg05_strict)
    broeg_convergence = float(opts.broeg_convergence)
    broeg_max_iters = int(opts.broeg_max_iters)

    if opts.precision_goal_rms <= 0.0:
        parser.error("--precision-goal-rms must be positive")
    if opts.precision_min_snr <= 0.0:
        parser.error("--precision-min-snr must be positive")
    if opts.precision_min_epochs < 2:
        parser.error("--precision-min-epochs must be at least 2")
    if opts.precision_max_excess_noise <= 0.0:
        parser.error("--precision-max-excess-noise must be positive")

    if broeg_strict:
        weight_mode = "inverse-sigma"
        min_candidate_epoch_fraction = 1.0
        if not user_set_max_cmp:
            max_cmp = 5

    if opts.lemon_mode:
        # Approximate upstream LEMON defaults:
        # - complete comparison stars only
        # - Broeg-style inverse-sigma artificial comparison star
        # - no additional candidate-median-SNR gate
        # - classic trimming and pool-size defaults
        broeg_strict = True
        weight_mode = "inverse-sigma"
        min_candidate_epoch_fraction = 1.0
        opts.min_candidate_snr = 0.0
        if not user_set_max_cmp:
            max_cmp = 20
        if not user_set_min_cmp:
            opts.min_cmp = 8
        if not any(arg == "--worst-fraction" or arg.startswith("--worst-fraction=") for arg in argv):
            opts.worst_fraction = 0.10

    if opts.precision_mode:
        # Broeg et al. (2005) artificial comparison star is mandatory for
        # precision products.  Complete coverage avoids an epoch-dependent
        # ensemble, and robust scatter prevents isolated bad measurements
        # from controlling a star's stability weight.
        broeg_strict = True
        weight_mode = "inverse-sigma"
        robust = True
        if not user_set_max_cmp:
            max_cmp = 5
        min_candidate_epoch_fraction = 1.0
        if not user_set_min_cmp:
            # Precision mode already applies an explicit acceptance gate on the
            # finished curve. Keep any non-empty strict Broeg ensemble so small
            # fields are still measurable instead of silently discarding all
            # epochs behind the generic default of five comparison stars.
            opts.min_cmp = 1
        opts.min_snr = max(float(opts.min_snr), float(opts.precision_min_snr))
        opts.min_candidate_snr = max(float(opts.min_candidate_snr), float(opts.precision_min_snr))

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
        'precision_eligible': 0,
        'precision_pass': 0,
    }
    star_info = _collect_star_info(in_eng)

    for pf in sorted(filters):
        print(style.prefix)
        print(f"{style.prefix}Processing filter {pf} ...")

        star_points, star_times = _collect_filter_data(in_eng, pf, min_snr=float(opts.min_snr))
        airmass_by_time = _collect_filter_airmass(in_eng, pf) if opts.detrend_airmass else None
        nstars = len(star_points)
        print(f"{style.prefix}{nstars} stars with usable photometry.")
        print(f"{style.prefix}Synthetic comparison star: Broeg+05 ensemble | "
              f"weight_mode={weight_mode} | max_cmp={'all' if max_cmp <= 0 else max_cmp} | "
              f"iterative={'yes' if broeg_strict else 'no'}")
        if opts.precision_mode:
            print(f"{style.prefix}Precision gate: RMS <= {opts.precision_goal_rms:.4f} mag | "
                  f"SNR >= {opts.precision_min_snr:.0f} | epochs >= {opts.precision_min_epochs} | "
                  f"observed/predicted <= {opts.precision_max_excess_noise:.2f}")

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

        def collect_batch(batch_idx: int, batch_size: int, batch_results, batch_stats) -> None:
            nonlocal last_update_time
            results.extend(batch_results)

            for key in all_stats:
                all_stats[key] += batch_stats[key]

            progress.update(len(batch_results))

            current_time = time.time()
            if opts.verbose or (current_time - last_update_time >= 2.0):
                elapsed = current_time - start_time
                rate = all_stats['completed'] / elapsed if elapsed > 0 else 0

                msg = (f"{style.prefix}  Batch {batch_idx + 1}/{len(batches)} done | "
                       f"Stars: {all_stats['completed']}/{len(targets)} | "
                       f"Rate: {rate:.1f} stars/s | "
                       f"Avg candidates: {all_stats['candidates'] / max(1, all_stats['completed']):.1f}")

                if show_progress:
                    progress.print_above(msg)
                elif opts.verbose:
                    print(msg)
                last_update_time = current_time

        if workers == 1:
            # Avoid spawning a process for the explicitly serial mode. This is
            # required in restricted environments and removes needless IPC.
            for batch_idx, batch in enumerate(batches):
                try:
                    batch_results, batch_stats = _process_filter_batch(
                        pf, batch, star_points, star_times, airmass_by_time, star_info,
                        int(opts.min_cmp), robust, float(opts.worst_fraction), max_cmp,
                        weight_mode, min_candidate_epoch_fraction,
                        float(opts.min_candidate_snr), float(opts.max_candidate_sigma),
                        max_candidate_mag_diff, max_candidate_distance,
                        bool(opts.detrend_airmass), broeg_strict, broeg_convergence,
                        broeg_max_iters, dbg_star,
                    )
                    collect_batch(batch_idx, len(batch), batch_results, batch_stats)
                except Exception as e:
                    logging.error(f"Batch {batch_idx} processing failed: {e}")
                    progress.update(len(batch))
        else:
            # Use processes for CPU-bound curve construction when requested.
            with cf.ProcessPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _process_filter_batch,
                        pf, batch, star_points, star_times, airmass_by_time, star_info,
                        int(opts.min_cmp), robust, float(opts.worst_fraction), max_cmp,
                        weight_mode, min_candidate_epoch_fraction,
                        float(opts.min_candidate_snr), float(opts.max_candidate_sigma),
                        max_candidate_mag_diff, max_candidate_distance,
                        bool(opts.detrend_airmass), broeg_strict, broeg_convergence,
                        broeg_max_iters, dbg_star,
                    ): (batch_idx, len(batch))
                    for batch_idx, batch in enumerate(batches)
                }
                for future in cf.as_completed(futures):
                    batch_idx, batch_size = futures[future]
                    try:
                        batch_results, batch_stats = future.result()
                        collect_batch(batch_idx, batch_size, batch_results, batch_stats)
                    except Exception as e:
                        logging.error(f"Batch {batch_idx} processing failed: {e}")
                        progress.update(batch_size)

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
                                         "cstar_id": int(c),
                                         "stdev": (None if sd is None else float(sd)),
                                         "weight": float(w)})
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
                diag_rows: List[dict] = []
                created_unix = float(time.time())
                for sid, payload in results:
                    pts = payload["points"]
                    diag = payload.get("diagnostics") or {}
                    if opts.precision_mode:
                        eligible, passed, failure = _precision_assessment(
                            diag, opts.precision_goal_rms, opts.precision_min_snr,
                            opts.precision_min_epochs, opts.precision_max_excess_noise,
                        )
                    else:
                        eligible, passed, failure = 0, 0, None
                    diag_rows.append({
                        "star_id": int(sid),
                        "filter_id": int(f_id),
                        "epochs": int(diag.get("epochs", 0)),
                        "points": int(diag.get("points", len(pts))),
                        "candidate_initial": int(diag.get("candidate_initial", 0)),
                        "candidate_kept": int(diag.get("candidate_kept", 0)),
                        "candidate_rejected": int(diag.get("candidate_rejected", 0)),
                        "rejected_epoch_fraction": int(diag.get("rejected_epoch_fraction", 0)),
                        "rejected_snr": int(diag.get("rejected_snr", 0)),
                        "rejected_sigma": int(diag.get("rejected_sigma", 0)),
                        "rejected_mag_diff": int(diag.get("rejected_mag_diff", 0)),
                        "rejected_distance": int(diag.get("rejected_distance", 0)),
                        "final_cmp": int(diag.get("final_cmp", 0)),
                        "rms": None if not math.isfinite(float(diag.get("rms", float("nan")))) else float(diag.get("rms")),
                        "mad": None if not math.isfinite(float(diag.get("mad", float("nan")))) else float(diag.get("mad")),
                        "p95_abs": None if not math.isfinite(float(diag.get("p95_abs", float("nan")))) else float(diag.get("p95_abs")),
                        "median_snr": None if not math.isfinite(float(diag.get("median_snr", float("nan")))) else float(diag.get("median_snr")),
                        "predicted_sigma": None if not math.isfinite(float(diag.get("predicted_sigma", float("nan")))) else float(diag.get("predicted_sigma")),
                        "excess_noise_ratio": None if not math.isfinite(float(diag.get("excess_noise_ratio", float("nan")))) else float(diag.get("excess_noise_ratio")),
                        "weight_mode": str(diag.get("weight_mode", weight_mode)),
                        "detrend_airmass": 1 if bool(diag.get("detrend_airmass", False)) else 0,
                        "airmass_slope": float(diag.get("airmass_slope", 0.0)),
                        "airmass_nfit": int(diag.get("airmass_nfit", 0)),
                        "precision_eligible": int(eligible),
                        "precision_pass": int(passed),
                        "precision_failure": failure,
                        "created_unix": created_unix,
                    })
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
                if diag_rows:
                    con.execute(text(
                        "INSERT OR REPLACE INTO diffphot_diagnostics "
                        "(star_id, filter_id, epochs, points, candidate_initial, candidate_kept, "
                        "candidate_rejected, rejected_epoch_fraction, rejected_snr, rejected_sigma, "
                        "rejected_mag_diff, rejected_distance, final_cmp, rms, mad, p95_abs, "
                        "median_snr, predicted_sigma, excess_noise_ratio, weight_mode, "
                        "detrend_airmass, airmass_slope, airmass_nfit, precision_eligible, "
                        "precision_pass, precision_failure, created_unix) "
                        "VALUES (:star_id, :filter_id, :epochs, :points, :candidate_initial, "
                        ":candidate_kept, :candidate_rejected, :rejected_epoch_fraction, "
                        ":rejected_snr, :rejected_sigma, :rejected_mag_diff, :rejected_distance, "
                        ":final_cmp, :rms, :mad, :p95_abs, :median_snr, :predicted_sigma, "
                        ":excess_noise_ratio, :weight_mode, :detrend_airmass, :airmass_slope, "
                        ":airmass_nfit, :precision_eligible, :precision_pass, :precision_failure, "
                        ":created_unix)"
                    ), diag_rows)

            n_written = sum(1 for _sid, p in results if p["points"])
            write_time = time.time() - write_start

            # Update global statistics
            global_stats['filters_processed'] += 1
            global_stats['total_curves'] += n_written
            global_stats['total_measurements'] += total_measurements
            global_stats['total_comparison_stars'] += sum(len(p.get('cstars', [])) for _, p in results)
            global_stats['failed_curves'] += all_stats['failed']
            if opts.precision_mode:
                eligible_count = sum(int(row['precision_eligible']) for row in diag_rows)
                pass_count = sum(int(row['precision_pass']) for row in diag_rows)
                global_stats['precision_eligible'] += eligible_count
                global_stats['precision_pass'] += pass_count
                print(f"{style.prefix}Precision gate: {pass_count}/{eligible_count} eligible curves meet "
                      f"{opts.precision_goal_rms:.4f} mag RMS.")

            print(f"{style.prefix}{n_written} curves written for filter {pf}.")
            print(f"{style.prefix}Database write time: {write_time:.1f} seconds")
            if total_measurements > 0:
                print(f"{style.prefix}Write rate: {total_measurements / write_time:.0f} measurements/second")

            rms_vals = np.asarray([
                float((p.get("diagnostics") or {}).get("rms", float("nan")))
                for _sid, p in results
            ], dtype=float)
            rms_vals = rms_vals[np.isfinite(rms_vals)]
            mad_vals = np.asarray([
                float((p.get("diagnostics") or {}).get("mad", float("nan")))
                for _sid, p in results
            ], dtype=float)
            mad_vals = mad_vals[np.isfinite(mad_vals)]
            excess_vals = np.asarray([
                float((p.get("diagnostics") or {}).get("excess_noise_ratio", float("nan")))
                for _sid, p in results
            ], dtype=float)
            excess_vals = excess_vals[np.isfinite(excess_vals)]
            if opts.diagnostics and rms_vals.size:
                best10 = float(np.percentile(rms_vals, 10))
                print(f"{style.prefix}Precision diagnostics for filter {pf}:")
                print(f"{style.prefix}  Median RMS: {float(np.median(rms_vals)):.5f} mag")
                if mad_vals.size:
                    print(f"{style.prefix}  Median robust scatter: {float(np.median(mad_vals)):.5f} mag")
                if excess_vals.size:
                    print(f"{style.prefix}  Median observed/predicted noise: {float(np.median(excess_vals)):.2f}x")
                print(f"{style.prefix}  Best 10% RMS threshold: {best10:.5f} mag")
                print(f"{style.prefix}  Curves <= 0.010 mag: {int((rms_vals <= 0.010).sum())}/{rms_vals.size}")
                print(f"{style.prefix}  Curves <= 0.003 mag: {int((rms_vals <= 0.003).sum())}/{rms_vals.size}")
                print(f"{style.prefix}  Curves <= 0.001 mag: {int((rms_vals <= 0.001).sum())}/{rms_vals.size}")
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
                {"k": "hostname", "v": socket.gethostname()},
                {"k": "diffphot_algorithm", "v": "Broeg+05 synthetic comparison star"},
                {"k": "diffphot_broeg05_strict", "v": int(broeg_strict)},
                {"k": "diffphot_broeg_convergence", "v": broeg_convergence},
                {"k": "diffphot_broeg_max_iters", "v": broeg_max_iters},
                {"k": "diffphot_weight_mode", "v": weight_mode},
                {"k": "diffphot_robust", "v": int(robust)},
                {"k": "diffphot_max_cmp", "v": max_cmp},
                {"k": "diffphot_min_candidate_epoch_fraction", "v": min_candidate_epoch_fraction},
                {"k": "diffphot_min_candidate_snr", "v": float(opts.min_candidate_snr)},
                {"k": "diffphot_max_candidate_sigma", "v": float(opts.max_candidate_sigma)},
                {"k": "diffphot_max_candidate_mag_diff", "v": max_candidate_mag_diff},
                {"k": "diffphot_max_candidate_distance", "v": max_candidate_distance},
                {"k": "diffphot_detrend_airmass", "v": int(bool(opts.detrend_airmass))},
                {"k": "diffphot_precision_mode", "v": int(bool(opts.precision_mode))},
                {"k": "diffphot_precision_goal_rms", "v": float(opts.precision_goal_rms)},
                {"k": "diffphot_precision_min_snr", "v": float(opts.precision_min_snr)},
                {"k": "diffphot_precision_min_epochs", "v": int(opts.precision_min_epochs)},
                {"k": "diffphot_precision_max_excess_noise", "v": float(opts.precision_max_excess_noise)}])

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

    if opts.precision_mode:
        print(f"{style.prefix}Precision acceptance: {global_stats['precision_pass']}/"
              f"{global_stats['precision_eligible']} eligible curves pass <= "
              f"{opts.precision_goal_rms:.4f} mag RMS.")

    if global_time > 0:
        print(f"{style.prefix}Overall processing rate: {global_stats['total_stars'] / global_time:.1f} stars/second")

    print(f"{style.prefix}Output database: {out_path}")
    print(f"{style.prefix}{'=' * 60}")
    print(style.prefix)
    print(f"{style.prefix}You're done ^_^")
    if opts.fail_precision_gate and opts.precision_mode and global_stats['precision_pass'] == 0:
        print(f"{style.prefix}Error. No eligible curve passed precision gate.")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
