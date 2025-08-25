# juicer/miner.py ? SQLite miner used by the GTK juicer UI (no legacy paths)
from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path
from typing import Any, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# Small DB helpers
# ----------------------------------------------------------------------------

def _open_sqlite(db_path: Union[str, Path]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # access by column name
    try:
        # Read-mostly workload; keep it simple & fast
        conn.execute("PRAGMA journal_mode=OFF;")
        conn.execute("PRAGMA synchronous=OFF;")
        conn.execute("PRAGMA temp_store=MEMORY;")
    except Exception:
        pass
    return conn


def _relation_exists(conn: sqlite3.Connection, name: str) -> bool:
    # True if a table or a view exists
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE (type='table' OR type='view') AND name=? LIMIT 1",
        (name,),
    )
    return cur.fetchone() is not None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
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

    def __str__(self) -> str:
        return self.name or str(self.id)


# ----------------------------------------------------------------------------
# Miner
# ----------------------------------------------------------------------------

class LEMONdBMiner:
    """
    Minimal miner for LEMON-like SQLite databases (current schema only).

    Exposed properties:
      - pfilters: list[PFilter]
      - star_ids: list[int]
      - field_name: str (derived from filename)
      - path: str

    Public API used by the UI:
      - get_star(star_id) -> dict
      - get_light_curve(star_id, pfilter=None) -> list[(t, mag, snr?)]
      - get_cmp_stars(star_id, pfilter=None) -> list[(cstar_id, weight, stdev)]
      - get_cmp_stars_info(star_id, pfilter=None) -> list[dict]
      - filter_id_from_label(label, star_id=None) -> Optional[int]
      - close()
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

        # Core relations we depend on
        self._has_stars = _table_exists(self._conn, "stars")
        self._has_images = _table_exists(self._conn, "images")
        self._has_filters = _table_exists(self._conn, "photometric_filters")

        # Measurement table: prefer 'photometry', else 'light_curves'
        if _relation_exists(self._conn, "photometry"):
            self._meas_table = "photometry"
        elif _relation_exists(self._conn, "light_curves"):
            self._meas_table = "light_curves"
        else:
            self._meas_table = ""  # absent; calls will return empty results

        # Cache light-weight lookups
        self._pfilters: List[PFilter] = self._load_filters()
        self._star_ids: List[int] = self._load_star_ids()

    # ------------------------------------------------------------------
    # Properties
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
    def _load_star_ids(self) -> List[int]:
        if not self._has_stars:
            return []
        rows = self._conn.execute("SELECT id FROM stars ORDER BY id").fetchall()
        return [int(r["id"]) for r in rows]

    def _load_filters(self) -> List[PFilter]:
        """Load distinct filter IDs/names seen in images."""
        if not (self._has_images and self._has_filters):
            return []
        rows = self._conn.execute(
            """
            SELECT DISTINCT f.id   AS id,
                            f.name AS name
            FROM images i
            JOIN photometric_filters f ON f.id = i.filter_id
            WHERE i.filter_id IS NOT NULL
            ORDER BY f.id
            """
        ).fetchall()
        return [PFilter(id=int(r["id"]), name=r["name"]) for r in rows]

    def _resolve_filter_id(self, pfilter: Any, star_id: int) -> Optional[int]:
        """Resolve UI/CLI filter spec to an integer filter_id. If None, choose dominant."""
        if pfilter is None:
            return self._dominant_filter_id_for_star(star_id)
        if isinstance(pfilter, PFilter):
            return int(pfilter.id)
        if isinstance(pfilter, int):
            return int(pfilter)
        if isinstance(pfilter, str):
            key = pfilter.strip().lower()
            # numeric string?
            try:
                return int(key)
            except Exception:
                pass
            # name match
            for pf in self._pfilters:
                if (pf.name or "").lower() == key:
                    return int(pf.id)
        return None

    def _dominant_filter_id_for_star(self, star_id: int) -> Optional[int]:
        """Pick the filter with the most measurement rows for this star.
        Falls back to the cmp_stars filter with largest total weight.
        """
        if self._meas_table and self._has_images and "filter_id" in _columns(self._conn, "images"):
            row = self._conn.execute(
                f"""
                SELECT i.filter_id, COUNT(*) AS n
                FROM {self._meas_table} m
                JOIN images i ON i.id = m.image_id
                WHERE m.star_id = ? AND i.filter_id IS NOT NULL
                GROUP BY i.filter_id
                ORDER BY n DESC
                LIMIT 1
                """,
                (int(star_id),),
            ).fetchone()
            if row is not None:
                return int(row["filter_id"] if "filter_id" in row.keys() else row[0])

        # Fallback: cmp_stars weight sum
        if _table_exists(self._conn, "cmp_stars"):
            row = self._conn.execute(
                """
                SELECT filter_id, SUM(weight) AS wsum
                FROM cmp_stars
                WHERE star_id = ?
                GROUP BY filter_id
                ORDER BY wsum DESC
                LIMIT 1
                """,
                (int(star_id),),
            ).fetchone()
            if row is not None:
                return int(row["filter_id"] if "filter_id" in row.keys() else row[0])

        return None

    # ------------------------------------------------------------------
    # Public API
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
            "x": float(row["x"]),
            "y": float(row["y"]),
            "ra": float(row["ra"]),
            "dec": float(row["dec"]),
            "imag": None if row["imag"] is None else float(row["imag"]),
        }

    def get_light_curve(
        self,
        star_id: int,
        pfilter: Any | None = None,
    ) -> List[Tuple[float, float, Optional[float]]]:
        """Return sorted (epoch_seconds, magnitude, snr?) for a star in a filter."""
        if not (self._meas_table and self._has_images):
            return []

        fid = self._resolve_filter_id(pfilter, star_id)

        # We assume modern columns: images.unix_time, measurements.magnitude, measurements.snr
        sql = [
            "SELECT i.unix_time AS t, m.magnitude AS mag, m.snr AS snr",
            f"FROM {self._meas_table} m",
            "JOIN images i ON i.id = m.image_id",
            "WHERE m.star_id = ?",
        ]
        params: List[Any] = [int(star_id)]
        if fid is not None:
            sql.append("AND i.filter_id = ?")
            params.append(int(fid))
        sql.append("ORDER BY i.unix_time ASC, i.id ASC")
        q = "\n".join(sql)

        rows = self._conn.execute(q, params).fetchall()
        out: List[Tuple[float, float, Optional[float]]] = []
        for r in rows:
            try:
                out.append(
                    (
                        float(r["t"]) if r["t"] is not None else 0.0,
                        float(r["mag"]) if r["mag"] is not None else float("nan"),
                        None if r["snr"] is None else float(r["snr"]),
                    )
                )
            except Exception:
                continue
        return out

    def get_cmp_stars(
        self,
        star_id: int,
        pfilter: Any | None = None,
    ) -> List[Tuple[int, float, float]]:
        """Return (cstar_id, weight, stdev) sorted by usefulness (weight desc, sigma asc)."""
        if not _table_exists(self._conn, "cmp_stars"):
            return []
        fid = self._resolve_filter_id(pfilter, star_id)
        if fid is None:
            return []

        rows = self._conn.execute(
            """
            SELECT cstar_id, weight, stdev
            FROM cmp_stars
            WHERE star_id = ? AND filter_id = ?
            ORDER BY weight DESC, stdev ASC
            """,
            (int(star_id), int(fid)),
        ).fetchall()

        out: List[Tuple[int, float, float]] = []
        for r in rows:
            try:
                out.append((int(r["cstar_id"]), float(r["weight"]), float(r["stdev"])))
            except Exception:
                continue
        return out

    def get_cmp_stars_info(
        self,
        star_id: int,
        pfilter: Any | None = None,
    ) -> List[dict]:
        """Return dict rows: {cstar_id, weight, stdev, ra, dec, imag}."""
        if not (_table_exists(self._conn, "cmp_stars") and self._has_stars):
            return []
        fid = self._resolve_filter_id(pfilter, star_id)
        if fid is None:
            return []

        rows = self._conn.execute(
            """
            SELECT cs.cstar_id AS cstar_id,
                   cs.weight   AS weight,
                   cs.stdev    AS stdev,
                   s.ra        AS ra,
                   s.dec       AS dec,
                   s.imag      AS imag
            FROM cmp_stars cs
            JOIN stars s ON s.id = cs.cstar_id
            WHERE cs.star_id = ? AND cs.filter_id = ?
            ORDER BY cs.weight DESC, cs.stdev ASC
            """,
            (int(star_id), int(fid)),
        ).fetchall()

        out: List[dict] = []
        for r in rows:
            try:
                out.append(
                    {
                        "cstar_id": int(r["cstar_id"]),
                        "weight": float(r["weight"]),
                        "stdev": float(r["stdev"]),
                        "ra": float(r["ra"]),
                        "dec": float(r["dec"]),
                        "imag": None if r["imag"] is None else float(r["imag"]),
                    }
                )
            except Exception:
                continue
        return out

    # Convenience for UI dropdowns / CLI
    def filter_id_from_label(self, label: Any, *, star_id: Optional[int] = None) -> Optional[int]:
        return self._resolve_filter_id(label, star_id or -1)

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass
