#!/usr/bin/env python3
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Dict, Any

"""
LEMON data-mining helpers.

This module implements several data mining and data analysis algorithms to
identify potentially interesting objects, such as those whose light curves are
strongly correlated across photometric filters or stars with the lowest light
curve standard deviation (i.e., the most constant).

It builds on top of the read-only `database.LEMONdB` interface and memoizes
expensive calls there to avoid redundant I/O/CPU work.
"""

import collections
import datetime
import itertools
import operator
import functools

import numpy as np
from scipy.stats import linregress

# Project modules (prefer relative import; fall back to absolute if needed)
try:
    from . import database, util
except Exception:  # pragma: no cover
    import database  # type: ignore
    import util      # type: ignore

__all__ = ["NoStarsSelectedError", "LEMONdBMiner"]

logger = logging.getLogger(__name__)


class NoStarsSelectedError(ValueError):
    """Raised when no stars satisfy the selection criteria in LEMONdBMiner."""


class LEMONdBMiner(database.LEMONdB):
    """
    Read-only, memoized interface to identify potentially interesting stars.

    Databases accessed through this class are expected to be read-only; methods
    that are heavy in I/O/CPU in the parent class are memoized. Mutating the
    underlying LEMONdB concurrently would risk stale reads.
    """

    # ---- Memoized pass-throughs to the base database API --------------------
    @functools.lru_cache(maxsize=None)
    def get_star(self, *args):
        return super(LEMONdBMiner, self).get_star(*args)

    @functools.lru_cache(maxsize=None)
    def get_light_curve(self, *args):
        return super(LEMONdBMiner, self).get_light_curve(*args)

    @functools.lru_cache(maxsize=None)
    def get_period(self, *args):
        return super(LEMONdBMiner, self).get_period(*args)

    # ---- Utilities -----------------------------------------------------------
    @staticmethod
    def _ascii_table(
        headers: Sequence[str],
        table_rows: Sequence[Sequence[Any]],
        *,
        sort_index: Optional[int] = 1,
        descending: bool = True,
        ndecimals: int = 8,
        dates_columns: Optional[Sequence[int]] = None,
    ) -> str:
        """
        Format `table_rows` as a simple ASCII table.

        Notes
        -----
        - A leftmost index column is automatically added (starting at 0).
        - `dates_columns` contains column indexes (0-based *in the input rows*)
          that represent durations in seconds and should be rendered as
          timedeltas (e.g., '0:11:06' or '17 days, 6:12:00').
        - `sort_index` is applied to the *input* columns (not counting the index
          column we add).
        """
        dates_columns = tuple(dates_columns or ())
        rows = [tuple(r) for r in table_rows]  # avoid mutating caller data

        if not rows:
            # produce just a header-only table
            effective_columns = [""] + list(headers)
            header_str = "|".join(s.center(len(s) + 2) for s in effective_columns)
            return header_str + "\n" + "-" * len(header_str) + "\n"

        ncols = len(rows[0])
        if any(len(r) != ncols for r in rows):
            raise ValueError("All rows must have the same number of columns.")
        if len(headers) != ncols:
            raise ValueError("Headers length must match the number of columns.")

        # Sort copy of rows by requested input column (if any)
        if sort_index is not None:
            rows = sorted(rows, key=operator.itemgetter(sort_index), reverse=descending)

        # Prepare a 2D dict of strings: table_data[row_idx][col_idx]
        table_data: Dict[int, Dict[int, str]] = collections.defaultdict(dict)

        # Top-left corner cell is empty; first row are headers (shifted by 1)
        table_data[0][0] = ""
        for c, name in enumerate(headers, start=1):
            table_data[0][c] = str(name)

        # Data rows: first column is the index (as string)
        for r_idx, row in enumerate(rows, start=1):
            table_data[r_idx][0] = str(r_idx - 1)
            for c_idx, value in enumerate(row, start=1):
                if value is None:
                    cell = ""
                elif (c_idx - 1) in dates_columns:
                    try:
                        cell = str(datetime.timedelta(seconds=float(value)))
                    except Exception:
                        cell = str(value)
                elif isinstance(value, float):
                    cell = f"{value:.{ndecimals}f}"
                else:
                    cell = str(value)
                table_data[r_idx][c_idx] = cell

        # Column widths (with padding), and data widths (exclude header)
        widths: Dict[int, int] = {}
        data_widths: Dict[int, int] = {}
        for c in table_data[0].keys():
            col_vals = [table_data[r].get(c, "") for r in table_data.keys()]
            lengths = [len(str(x)) for x in col_vals]
            widths[c] = (max(lengths) if lengths else 0) + 2
            data_only = [len(str(x)) for x in col_vals[1:]] or [0]
            data_widths[c] = max(data_only)

        # Header row
        header_values = [table_data[0][c] for c in sorted(table_data[0])]
        header_str = "|".join(
            s.center(widths[c]) for c, s in enumerate(header_values)
        )
        out = header_str + "\n" + "-" * len(header_str) + "\n"

        # Data rows
        for r in sorted(k for k in table_data.keys() if k != 0):
            row_vals = [table_data[r][c] for c in sorted(table_data[r])]
            # First cell (index) is left-justified within data width, centered overall
            left = row_vals[0].ljust(data_widths[0]).center(widths[0])
            out += left + "|"
            # Remaining cells right-justified within data width, centered overall
            rendered = []
            for c, s in enumerate(row_vals[1:], start=1):
                rendered.append(s.rjust(data_widths[c]).center(widths[c]))
            out += "|".join(rendered) + "\n"

        return out

    # ---- Analyses ------------------------------------------------------------
    def sort_by_period_similarity(
        self,
        minimum: int = 2,
        normalization: str = "max",
    ) -> List[Tuple[int, float]]:
        """
        Sort stars by the similarity (low scatter) of their periods across filters.

        The similarity is measured as the standard deviation of per-filter
        periods after normalizing them for each star.

        Parameters
        ----------
        minimum : int
            Require at least this many known periods for the star.
        normalization : {"max", "median", "mean"}
            Normalization applied to the vector of periods before computing stdev.

        Returns
        -------
        list[tuple[star_id, stdev]]
            Sorted ascending by stdev.

        Raises
        ------
        ValueError
            If `minimum < 2` or `normalization` is unknown.
        NoStarsSelectedError
            If no stars satisfy the constraints.
        """
        if minimum < 2:
            raise ValueError("A minimum of at least two periods is required.")

        if normalization == "max":
            norm_func = np.max
        elif normalization == "mean":
            norm_func = np.mean
        elif normalization == "median":
            norm_func = np.median
        else:
            raise ValueError("Unrecognized normalization method: %r" % normalization)

        periods_stdevs: List[Tuple[int, float]] = []
        for star_id in self.star_ids:
            star_periods = self.get_periods(star_id)  # type: ignore[attr-defined]
            if len(star_periods) < minimum:
                continue

            normalized = star_periods / norm_func(star_periods)
            stdev = float(np.std(normalized, dtype=self.dtype))  # dtype from base class
            periods_stdevs.append((star_id, stdev))

        if not periods_stdevs:
            raise NoStarsSelectedError(f"no stars with at least {minimum} known periods")

        return sorted(periods_stdevs, key=operator.itemgetter(1))

    def period_similarity(
        self,
        how_many: int,
        *,
        minimum: int = 2,
        normalization: str = "max",
        ndecimals: int = 8,
    ) -> str:
        """
        Return an ASCII table with the `how_many` stars with the most similar periods.

        See `sort_by_period_similarity` for details.
        """
        most_similar = self.sort_by_period_similarity(
            normalization=normalization, minimum=minimum
        )[:how_many]

        header: List[str] = ["Star", "Stdev. Norm."] + [f"Period {pf.letter}" for pf in self.pfilters]

        table_rows: List[List[Optional[float]]] = []
        for star_id, stdev in most_similar:
            row: List[Optional[float]] = [star_id, stdev]
            for pf in self.pfilters:
                period = self.get_period(star_id, pf)
                if period is None:
                    row.append(None)
                else:
                    period_secs, _step_secs = period
                    row.append(period_secs)
            table_rows.append(row)

        # NB: the positions 2,3,4,... with durations depend on number of filters;
        # we mark all those 'Period *' columns as date-like (seconds).
        date_cols = tuple(range(2, 2 + len(self.pfilters)))
        return self._ascii_table(
            header,
            table_rows,
            sort_index=1,
            descending=False,
            ndecimals=ndecimals,
            dates_columns=date_cols,
        )

    def match_bands(
        self,
        star_id: int,
        first_pfilter,
        second_pfilter,
        *,
        delta: float = 3600 * 1.5,
    ) -> Optional[List[Tuple[float, float]]]:
        """
        Pair magnitudes from two light curves of the same star (nearest in time).

        Only pairs with |Δt| < delta seconds are retained.

        Returns
        -------
        list[(mag1, mag2)] or None
        """
        try:
            phot1_points = list(self.get_light_curve(star_id, first_pfilter))
            phot2_points = list(self.get_light_curve(star_id, second_pfilter))
        except TypeError:
            return None  # no curve in one/both filters

        if not phot1_points or not phot2_points:
            return None

        matches: List[Tuple[float, float]] = []
        for t1, mag1, _snr1 in phot1_points:
            # Find closest time in second curve (ValueError on empty guarded above)
            t2, mag2, _snr2 = min(phot2_points, key=lambda x: abs(t1 - x[0]))
            if abs(t1 - t2) < delta:
                matches.append((mag1, mag2))
        return matches

    def star_correlation(
        self,
        star_id: int,
        first_pfilter,
        second_pfilter,
        *,
        min_matches: int = 10,
        delta: float = 3600 * 1.5,
    ) -> Optional[Tuple[int, float, float, int]]:
        """
        Correlate two filters for a star using least-squares regression.

        Returns
        -------
        (star_id, slope, r_value, n_matches) or None
        """
        matches = self.match_bands(star_id, first_pfilter, second_pfilter, delta=delta)
        if not matches or len(matches) < min_matches:
            return None

        x, y = zip(*matches)
        slope, _intercept, r_value, _p_value, _std_err = linregress(x, y)
        return star_id, float(slope), float(r_value), len(matches)

    def band_correlation(
        self,
        how_many: int,
        *,
        sort_index: int = 0,
        delta: float = 3600 * 1.5,
        min_matches: int = 10,
        ndecimals: int = 8,
    ) -> str:
        """
        ASCII table with the most correlated stars across filter pairs.

        The table includes, for each pair of filters, the correlation coefficient
        and the number of matched points used. Stars are selected by taking the
        top-`how_many` using the `sort_index`-th filter-pair as the primary key.
        """
        pf_pairs = list(itertools.combinations(self.pfilters, 2))
        if not pf_pairs:
            raise NoStarsSelectedError("database has fewer than two photometric filters")

        if not (0 <= sort_index < len(pf_pairs)):
            raise ValueError(f"sort_index out of range (0..{len(pf_pairs)-1})")

        main_pair = pf_pairs[sort_index]
        main_corr: List[Tuple[int, float, int]] = []  # (id, r, n)

        for sid in self.star_ids:
            res = self.star_correlation(sid, *main_pair, min_matches=min_matches, delta=delta)
            if res:
                _sid, _slope, r, n = res
                main_corr.append((sid, r, n))

        if not main_corr:
            a, b = main_pair
            raise NoStarsSelectedError(
                f"no stars with at least {min_matches} matched points in {a}-{b}"
            )

        main_corr.sort(key=operator.itemgetter(1), reverse=True)
        top = main_corr[:how_many]
        top_ids = [sid for sid, _r, _n in top]

        # correlations[ pair ][ star_id ] = (r_value or None, n_matches or None)
        correlations: Dict[Tuple[Any, Any], Dict[int, Tuple[Optional[float], Optional[int]]]] = \
            collections.defaultdict(dict)

        for sid, r, n in top:
            correlations[main_pair][sid] = (float(r), int(n))

        # Compute correlations for other pairs only for selected stars
        for idx, pair in enumerate(pf_pairs):
            if idx == sort_index:
                continue
            for sid in top_ids:
                res = self.star_correlation(sid, *pair, min_matches=min_matches, delta=delta)
                if res:
                    _sid, _slope, r, n = res
                    correlations[pair][sid] = (float(r), int(n))
                else:
                    correlations[pair][sid] = (None, None)

        # Build table
        header: List[str] = ["Star"]
        for a, b in pf_pairs:
            tag = f"{a.letter}-{b.letter}"
            header.append(f"r-value {tag}")
            header.append("Matches")

        rows: List[List[Optional[float]]] = []
        for sid in top_ids:
            row: List[Optional[float]] = [sid]
            for pair in pf_pairs:
                r, n = correlations[pair][sid]
                row.append(r)
                row.append(n)
            rows.append(row)

        return self._ascii_table(header, rows, sort_index=None, descending=True, ndecimals=ndecimals)

    @staticmethod
    def dump(path: str | Path, data: Iterable[Iterable[float]], *, decimals: int = 8) -> None:
        """
        Dump rows of floats to a tab-separated text file (overwrites if exists).
        """
        p = Path(path)
        with p.open("w", encoding="utf-8") as fd:
            for row in data:
                fd.write("\t".join(f"{val:.{decimals}f}" for val in row) + "\n")

    def sort_by_curve_stdev(
        self,
        pfilter,
        *,
        minimum: int = 10,
    ) -> List[Tuple[int, float]]:
        """
        Sort stars by the standard deviation of their light curves in `pfilter`.

        Returns
        -------
        list[(star_id, stdev)] sorted ascending by stdev.
        """
        out: List[Tuple[int, float]] = []
        for sid in self.star_ids:
            curve = self.get_light_curve(sid, pfilter)
            if curve is None or len(curve) < minimum:
                continue
            out.append((sid, float(curve.stdev)))

        if not out:
            raise NoStarsSelectedError(f"no light curves with at least {minimum} points in {pfilter}")

        return sorted(out, key=operator.itemgetter(1))

    def curve_stdev(
        self,
        how_many: int,
        *,
        sort_index: int = 0,
        minimum: int = 10,
        ndecimals: int = 8,
    ) -> str:
        """
        Return an ASCII table with the `how_many` most constant stars.

        Stars are selected by smallest stdev in the `sort_index`-th filter
        (filters are ordered by effective wavelength). Rows include the stdev
        across all filters for those stars (or empty if no curve/too few points).
        """
        main_pf = self.pfilters[sort_index]
        best = self.sort_by_curve_stdev(main_pf, minimum=minimum)[:how_many]
        top_ids = [sid for sid, _stdev in best]

        stdevs: Dict[Any, Dict[int, Optional[float]]] = collections.defaultdict(dict)
        for sid, stdev in best:
            stdevs[main_pf][sid] = stdev

        for idx, pf in enumerate(self.pfilters):
            if idx == sort_index:
                continue
            for sid in top_ids:
                curve = self.get_light_curve(sid, pf)
                if curve is None or len(curve) < minimum:
                    stdevs[pf][sid] = None
                else:
                    stdevs[pf][sid] = float(curve.stdev)

        header = ["Star"] + [f"{pf} Stdev" for pf in self.pfilters]
        rows: List[List[Optional[float]]] = []
        for sid in top_ids:
            row: List[Optional[float]] = [sid]
            for pf in self.pfilters:
                row.append(stdevs[pf].get(sid))
            rows.append(row)

        # +1 because first displayed column is the index we add, then 'Star'
        return self._ascii_table(
            header,
            rows,
            sort_index=sort_index + 1,
            descending=False,
            ndecimals=ndecimals,
        )

    def amplitudes_by_wavelength(
        self,
        increasing: bool,
        npoints: int,
        use_median: bool,
        exclude_noisy: bool,
        noisy_nstdevs: int,
        noisy_use_median: bool,
        noisy_min_ratio: float,
    ) -> Iterator[Optional[Tuple[int, List[Tuple[Any, float, Optional[float]]]]]]:
        """
        Iterate over stars whose peak-to-peak amplitudes are monotonic with wavelength.

        Yields
        ------
        - `(star_id, [(Passband, amplitude, cmp_stdev_or_None), ...])` for stars
          that pass the criteria, otherwise `None` (useful for progress tracking).

        Notes
        -----
        - Amplitude is defined as (median/mean of the `npoints` highest magnitudes)
          minus (median/mean of the `npoints` lowest magnitudes) depending on
          `use_median`.
        - If `exclude_noisy` is True, amplitudes must exceed
          `noisy_min_ratio * S`, where `S` is the median/mean (depending on
          `noisy_use_median`) stdev of `noisy_nstdevs` stars with most similar
          magnitude that *do* have a light curve in that filter.
        """
        pfilters = sorted(self.pfilters, reverse=not increasing)
        amp_kwargs = dict(npoints=npoints, median=use_median)

        for sid in self.star_ids:
            discarded = False
            amps: List[float] = []
            cmp_stdevs: List[Optional[float]] = []

            for pf in pfilters:
                curve = self.get_light_curve(sid, pf)
                if not curve:
                    discarded = True
                    break

                amp = float(curve.amplitude(**amp_kwargs))
                amps.append(amp)

                if exclude_noisy:
                    # IDs of `noisy_nstdevs` most similar-magnitude stars (with curves)
                    similar_ids: List[int] = []
                    g = self.most_similar_magnitude(sid, pf)  # generator of (id, diff, ...)
                    try:
                        for _ in range(noisy_nstdevs):
                            similar_ids.append(next(g)[0])  # type: ignore[index]
                    except StopIteration:
                        pass

                    if len(similar_ids) != noisy_nstdevs:
                        discarded = True
                        break

                    stdevs = [self.get_light_curve(i, pf).stdev for i in similar_ids]
                    agg = np.median if noisy_use_median else np.mean
                    s_ref = float(agg(stdevs))
                    cmp_stdevs.append(s_ref)
                    if s_ref == 0 or (amp / s_ref) < noisy_min_ratio:
                        discarded = True
                        break
                else:
                    cmp_stdevs.append(None)

            if not discarded and amps == sorted(amps):
                yield sid, list(zip(pfilters, amps, cmp_stdevs))
            else:
                yield None


# Default logger config (only if the root hasn't been configured)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
