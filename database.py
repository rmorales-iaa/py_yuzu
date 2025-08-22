#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
database.py ? LEMON database helpers (Python 3)

IMPORTANT CHANGE:
-----------------
This version NEVER uses Python's process-random `hash()` to identify
photometric filters. All reads/writes resolve the filter **by name** via the
`photometric_filters` table and its stable `id`. This fixes the classic issue
where different processes see different `hash(Passband)` values, which led to
empty results in queries such as get_photometry()/diffphot.

Only minimal SQL assumptions are made to match the original LEMON schema:

Tables relied upon here:
- stars(id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag)
- images(id, path, filter_id, unix_time, object, airmass, gain, ra, dec, sources)
- photometric_filters(id, name UNIQUE)
- photometry(star_id, image_id, magnitude, snr, UNIQUE(star_id,image_id))
- light_curves(id, star_id, image_id, magnitude, snr, UNIQUE(star_id,image_id))
- cmp_stars(id, star_id, filter_id, cstar_id, stdev, weight,
            UNIQUE(star_id, filter_id, cstar_id))

If your schema names differ, please adapt the SQL in the helper methods.
"""

from __future__ import annotations

import collections
import contextlib
import hashlib
import os
import sqlite3
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Dict

import numpy

import util
try:
    # Prefer the project's Passband class if available
    from passband import Passband
except Exception:  # pragma: no cover
    # Fallback shim
    class Passband(str):
        def __new__(cls, name: str):
            return str.__new__(cls, name)


# ---------------------------------------------------------------------
# Lightweight data containers used by other modules
# ---------------------------------------------------------------------

PhotometricParameters = collections.namedtuple(
    "PhotometricParameters", "aperture annulus dannulus"
)


class Image(object):
    """In-memory representation of an image row."""

    __slots__ = (
        "path",
        "pfilter",
        "unix_time",
        "object",
        "airmass",
        "gain",
        "ra",
        "dec",
        "sources",
    )

    def __init__(
        self,
        path: str,
        pfilter,
        unix_time: Optional[float],
        object_: Optional[str],
        airmass: Optional[float],
        gain: Optional[float],
        ra: float,
        dec: float,
        sources: int = 0,
    ):
        self.path = path
        self.pfilter = pfilter  # Passband or str
        self.unix_time = unix_time
        self.object = object_
        self.airmass = airmass
        self.gain = gain
        self.ra = ra
        self.dec = dec
        self.sources = int(sources)


class LightCurve(object):
    """A light curve for one star in one passband, plus comparison star info."""

    def __init__(
        self,
        pfilter,
        cstars: Sequence[int],
        cweights: Sequence[float],
        cstdevs: Sequence[float],
        dtype=numpy.longdouble,
    ):
        self.pfilter = pfilter
        self.cstars = list(cstars)
        self.cweights = numpy.asarray(list(cweights), dtype=float)
        self.cstdevs = numpy.asarray(list(cstdevs), dtype=float)
        self.dtype = dtype

        self._times: List[float] = []
        self._mags: List[float] = []
        self._snrs: List[Optional[float]] = []

    def __len__(self) -> int:
        return len(self._times)

    @property
    def stdev(self) -> float:
        if len(self._mags) < 2:
            return 0.0
        return float(numpy.std(self._mags))

    def add(self, unix_time: float, magnitude: float, snr: Optional[float]):
        self._times.append(float(unix_time))
        self._mags.append(float(magnitude))
        self._snrs.append(None if snr is None else float(snr))

    @property
    def points(self) -> Iterator[Tuple[float, float, Optional[float]]]:
        return iter(zip(self._times, self._mags, self._snrs))


class DBStar(object):
    """
    Photometry time-series for one star in a single passband.

    Internal layout mimics the original project:
      _phot_info: 3 x N array -> [0]=times, [1]=mags, [2]=snrs
      _unix_times: 1 x N array (alias of _phot_info[0])
      _time_indexes: {unix_time -> index}
    """

    def __init__(
        self,
        star_id: int,
        pfilter,
        phot_info: numpy.ndarray,  # shape (3, N)
        time_index: Dict[float, int],
        dtype=numpy.longdouble,
    ):
        self.id = int(star_id)
        self.pfilter = pfilter
        self._phot_info = numpy.asarray(phot_info, dtype=dtype)
        self._unix_times = self._phot_info[0]
        self._time_indexes = dict(time_index)
        self.dtype = dtype

    def __len__(self) -> int:
        return self._phot_info.shape[1]

    def mag(self, idx: int) -> float:
        return float(self._phot_info[1, idx])

    def snr(self, idx: int) -> Optional[float]:
        val = self._phot_info[2, idx]
        return None if val is None else float(val)

    def time(self, idx: int) -> float:
        return float(self._unix_times[idx])

    def _time_index(self, unix_time: float) -> int:
        return self._time_indexes[float(unix_time)]

    def _trim_to(self, other: "DBStar") -> "DBStar":
        """Return a new DBStar containing only times present in `other`."""
        keep: List[int] = []
        t_index: Dict[float, int] = {}
        for i, t in enumerate(other._unix_times.tolist()):
            try:
                idx = self._time_indexes[float(t)]
            except KeyError:
                continue
            t_index[float(t)] = len(keep)
            keep.append(idx)
        if not keep:
            return DBStar(self.id, self.pfilter, self._phot_info[:, :0], {}, self.dtype)

        sub = numpy.empty((3, len(keep)), dtype=self.dtype)
        sub[0, :] = numpy.array([self._unix_times[i] for i in keep], dtype=self.dtype)
        sub[1, :] = numpy.array([self._phot_info[1, i] for i in keep], dtype=self.dtype)
        sub[2, :] = numpy.array([self._phot_info[2, i] for i in keep], dtype=self.dtype)
        return DBStar(self.id, self.pfilter, sub, t_index, self.dtype)

    # Factory
    @classmethod
    def make_star(
        cls,
        star_id: int,
        pfilter,
        rows: Sequence[Tuple[float, float, Optional[float]]],
        dtype=numpy.longdouble,
    ) -> "DBStar":
        """rows: iterable of (unix_time, magnitude, snr) ordered (or not)."""
        if not rows:
            empty = numpy.empty((3, 0), dtype=dtype)
            return cls(star_id, pfilter, empty, {}, dtype)

        rows = sorted(rows, key=lambda r: float(r[0]))
        n = len(rows)
        arr = numpy.empty((3, n), dtype=dtype)
        t_index: Dict[float, int] = {}
        for i, (t, m, s) in enumerate(rows):
            t = float(t)
            arr[0, i] = t
            arr[1, i] = float(m)
            arr[2, i] = numpy.nan if s is None else float(s)
            t_index[t] = i
        # store NaN for None SNRs; expose as None in snr()
        return cls(star_id, pfilter, arr, t_index, dtype)


# ---------------------------------------------------------------------
# Database access wrapper
# ---------------------------------------------------------------------


class UnknownStarError(KeyError):
    pass


class LEMONdB(object):
    """Small convenience wrapper around an SQLite LEMON database."""

    def __init__(self, path: str, isolation_level: Optional[str] = None, dtype=numpy.longdouble):
        self.path = path
        self.dtype = dtype
        self.connection = sqlite3.connect(self.path, isolation_level=isolation_level)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._cursor = self.connection.cursor()
        self._rows: Iterable[Tuple] = ()

    # -------------- context manager --------------
    def __enter__(self) -> "LEMONdB":
        return self

    def __exit__(self, exc_type, exc, tb):
        # leave commit control to callers; close cursor/connection
        with contextlib.suppress(Exception):
            self._cursor.close()
        with contextlib.suppress(Exception):
            self.connection.close()

    # -------------- low-level helpers --------------
    def _execute(self, sql: str, params: Sequence = ()):
        self._cursor.execute(sql, params)
        self._rows = self._cursor.fetchall()
        return self._rows

    def commit(self):
        self.connection.commit()

    def analyze(self):
        self.connection.execute("ANALYZE")
        self.connection.commit()

    # -------------- metadata helpers --------------
    def _meta_set(self, key: str, value: str):
        self.connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, str(value))
        )

    @property
    def date(self) -> Optional[float]:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='date'").fetchone()
            return None if row is None else float(row[0])
        except sqlite3.OperationalError:
            return None

    @date.setter
    def date(self, val: float):
        self._meta_set("date", val)

    @property
    def author(self) -> Optional[str]:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='author'").fetchone()
            return None if row is None else str(row[0])
        except sqlite3.OperationalError:
            return None

    @author.setter
    def author(self, val: str):
        self._meta_set("author", val)

    @property
    def hostname(self) -> Optional[str]:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='hostname'").fetchone()
            return None if row is None else str(row[0])
        except sqlite3.OperationalError:
            return None

    @hostname.setter
    def hostname(self, val: str):
        self._meta_set("hostname", val)

    @property
    def id(self) -> Optional[str]:
        try:
            row = self.connection.execute("SELECT value FROM metadata WHERE key='id'").fetchone()
            return None if row is None else str(row[0])
        except sqlite3.OperationalError:
            return None

    @id.setter
    def id(self, val: str):
        self._meta_set("id", val)

    # -------------- filters (by NAME, never by hash) --------------
    def _get_filter_id(self, pfilter) -> int:
        """Return stable filter_id for this Passband/string; create by name if needed."""
        name = str(pfilter)
        row = self.connection.execute(
            "SELECT id FROM photometric_filters WHERE name = ?", (name,)
        ).fetchone()
        if row:
            return int(row[0])
        cur = self.connection.execute(
            "INSERT INTO photometric_filters(name) VALUES (?)", (name,)
        )
        return int(cur.lastrowid)

    def _ensure_filter(self, pfilter):
        self.connection.execute(
            "INSERT OR IGNORE INTO photometric_filters(name) VALUES (?)", (str(pfilter),)
        )

    @property
    def pfilters(self) -> List[Passband]:
        """Return passbands present in the database (from photometric_filters joined to images)."""
        rows = self.connection.execute(
            "SELECT DISTINCT f.name "
            "FROM photometric_filters AS f "
            "JOIN images AS i ON i.filter_id = f.id "
            "ORDER BY f.name"
        ).fetchall()
        return [Passband(r[0]) for r in rows]

    # Back-compat alias some code may call
    def filters(self) -> List[Passband]:
        return list(self.pfilters)

    # -------------- stars --------------
    @property
    def star_ids(self) -> List[int]:
        rows = self.connection.execute("SELECT id FROM stars ORDER BY id").fetchall()
        return [int(r[0]) for r in rows]

    def nstars(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM stars").fetchone()
        return int(row[0]) if row else 0

    def add_star(self, id_: int, x: float, y: float, ra: float, dec: float,
                 epoch: float, pm_ra: Optional[float], pm_dec: Optional[float], imag: float):
        self.connection.execute(
            "INSERT OR REPLACE INTO stars(id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(id_), float(x), float(y), float(ra), float(dec), float(epoch),
             None if pm_ra is None else float(pm_ra),
             None if pm_dec is None else float(pm_dec),
             float(imag))
        )

    def get_star(self, star_id: int) -> Tuple:
        row = self.connection.execute(
            "SELECT id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag "
            "FROM stars WHERE id = ?",
            (int(star_id),)
        ).fetchone()
        if not row:
            raise UnknownStarError(f"star with ID = {star_id} not in database")
        return row

    # -------------- images --------------
    def _get_image_id(self, unix_time: float, pfilter) -> int:
        """Return image id for (unix_time, pfilter-name)."""
        row = self.connection.execute(
            "SELECT img.id "
            "FROM images AS img "
            "JOIN photometric_filters AS f ON img.filter_id = f.id "
            "WHERE img.unix_time = ? AND f.name = ?",
            (float(unix_time), str(pfilter)),
        ).fetchone()
        if not row:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, util.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        return int(row[0])

    def add_image(self, img: Image):
        """Insert image row if not present (sources defaults to 0)."""
        self._ensure_filter(img.pfilter)
        filter_id = self._get_filter_id(img.pfilter)

        # INSERT OR IGNORE to respect UNIQUE(filter_id, unix_time)
        self.connection.execute(
            "INSERT OR IGNORE INTO images(path, filter_id, unix_time, object, airmass, gain, ra, dec, sources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                img.path,
                filter_id,
                None if img.unix_time is None else float(img.unix_time),
                img.object,
                None if img.airmass is None else float(img.airmass),
                None if img.gain is None else float(img.gain),
                float(img.ra),
                float(img.dec),
                int(img.sources),
            ),
        )

    # -------------- photometry (instrumental) --------------
    def add_photometry(
        self,
        star_id: int,
        unix_time: float,
        pfilter,
        magnitude: float,
        snr: Optional[float],
    ):
        """Add one photometric measurement (instrumental mag) for (star,image)."""
        image_id = self._get_image_id(unix_time, pfilter)
        try:
            self.connection.execute(
                "INSERT INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                (int(star_id), image_id, float(magnitude), None if snr is None else float(snr)),
            )
        except sqlite3.IntegrityError:
            # If already exists, update values
            self.connection.execute(
                "UPDATE photometry SET magnitude=?, snr=? WHERE star_id=? AND image_id=?",
                (float(magnitude), None if snr is None else float(snr), int(star_id), image_id),
            )

        # keep images.sources in sync (increment by 1)
        self.connection.execute(
            "UPDATE images SET sources = sources + 1 WHERE id = ?", (image_id,)
        )

    def get_photometry(self, star_id: int, pfilter) -> DBStar:
        """Return instrumental photometry for star in the given filter."""
        if int(star_id) not in self.star_ids:
            raise UnknownStarError(f"star with ID = {star_id} not in database")

        rows = self.connection.execute(
            "SELECT img.unix_time, p.magnitude, p.snr "
            "FROM photometry AS p "
            "JOIN images AS img ON p.image_id = img.id "
            "JOIN photometric_filters AS f ON img.filter_id = f.id "
            "WHERE p.star_id = ? AND f.name = ? "
            "ORDER BY img.unix_time ASC",
            (int(star_id), str(pfilter)),
        ).fetchall()

        return DBStar.make_star(int(star_id), pfilter, rows, dtype=self.dtype)

    # -------------- differential curves --------------
    def _add_cmp_star(self, star_id: int, pfilter, cstar_id: int, cweight: float, cstdev: float):
        """Store one comparison star (unique per (star_id, filter_id, cstar_id))."""
        if int(star_id) == int(cstar_id):
            raise ValueError(f"star with ID = {star_id} cannot use itself as comparison")

        self._ensure_filter(pfilter)
        filter_id = self._get_filter_id(pfilter)
        try:
            self.connection.execute(
                "INSERT INTO cmp_stars(star_id, filter_id, cstar_id, stdev, weight) "
                "VALUES (?, ?, ?, ?, ?)",
                (int(star_id), filter_id, int(cstar_id), float(cstdev), float(cweight)),
            )
        except sqlite3.IntegrityError:
            # update existing entry
            self.connection.execute(
                "UPDATE cmp_stars SET stdev=?, weight=? "
                "WHERE star_id=? AND filter_id=? AND cstar_id=?",
                (float(cstdev), float(cweight), int(star_id), filter_id, int(cstar_id)),
            )

    def add_light_curve(self, star_id: int, curve: LightCurve):
        """
        Persist a light curve:
          - for each point: insert into light_curves (star_id, image_id, magnitude, snr)
          - store comparison stars into cmp_stars
        """
        self._ensure_filter(curve.pfilter)
        filter_id = self._get_filter_id(curve.pfilter)

        # Store comparison stars
        for cstar_id, w, sd in zip(curve.cstars, curve.cweights, curve.cstdevs):
            self._add_cmp_star(star_id, curve.pfilter, int(cstar_id), float(w), float(sd))

        # Store points
        for unix_time, magnitude, snr_val in curve.points:
            # Translate (filter, time) to image_id
            try:
                image_id = self._get_image_id(unix_time, curve.pfilter)
            except KeyError:
                # If the image is not present (shouldn't happen with proper DB),
                # skip this point.
                continue

            try:
                self.connection.execute(
                    "INSERT INTO light_curves(star_id, image_id, magnitude, snr) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        int(star_id),
                        image_id,
                        float(magnitude),
                        None if snr_val is None else float(snr_val),
                    ),
                )
            except sqlite3.IntegrityError:
                # Update existing curve point
                self.connection.execute(
                    "UPDATE light_curves SET magnitude=?, snr=? "
                    "WHERE star_id=? AND image_id=?",
                    (
                        float(magnitude),
                        None if snr_val is None else float(snr_val),
                        int(star_id),
                        image_id,
                    ),
                )

    def get_light_curve(self, star_id: int, pfilter) -> Optional[LightCurve]:
        """Load an existing light curve and its comparison stars for this star/filter."""
        pts = self.connection.execute(
            "SELECT img.unix_time, lc.magnitude, lc.snr "
            "FROM light_curves AS lc "
            "JOIN images AS img ON lc.image_id = img.id "
            "JOIN photometric_filters AS f ON img.filter_id = f.id "
            "WHERE lc.star_id = ? AND f.name = ? "
            "ORDER BY img.unix_time ASC",
            (int(star_id), str(pfilter)),
        ).fetchall()

        if not pts:
            return None

        rows = self.connection.execute(
            "SELECT cstar_id, weight, stdev "
            "FROM cmp_stars AS cs "
            "JOIN photometric_filters AS f ON cs.filter_id = f.id "
            "WHERE cs.star_id = ? AND f.name = ? "
            "ORDER BY cstar_id",
            (int(star_id), str(pfilter)),
        ).fetchall()

        if rows:
            cstars, cweights, cstdevs = zip(*rows)
        else:
            cstars, cweights, cstdevs = [], [], []

        lc = LightCurve(pfilter, cstars, cweights, cstdevs, dtype=self.dtype)
        for t, m, s in pts:
            lc.add(float(t), float(m), None if s is None else float(s))
        return lc

    # -------------- airmass helper --------------
    def airmasses(self, pfilter) -> Dict[float, Optional[float]]:
        """Return {unix_time: airmass} for images in a filter."""
        rows = self.connection.execute(
            "SELECT img.unix_time, img.airmass "
            "FROM images AS img "
            "JOIN photometric_filters AS f ON img.filter_id = f.id "
            "WHERE f.name = ?",
            (str(pfilter),),
        ).fetchall()
        return dict((float(t), None if a is None else float(a)) for t, a in rows)

    # -------------- (optional) proper motion corrections --------------
    def add_pm_correction(self, star_id: int, unix_time: float, pfilter, x: float, y: float):
        """
        Store PM correction pixel coordinates. If your DB does not have a dedicated
        table, you can remove this method or adapt it. We implement a no-op with
        an auxiliary table if present.
        """
        try:
            self.connection.execute(
                "INSERT INTO pm_corrections(star_id, image_id, x, y) VALUES (?,?,?,?)",
                (int(star_id), self._get_image_id(unix_time, pfilter), float(x), float(y)),
            )
        except sqlite3.OperationalError:
            # table not present in this DB; silently ignore
            pass
        except sqlite3.IntegrityError:
            # already present -> update
            try:
                self.connection.execute(
                    "UPDATE pm_corrections SET x=?, y=? WHERE star_id=? AND image_id=?",
                    (float(x), float(y), int(star_id), self._get_image_id(unix_time, pfilter)),
                )
            except sqlite3.OperationalError:
                pass

    # -------------- utility --------------
    def __len__(self) -> int:
        return self.nstars()
