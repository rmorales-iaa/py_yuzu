#!/usr/bin/env python3
from __future__ import annotations

import functools
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

__all__ = ["NoStarsSelectedError", "PFilter", "LEMONdBMiner"]


class NoStarsSelectedError(ValueError):
    """Raised when no stars satisfy the selection criteria in LEMONdBMiner."""


@dataclass(frozen=True)
class PFilter:
    id: int
    name: str  # e.g., "R", "UNKNOWN"


class LEMONdBMiner:
    """
    Read-only miner for a LEMON-like SQLite database.

    Robust to DBs that lack a `photometry` table:
    - If `photometry` is missing but `light_curves` exists, we auto-create a
      compatibility layer:
        * Prefer a VIEW `photometry` mapped from `light_curves` (with an `id`)
        * Fallback to MATERIALIZE a real `photometry` TABLE populated from `light_curves`
      This keeps downstream UI/queries that rely on `photometry` working.

    Public surface:
      - .pfilters -> List[PFilter]
      - .star_ids -> List[int]
      - .get_star(star_id) -> dict with at least {ra, dec, imag}
      - .get_light_curve(star_id, pfilter) -> List[(unix_time, magnitude, snr)]
      - .get_cmp_stars(star_id, pfilter=None) -> comparison stars for a star/filter
      - .field_name -> file name of the DB (for status bar)
    """

    # ------------------------- lifecycle -------------------------

    def __init__(self, db_path: Union[str, Path]):
        self.db_path = str(db_path)
        self._conn: sqlite3.Connection = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row

        # Ensure compatibility when photometry is absent
        self._ensure_photometry_compat()

        self._has_photometry = self._table_has_rows("photometry")
        self._has_light_curves = self._table_has_rows("light_curves")

        self._pfilters: List[PFilter] = []
        self._star_ids: List[int] = []
        self._filter_by_id: Dict[int, PFilter] = {}
        self._filter_by_name: Dict[str, PFilter] = {}

        self.field_name = Path(self.db_path).name
        self._build_index()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # -------------------------- helpers --------------------------

    def _table_or_view_exists(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (name,),
        )
        return cur.fetchone() is not None

    def _table_exists(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
        return cur.fetchone() is not None

    def _view_exists(self, name: str) -> bool:
        cur = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view' AND name=?",
            (name,),
        )
        return cur.fetchone() is not None

    def _table_has_rows(self, name: str) -> bool:
        if not self._table_or_view_exists(name):
            return False
        try:
            cur = self._conn.execute(f"SELECT 1 FROM {name} LIMIT 1")
            return cur.fetchone() is not None
        except Exception:
            return False

    def _sqlite_version_tuple(self) -> Tuple[int, int, int]:
        v = self._conn.execute("SELECT sqlite_version()").fetchone()[0]
        parts = []
        for tok in str(v).split("."):
            try:
                parts.append(int(tok))
            except Exception:
                parts.append(0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])  # type: ignore[return-value]

    def _ensure_photometry_compat(self) -> None:
        """
        If `photometry` doesn't exist but `light_curves` does, create a
        compatibility layer so code that expects `photometry` keeps working.

        Preference:
          1) Create a VIEW `photometry` selecting from `light_curves` with a ROW_NUMBER() id
             (requires SQLite >= 3.25).
          2) Else materialize a TABLE `photometry` and bulk-copy from `light_curves`.
        """
        has_photometry = self._table_or_view_exists("photometry")
        has_light_curves = self._table_or_view_exists("light_curves")
        if has_photometry or not has_light_curves:
            return  # nothing to do

        major, minor, patch = self._sqlite_version_tuple()
        can_row_number = (major, minor, patch) >= (3, 25, 0)

        try:
            if can_row_number:
                # Try to create a compatibility VIEW with an id
                self._conn.execute("DROP VIEW IF EXISTS photometry")
                self._conn.execute(
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
                )
                # Optional: create indexes on the underlying tables already exist;
                # view indices aren't a thing, but keeping parity with names helps external code.
                logger.info("Created compatibility VIEW 'photometry' from 'light_curves'.")
            else:
                # Materialize a real table with sane schema and indexes
                self._conn.execute(
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
                )
                # Populate from light_curves
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO photometry (star_id, image_id, magnitude, snr)
                    SELECT lc.star_id, lc.image_id, lc.magnitude, COALESCE(lc.snr, 0.0)
                    FROM light_curves lc
                    """
                )
                # Add expected indexes (names match common conventions)
                self._conn.execute("CREATE INDEX IF NOT EXISTS phot_by_star_image ON photometry(star_id, image_id)")
                self._conn.execute("CREATE INDEX IF NOT EXISTS phot_by_image ON photometry(image_id)")
                self._conn.commit()
                logger.info("Materialized TABLE 'photometry' from 'light_curves'.")
        except Exception as e:
            # If anything goes wrong, log but allow miner to fall back to `light_curves`
            logger.warning("Failed to create photometry compatibility layer: %s", e)

    def _measurement_table(self) -> Optional[str]:
        if self._table_has_rows("photometry"):
            return "photometry"
        if self._table_has_rows("light_curves"):
            return "light_curves"
        return None

    def _build_index(self) -> None:
        """
        Populate self._pfilters and self._star_ids without ever requiring a
        'photometry' table. Prefer light_curves; fall back to images/stars.
        """
        self._pfilters = []
        self._star_ids = []

        has_lc = self._table_has_rows("light_curves")
        has_images = self._table_has_rows("images")
        has_filters = self._table_has_rows("photometric_filters")
        has_stars = self._table_has_rows("stars")

        # 1) Filters actually used by images that have light-curve measurements
        if has_lc and has_images and has_filters:
            sql_pf_from_lc = """
                SELECT DISTINCT pf.id, pf.name
                FROM light_curves lc
                JOIN images i   ON i.id = lc.image_id
                JOIN photometric_filters pf ON pf.id = i.filter_id
                WHERE i.filter_id IS NOT NULL
                ORDER BY pf.id
            """
            self._pfilters = [PFilter(r["id"], r["name"]) for r in self._conn.execute(sql_pf_from_lc)]

        # 1b) Fallback: any filters referenced by images at all
        if not self._pfilters and has_images and has_filters:
            sql_pf_from_images = """
                SELECT DISTINCT pf.id, pf.name
                FROM images i
                JOIN photometric_filters pf ON pf.id = i.filter_id
                WHERE i.filter_id IS NOT NULL
                ORDER BY pf.id
            """
            self._pfilters = [PFilter(r["id"], r["name"]) for r in self._conn.execute(sql_pf_from_images)]

        # 1c) Last resort: list all filter rows (keeps UI usable even if images lack filter_id)
        if not self._pfilters and has_filters:
            sql_all_pf = "SELECT id, name FROM photometric_filters ORDER BY id"
            self._pfilters = [PFilter(r["id"], r["name"]) for r in self._conn.execute(sql_all_pf)]

        # Build quick lookups
        self._filter_by_id = {pf.id: pf for pf in self._pfilters}
        self._filter_by_name = {pf.name: pf for pf in self._pfilters}

        # 2) Stars: prefer those that actually have measurements in light_curves
        if has_lc:
            sql_star_ids = "SELECT DISTINCT star_id FROM light_curves ORDER BY star_id"
            self._star_ids = [r["star_id"] for r in self._conn.execute(sql_star_ids)]

        # 2b) Fallback: all cataloged stars
        if not self._star_ids and has_stars:
            sql_all_stars = "SELECT id FROM stars ORDER BY id"
            self._star_ids = [r["id"] for r in self._conn.execute(sql_all_stars)]

        # If still nothing, make it explicit so callers can react cleanly
        if not self._star_ids:
            raise NoStarsSelectedError("No stars found (no light_curves and no stars).")

        # Clear caches that depend on index contents
        try:
            self.get_star.cache_clear()  # type: ignore[attr-defined]
            self.get_light_curve.cache_clear()  # type: ignore[attr-defined]
            self.get_cmp_stars.cache_clear()  # type: ignore[attr-defined]
        except Exception:
            pass

    # ---------------------- public attributes --------------------

    @property
    def pfilters(self) -> List[PFilter]:
        return self._pfilters

    @property
    def star_ids(self) -> List[int]:
        return self._star_ids

    # ----------------------- read operations ---------------------

    @functools.lru_cache(maxsize=None)
    def get_star(self, star_id: int) -> Dict[str, Any]:
        """
        Return a dict with at least: ra, dec, imag.
        More keys are harmless and often handy for future views.
        """
        sql = """
            SELECT id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag
            FROM stars WHERE id = ?
        """
        row = self._conn.execute(sql, (int(star_id),)).fetchone()
        if row is None:
            raise KeyError(f"Star {star_id} not found")
        return {
            "id": row["id"],
            "x": row["x"],
            "y": row["y"],
            "ra": row["ra"],
            "dec": row["dec"],
            "epoch": row["epoch"],
            "pm_ra": row["pm_ra"],
            "pm_dec": row["pm_dec"],
            "imag": row["imag"],
        }

    def _resolve_filter_id(self, pfilter: Union[int, str, PFilter, None], star_id: int) -> Optional[int]:
        """
        Resolve arbitrary pfilter spec to a concrete filter_id.
        If None, choose the star's dominant filter (most points).
        """
        if pfilter is None:
            return self._dominant_filter_id_for_star(star_id)

        if isinstance(pfilter, PFilter):
            return int(pfilter.id)

        if isinstance(pfilter, str):
            pf = self._filter_by_name.get(pfilter)
            if pf is not None:
                return int(pf.id)
            try:
                fid = int(pfilter)
                return fid if (fid in self._filter_by_id or not self._filter_by_id) else None
            except Exception:
                return None

        try:
            return int(pfilter)  # type: ignore[arg-type]
        except Exception:
            return None

    def _dominant_filter_id_for_star(self, star_id: int) -> Optional[int]:
        """
        Return the filter_id where this star has the most measurements,
        using whichever measurement table is available.
        """
        meas = self._measurement_table()
        if meas is None:
            return None

        sql = f"""
            SELECT i.filter_id AS fid, COUNT(*) AS n
            FROM {meas} m
            JOIN images i ON i.id = m.image_id
            WHERE m.star_id = ?
            GROUP BY i.filter_id
            ORDER BY n DESC
            LIMIT 1
        """
        row = self._conn.execute(sql, (int(star_id),)).fetchone()
        if row and row["fid"] is not None:
            return int(row["fid"])
        return None

    @functools.lru_cache(maxsize=None)
    def get_light_curve(self, star_id: int, pfilter: Union[int, str, PFilter]) -> List[Tuple[float, float, float]]:
        """
        Return a list of (unix_time, magnitude, snr) triples for the given star
        and filter. Uses `photometry` if present (or compat), otherwise `light_curves`.
        """
        fid = self._resolve_filter_id(pfilter, star_id)
        if fid is None:
            return []

        meas = self._measurement_table()
        if meas is None:
            return []

        # Filter out NULL unix_time for plotting/UI sanity
        sql = f"""
            SELECT i.unix_time AS t,
                   m.magnitude  AS mag,
                   COALESCE(m.snr, 0.0) AS snr
            FROM {meas} m
            JOIN images i ON i.id = m.image_id
            WHERE m.star_id = ?
              AND i.filter_id = ?
              AND i.unix_time IS NOT NULL
            ORDER BY i.unix_time
        """
        rows = self._conn.execute(sql, (int(star_id), int(fid))).fetchall()
        return [(r["t"], r["mag"], r["snr"]) for r in rows]

    # --- comparison stars -------------------------------------------------
    def _resolve_filter_id(self, pfilter) -> int:
        # Accept PFilter, id int, id-as-str, or name like 'R'
        if hasattr(pfilter, "id"):
            return int(pfilter.id)
        try:
            return int(pfilter)  # handles ints and numeric strings
        except Exception:
            pass
        row = self._conn.execute(
            "SELECT id FROM photometric_filters WHERE name = ? LIMIT 1",
            (str(pfilter),),
        ).fetchone()
        if not row:
            raise KeyError(f"Unknown filter: {pfilter!r}")
        return int(row["id"])

    @functools.lru_cache(maxsize=None)
    def get_cmp_stars(self, star_id: int, pfilter) -> list[tuple[int, float, float]]:
        fid = self._resolve_filter_id(pfilter)
        rows = self._conn.execute(
            "SELECT cstar_id, weight, stdev "
            "FROM cmp_stars "
            "WHERE star_id = ? AND filter_id = ? "
            "ORDER BY weight DESC, stdev ASC",
            (int(star_id), fid),
        ).fetchall()
        return [(int(r["cstar_id"]), float(r["weight"]), float(r["stdev"])) for r in rows]

    # --- (optional) periods hook -----------------------------------------

    @functools.lru_cache(maxsize=None)
    def get_period(self, star_id: int, pfilter: Union[int, str, PFilter]) -> Optional[float]:
        """
        Placeholder for per-star period retrieval if/when you store them.
        Return None if not available.
        """
        try:
            if not self._table_or_view_exists("candidate_parameters"):
                return None
            # Adapt this to your actual period storage if/when you add it.
            return None
        except Exception:
            return None

    # --------------------------- utils ---------------------------

    def refresh(self) -> None:
        """Recompute pfilters and star_ids (e.g., after DB updates)."""
        self._ensure_photometry_compat()
        self._has_photometry = self._table_has_rows("photometry")
        self._has_light_curves = self._table_has_rows("light_curves")
        self._build_index()


# Default logger config (only if root isn't configured)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
