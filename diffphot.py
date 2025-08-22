# diffphot.py
from __future__ import annotations
import argparse
import os
import shutil
import sqlite3
import stat
import sys
from dataclasses import dataclass
from typing import Iterable, Optional


# -------------------------- small utils --------------------------
def owner_writable(path: str, make_writable: bool = True) -> None:
    """
    Make file owner-writable (u+w). Uses util.io.owner_writable if available,
    otherwise a tiny fallback.
    """
    try:
        from util.io import owner_writable as _ow  # type: ignore
    except Exception:
        _ow = None

    if _ow is not None:
        _ow(path, make_writable)  # type: ignore[misc]
        return

    # Fallback: chmod u+w or remove u+w
    st = os.stat(path)
    mode = stat.S_IMODE(st.st_mode)
    if make_writable:
        mode |= stat.S_IWUSR
    else:
        mode &= ~stat.S_IWUSR
    os.chmod(path, mode)


def _ensure_parent_dir(path: str) -> None:
    d = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(d, exist_ok=True)


def _human_int(n: int) -> str:
    return f"{n:,}".replace(",", "_")


# -------------------------- CLI --------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="yuzu diffphot",
        description="Compute differential photometry and write light_curves table.",
    )
    p.add_argument("input_db", help="Input LEMON SQLite database (photometry present)")
    p.add_argument("output_db", help="Output LEMON SQLite database to write light_curves")
    p.add_argument(
        "-y", "--overwrite", action="store_true",
        help="Overwrite output DB if it exists."
    )
    # old names you may have used
    p.add_argument("--minimum-images", type=int, default=10,
                   help="Minimum images per target to keep (default: 10)")
    p.add_argument("--minimum-stars", type=int, default=2,
                   help="Minimum comparison stars per target (default: 2)")
    # backwards-compat aliases
    p.add_argument("--min-images", type=int, dest="minimum_images")
    p.add_argument("--min-cstars", type=int, dest="minimum_stars")
    p.add_argument("--snr-threshold", type=float, default=0.0,
                   help="Discard per-image photometry with SNR below this (default: 0)")
    return p


@dataclass
class Options:
    input_db: str
    output_db: str
    overwrite: bool
    minimum_images: int
    minimum_stars: int
    snr_threshold: float


def _coerce_namespace(ns: argparse.Namespace | object) -> Options:
    """
    Accept either a Namespace (from yuzu's dispatcher) or parse argv
    when invoked as a standalone script.
    """
    if isinstance(ns, argparse.Namespace):
        # Accept both new and old names
        inp = getattr(ns, "input_db", None) or getattr(ns, "input", None)
        out = getattr(ns, "output_db", None) or getattr(ns, "output", None)
        if not inp or not out:
            raise SystemExit("diffphot: missing input_db/output_db in arguments")

        min_images = getattr(ns, "minimum_images", None)
        if min_images is None:
            min_images = getattr(ns, "min_images", 10)
        min_stars = getattr(ns, "minimum_stars", None)
        if min_stars is None:
            min_stars = getattr(ns, "min_cstars", 2)
        snr_th = getattr(ns, "snr_threshold", 0.0)
        return Options(
            input_db=str(inp),
            output_db=str(out),
            overwrite=bool(getattr(ns, "overwrite", False)),
            minimum_images=int(min_images),
            minimum_stars=int(min_stars),
            snr_threshold=float(snr_th),
        )

    # Fallback: parse sys.argv
    args = build_parser().parse_args()
    return _coerce_namespace(args)


# -------------------------- DB helpers --------------------------
SCHEMA_LIGHT_CURVES = """
CREATE TABLE IF NOT EXISTS light_curves (
    id         INTEGER PRIMARY KEY,
    star_id    INTEGER NOT NULL,
    image_id   INTEGER NOT NULL,
    magnitude  REAL NOT NULL,
    snr        REAL,
    FOREIGN KEY (star_id)  REFERENCES stars(id),
    FOREIGN KEY (image_id) REFERENCES images(id),
    UNIQUE (star_id, image_id)
);
"""

INDEX_CURVE = "CREATE INDEX IF NOT EXISTS curve_by_star_image ON light_curves(star_id, image_id);"


def _open_sqlite(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA temp_store = MEMORY;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?;", (name,)
    )
    return cur.fetchone() is not None


