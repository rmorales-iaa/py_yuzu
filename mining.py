# juicer/mining.py — SQLite miner used by the GTK juicer UI
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------------
# Small DB helpers
# ----------------------------------------------------------------------------

def _open_sqlite(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # access by column name
    try:
        conn.execute("PRAGMA journal_mode=OFF;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _view_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _columns(conn: sqlite3.Connection, name: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({name})").fetchall()]
    except Exception:
        return []


# ----------------------------------------------------------------------------
# Public types
# ----------------------------------------------------------------------------

@dataclasses.dataclass(slots=True)
class PFilter:
    id: int
    name: Optional[str] = None
    letter: Optional[str] = None
    def __str__(self) -> str:
        return self.name or self.letter or str(self.id)


# ----------------------------------------------------------------------------
# Miner
# ----------------------------------------------------------------------------

class LEMONdBMiner:
    """Minimal, robust miner for LEMON-like SQLite databases.

    UI-facing properties (iterable attributes, not methods):
      • ``pfilters: list[PFilter]``
      • ``star_ids: list[int]``
      • ``field_name: str`` (nice label derived from filename)
      • ``path: str``

    Methods the GTK UI calls:
      • ``get_star(star_id)`` → mapping with at least ra, dec, imag
      • ``get_light_curve(star_id, pfilter=None)`` → list[(t, mag, snr)]

    It also guarantees a *materialized* ``photometry`` table exists if the DB only
    has ``light_curves``; a legacy ``photometry`` VIEW is dropped and replaced by a
    real table to keep downstream code consistent.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def __init__(self, db_path: Union[str, Path]) -> None:
        p = Path(db_path)
        if not p.is_file():
            raise FileNotFoundError(str(p))
        self.path: str = str(p)
        self.field_name: str = p.stem

        self._conn = _open_sqlite(self.path)

        # Ensure photometry compatibility (TABLE from light_curves if needed)
        self._ensure_photometry_table()

        # Detect schema bits
        self._has_stars = _table_exists(self._conn, "stars")
        self._has_images = _table_exists(self._conn, "images")
        # Filter table can be named either photometric_filters (LEMON/yuzu) or filters
        if _table_exists(self._conn, "photometric_filters"):
            self._filter_table = "photometric_filters"
        elif _table_exists(self._conn, "filters"):
            self._filter_table = "filters"
        else:
            self._filter_table = None

        # Measurement table selection
        self._meas_table = self._detect_measurement_table()

        # Build in-memory indexes
        self._pfilters: List[PFilter] = self._load_filters()
        self._star_ids: List[int] = self._load_star_ids()

    # ------------------------------------------------------------------
    # Properties (as requested by the UI)
    # ------------------------------------------------------------------
    @property
    def pfilters(self) -> List[PFilter]:
        return list(self._pfilters)

    @property
    def star_ids(self) -> List[int]:
        return list(self._star_ids)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _detect_measurement_table(self) -> str:
        for name in ("photometry", "light_curves", "diff_photometry", "measurements"):
            if _table_exists(self._conn, name) or _view_exists(self._conn, name):
                return name
        return "photometry"  # safest default; may be a view created elsewhere

    def _ensure_photometry_table(self) -> None:
        """If ``photometry`` table is missing but ``light_curves`` exists, materialize it.
        Drop a legacy VIEW named ``photometry`` so a TABLE can be created.
        """
        has_lc = _table_exists(self._conn, "light_curves") or _view_exists(self._conn, "light_curves")
        has_phot_table = _table_exists(self._conn, "photometry")
        has_phot_view = _view_exists(self._conn, "photometry")

        try:
            if has_phot_view and not has_phot_table:
                self._conn.execute("DROP VIEW IF EXISTS photometry")
                self._conn.commit()
                logger.info("Dropped compatibility VIEW 'photometry' to create a TABLE.")

            has_phot_table = _table_exists(self._conn, "photometry")
            if has_phot_table or not has_lc:
                return

            # Create table and populate from light_curves
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
            self._conn.execute(
                """
                INSERT OR IGNORE INTO photometry (star_id, image_id, magnitude, snr)
                SELECT lc.star_id, lc.image_id, lc.magnitude, COALESCE(lc.snr, 0.0)
                FROM light_curves lc
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS phot_by_star_image ON photometry(star_id, image_id)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS phot_by_image ON photometry(image_id)"
            )
            self._conn.commit()
        except Exception as e:
            logger.warning("Failed to ensure 'photometry' TABLE: %s", e)

    def _load_star_ids(self) -> List[int]:
        if not _table_exists(self._conn, "stars"):
            return []
        cur = self._conn.execute("SELECT id FROM stars ORDER BY id")
        return [int(r[0]) for r in cur.fetchall()]

    def _load_filters(self) -> List[PFilter]:
        """Load filters robustly without assuming columns like `letter` exist.
        We only rely on `id` and (if present) `name`.
        """
        if not self._has_images:
            return []
        if self._filter_table:
            cols = set(_columns(self._conn, self._filter_table))
            if "name" in cols:
                cur = self._conn.execute(
                    f"""
                    SELECT DISTINCT f.id AS id, f.name AS name
                    FROM images i JOIN {self._filter_table} f ON f.id = i.filter_id
                    WHERE i.filter_id IS NOT NULL
                    ORDER BY id
                    """
                )
                out: List[PFilter] = []
                for r in cur.fetchall():
                    name = r["name"]
                    letter = (name or "").upper()[:1] if name else None
                    out.append(PFilter(int(r["id"]), name, letter))
                return out
            else:
                # Table has no `name`; fall back to numeric IDs only
                cur = self._conn.execute(
                    f"""
                    SELECT DISTINCT f.id AS id
                    FROM images i JOIN {self._filter_table} f ON f.id = i.filter_id
                    WHERE i.filter_id IS NOT NULL
                    ORDER BY id
                    """
                )
                return [PFilter(int(r["id"])) for r in cur.fetchall()]
        else:
            # No filter table at all; synthesize from images.filter_id
            cur = self._conn.execute(
                "SELECT DISTINCT filter_id FROM images WHERE filter_id IS NOT NULL ORDER BY filter_id"
            )
            return [PFilter(int(r[0])) for r in cur.fetchall()]

    def _time_expr(self) -> Tuple[str, str]:
        cols = set(_columns(self._conn, "images")) if self._has_images else set()
        if "unix_time" in cols:
            return ("i.unix_time", "t")
        for c in ("dateobs", "date_obs", "timestamp", "time"):
            if c in cols:
                return (f"strftime('%s', i.{c})", "t")
        return ("0", "t")  # last resort, still sortable

    def _mag_col(self) -> Optional[str]:
        cols = set(_columns(self._conn, self._meas_table))
        for c in ("magnitude", "mag", "delta_mag", "dm", "mag_rel", "inst_mag"):
            if c in cols:
                return c
        return None

    def _snr_col(self) -> Optional[str]:
        cols = set(_columns(self._conn, self._meas_table))
        for c in ("snr", "signal_to_noise", "snr_rel", "snr_inst"):
            if c in cols:
                return c
        return None

    # ------------------------------------------------------------------
    # Public API used by the UI
    # ------------------------------------------------------------------
    def get_star(self, star_id: int) -> dict:
        if not self._has_stars:
            raise KeyError("stars table missing")
        row = self._conn.execute(
            "SELECT id, x, y, ra, dec, imag FROM stars WHERE id = ?",
            (int(star_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"star {star_id} not found")
        return {
            "id": int(row["id"]),
            "x": row["x"],
            "y": row["y"],
            "ra": float(row["ra"]),
            "dec": float(row["dec"]),
            "imag": (None if row["imag"] is None else float(row["imag"]))
        }

    def get_light_curve(
        self,
        star_id: int,
        pfilter: Any | None = None,
    ) -> List[Tuple[float, float, Optional[float]]]:
        """Return a sorted list of (epoch_seconds, magnitude, snr?) for a star.
        ``pfilter`` may be a ``PFilter``, an ``int`` (filter id), or a ``str`` name/letter.
        """
        if not self._has_images or not (_table_exists(self._conn, self._meas_table) or _view_exists(self._conn, self._meas_table)):
            return []

        # Resolve filter id if provided
        flt_id: Optional[int] = None
        if isinstance(pfilter, PFilter):
            flt_id = int(pfilter.id)
        elif isinstance(pfilter, int):
            flt_id = int(pfilter)
        elif isinstance(pfilter, str):
            key = pfilter.strip().lower()
            for pf in self._pfilters:
                if key in {str(pf.id), (pf.name or "").lower(), (pf.letter or "").lower()}:
                    flt_id = pf.id
                    break

        t_expr, t_label = self._time_expr()
        mag_col = self._mag_col()
        snr_col = self._snr_col()

        sel_cols: List[str] = [f"{t_expr} AS {t_label}"]
        sel_cols.append(f"m.{mag_col} AS mag" if mag_col else "NULL AS mag")
        sel_cols.append(f"m.{snr_col} AS snr" if snr_col else "NULL AS snr")
        sel = ", ".join(sel_cols)

        sql: List[str] = [
            f"SELECT {sel}",
            f"FROM {self._meas_table} m",
            "JOIN images i ON i.id = m.image_id",
            "WHERE m.star_id = ?",
        ]
        params: List[Any] = [int(star_id)]

        if flt_id is not None:
            # Prefer images.filter_id; fall back to m.filter_id if present
            if "filter_id" in _columns(self._conn, "images"):
                sql.append("AND i.filter_id = ?")
                params.append(int(flt_id))
            elif "filter_id" in _columns(self._conn, self._meas_table):
                sql.append("AND m.filter_id = ?")
                params.append(int(flt_id))

        sql.append("ORDER BY i.unix_time ASC, i.id ASC")
        q = "\n".join(sql)

        rows = self._conn.execute(q, params).fetchall()
        out: List[Tuple[float, float, Optional[float]]] = []
        for r in rows:
            try:
                t = float(r[t_label]) if r[t_label] is not None else 0.0
                mag = float(r["mag"]) if r["mag"] is not None else float("nan")
                snr = None if r["snr"] is None else float(r["snr"])
                out.append((t, mag, snr))
            except Exception:
                # Be permissive; skip malformed rows
                continue
        return out

    # Back-compat convenience used elsewhere sometimes
    def filters(self) -> Sequence[PFilter]:
        return tuple(self._pfilters)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