def _count(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> int:
    cur = conn.execute(sql, params)
    row = cur.fetchone()
    return int(row[0] if row and row[0] is not None else 0)


# -------------------------- core calculation --------------------------
DIFFSQL = r"""
-- Weighted differential magnitude:
--   ?m = m_target - ( ? w_i m_i / ? w_i )
WITH valid_targets AS (
    SELECT star_id, filter_id, COUNT(*) AS ncs
    FROM cmp_stars
    GROUP BY star_id, filter_id
    HAVING ncs >= :min_cstars
),
pairs AS (
    SELECT
        t.star_id              AS star_id,
        t.image_id             AS image_id,
        t.magnitude            AS mt,
        t.snr                  AS snr_t,
        cs.cstar_id            AS cstar_id,
        cs.weight              AS w,
        c.magnitude            AS mc
    FROM photometry AS t
    JOIN images AS img           ON img.id = t.image_id
    JOIN valid_targets AS vt     ON vt.star_id = t.star_id
                                 AND vt.filter_id IS img.filter_id
    JOIN cmp_stars AS cs         ON cs.star_id = vt.star_id
                                 AND cs.filter_id = vt.filter_id
    JOIN photometry AS c         ON c.star_id  = cs.cstar_id
                                 AND c.image_id = t.image_id
    WHERE (t.snr IS NULL OR t.snr >= :snr_th)
      AND (c.snr IS NULL OR c.snr >= :snr_th)
),
agg AS (
    SELECT
        star_id,
        image_id,
        MAX(snr_t)                             AS snr_t,
        SUM(w * mc) / NULLIF(SUM(w), 0.0)      AS wmean_c
    FROM pairs
    GROUP BY star_id, image_id
)
INSERT OR REPLACE INTO light_curves (star_id, image_id, magnitude, snr)
SELECT
    t.star_id,
    t.image_id,
    t.magnitude - a.wmean_c        AS diffmag,
    a.snr_t                        AS snr
FROM photometry AS t
JOIN agg AS a ON a.star_id  = t.star_id AND a.image_id = t.image_id
WHERE a.wmean_c IS NOT NULL
;
"""


def _rebuild_light_curves(conn: sqlite3.Connection,
                          min_images: int,
                          min_cstars: int,
                          snr_threshold: float) -> tuple[int, int]:
    """
    Compute differential magnitudes into light_curves.
    Returns: (n_rows_written, n_stars_kept)
    """
    conn.execute("BEGIN;")
    conn.execute(SCHEMA_LIGHT_CURVES)
    conn.execute(INDEX_CURVE)
    # Clear previous results
    conn.execute("DELETE FROM light_curves;")
    conn.execute("COMMIT;")

    conn.execute("BEGIN;")
    conn.execute(DIFFSQL, {"min_cstars": int(min_cstars), "snr_th": float(snr_threshold)})
    conn.execute("COMMIT;")

    # Drop stars with too few images (optional filter)
    if min_images > 0:
        conn.execute("BEGIN;")
        conn.execute(
            """
            DELETE FROM light_curves
            WHERE star_id IN (
                SELECT star_id FROM light_curves
                GROUP BY star_id
                HAVING COUNT(*) < ?
            );
            """,
            (int(min_images),),
        )
        conn.execute("COMMIT;")

    n_rows = _count(conn, "SELECT COUNT(*) FROM light_curves;")
    n_stars = _count(conn, "SELECT COUNT(DISTINCT star_id) FROM light_curves;")
    return n_rows, n_stars


# -------------------------- command runner --------------------------
def run(opts: Options) -> int:
    in_db = os.path.abspath(opts.input_db)
    out_db = os.path.abspath(opts.output_db)

    if not os.path.isfile(in_db):
        print(f"ERROR: Input database does not exist:\n  {in_db}", file=sys.stderr)
        return 2

    if os.path.exists(out_db):
        if not opts.overwrite:
            print(f"ERROR: Output database already exists:\n  {out_db}\n"
                  f"Use --overwrite to replace it.", file=sys.stderr)
            return 2
        # make sure it's writable before we replace it
        try:
            owner_writable(out_db, True)
        except Exception:
            pass

    # 1) Make a copy first, then chmod
    print(">> Making a copy of the input database...", end=" ")
    sys.stdout.flush()
    _ensure_parent_dir(out_db)
    shutil.copy2(in_db, out_db)
    try:
        owner_writable(out_db, True)
    except Exception:
        pass
    print("done.")

    # 2) Compute differential photometry into light_curves
    print(">> Computing differential magnitudes (weighted by cmp_stars.weight)...", end=" ")
    sys.stdout.flush()
    conn = _open_sqlite(out_db)
    try:
        if not _table_exists(conn, "photometry"):
            print("\nERROR: Table 'photometry' is missing in the database.", file=sys.stderr)
            return 3
        if not _table_exists(conn, "images"):
            print("\nERROR: Table 'images' is missing in the database.", file=sys.stderr)
            return 3
        if not _table_exists(conn, "cmp_stars"):
            print("\nERROR: Table 'cmp_stars' is missing in the database.", file=sys.stderr)
            return 3

        n_rows, n_stars = _rebuild_light_curves(
            conn,
            min_images=opts.minimum_images,
            min_cstars=opts.minimum_stars,
            snr_threshold=opts.snr_threshold,
        )
    finally:
        conn.close()
    print("done.")
    print(f">> Wrote {_human_int(n_rows)} points for {_human_int(n_stars)} stars into light_curves.")
    return 0


# -------------------------- entry points --------------------------
def main(ns_or_argv: Optional[argparse.Namespace | list[str]] = None) -> int:
    """
    yuzu's dispatcher calls module.main(args_namespace).
    Running as a script calls main() with None; we parse argv then.
    """
    if isinstance(ns_or_argv, argparse.Namespace):
        opts = _coerce_namespace(ns_or_argv)
        return run(opts)

    # Running standalone
    parser = build_parser()
    args = parser.parse_args(ns_or_argv)
    opts = _coerce_namespace(args)
    return run(opts)


if __name__ == "__main__":
    sys.exit(main())
