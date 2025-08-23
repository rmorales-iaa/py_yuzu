#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import collections
import contextlib
import sqlite3
from typing import Iterable, Iterator, List, Optional, Sequence, Tuple, Dict

import numpy

import util
try:
    from passband import Passband
except Exception:  # pragma: no cover
    class Passband(str):
        def __new__(cls, name: str):
            return str.__new__(cls, name)


PhotometricParameters = collections.namedtuple(
    "PhotometricParameters", "aperture annulus dannulus"
)


class Image(object):
    __slots__ = ("path", "pfilter", "unix_time", "object", "airmass", "gain", "ra", "dec", "sources")

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
        self.pfilter = pfilter
        self.unix_time = unix_time
        self.object = object_
        self.airmass = airmass
        self.gain = gain
        self.ra = ra
        self.dec = dec
        self.sources = int(sources)


class LightCurve(object):
    def __init__(self, pfilter, cstars, cweights, cstdevs, dtype=numpy.longdouble):
        self.pfilter = pfilter
        self.cstars = list(cstars)
        self.cweights = numpy.asarray(list(cweights), dtype=float)
        self.cstdevs = numpy.asarray(list(cstdevs), dtype=float)
        self.dtype = dtype
        self._times: List[float] = []
        self._mags: List[float] = []
        self._snrs: List[Optional[float]] = []

    def __len__(self): return len(self._times)
    @property
    def stdev(self): return float(numpy.std(self._mags)) if len(self._mags) > 1 else 0.0
    def add(self, t, m, s): self._times.append(float(t)); self._mags.append(float(m)); self._snrs.append(None if s is None else float(s))
    @property
    def points(self): return iter(zip(self._times, self._mags, self._snrs))


class DBStar(object):
    def __init__(self, star_id, pfilter, phot_info, time_index, dtype=numpy.longdouble):
        self.id = int(star_id)
        self.pfilter = pfilter
        self._phot_info = numpy.asarray(phot_info, dtype=dtype)
        self._unix_times = self._phot_info[0]
        self._time_indexes = dict(time_index)
        self.dtype = dtype

    def __len__(self): return self._phot_info.shape[1]
    def mag(self, idx): return float(self._phot_info[1, idx])
    def snr(self, idx):
        val = self._phot_info[2, idx]
        try: return None if numpy.isnan(val) else float(val)
        except Exception: return None if val is None else float(val)
    def time(self, idx): return float(self._unix_times[idx])
    def _time_index(self, t): return self._time_indexes[float(t)]

    def _trim_to(self, other: "DBStar") -> "DBStar":
        keep, t_index = [], {}
        for t in other._unix_times.tolist():
            idx = self._time_indexes.get(float(t))
            if idx is None: continue
            t_index[float(t)] = len(keep)
            keep.append(idx)
        if not keep:
            return DBStar(self.id, self.pfilter, self._phot_info[:, :0], {}, self.dtype)
        sub = numpy.empty((3, len(keep)), dtype=self.dtype)
        sub[0, :] = numpy.array([self._unix_times[i] for i in keep], dtype=self.dtype)
        sub[1, :] = numpy.array([self._phot_info[1, i] for i in keep], dtype=self.dtype)
        sub[2, :] = numpy.array([self._phot_info[2, i] for i in keep], dtype=self.dtype)
        return DBStar(self.id, self.pfilter, sub, t_index, self.dtype)

    @classmethod
    def make_star(cls, star_id, pfilter, rows, dtype=numpy.longdouble) -> "DBStar":
        if not rows:
            return cls(star_id, pfilter, numpy.empty((3, 0), dtype=dtype), {}, dtype)
        rows = sorted(rows, key=lambda r: float(r[0]))
        n = len(rows)
        arr = numpy.empty((3, n), dtype=dtype)
        t_index = {}
        for i, (t, m, s) in enumerate(rows):
            t = float(t)
            arr[0, i] = t
            arr[1, i] = float(m)
            arr[2, i] = numpy.nan if s is None else float(s)
            t_index[t] = i
        return cls(star_id, pfilter, arr, t_index, dtype)


class UnknownStarError(KeyError): pass


class LEMONdB(object):
    def __init__(self, path: str, isolation_level: Optional[str] = None, dtype: numpy.longdouble = numpy.longdouble):
        self.path = path
        self.dtype = dtype
        self.connection = sqlite3.connect(self.path, isolation_level=isolation_level)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._cursor = self.connection.cursor()
        self._rows: Iterable[Tuple] = ()
        self._simage: Optional[Image] = None
        self._ensure_schema()

    def __enter__(self): return self
    def __exit__(self, exc_type, exc, tb):
        with contextlib.suppress(Exception): self._cursor.close()
        with contextlib.suppress(Exception): self.connection.close()

    # ---------- schema ----------
    def _ensure_schema(self):
        cur = self.connection.cursor()
        cur.execute("BEGIN")
        cur.execute("""CREATE TABLE IF NOT EXISTS photometric_filters (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS stars (
            id INTEGER PRIMARY KEY,x REAL NOT NULL,y REAL NOT NULL,ra REAL NOT NULL,dec REAL NOT NULL,
            epoch REAL NOT NULL,pm_ra REAL,pm_dec REAL,imag REAL NOT NULL)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY,path TEXT NOT NULL,filter_id INTEGER,unix_time REAL,object TEXT,
            airmass REAL,gain REAL,ra REAL NOT NULL,dec REAL NOT NULL,sources INTEGER NOT NULL,
            FOREIGN KEY(filter_id) REFERENCES photometric_filters(id),
            UNIQUE (filter_id, unix_time))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS photometry (
            star_id INTEGER NOT NULL,image_id INTEGER NOT NULL,magnitude REAL NOT NULL,snr REAL,
            FOREIGN KEY(star_id) REFERENCES stars(id),FOREIGN KEY(image_id) REFERENCES images(id),
            UNIQUE (star_id, image_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS light_curves (
            id INTEGER PRIMARY KEY,star_id INTEGER NOT NULL,image_id INTEGER NOT NULL,magnitude REAL NOT NULL,snr REAL,
            FOREIGN KEY(star_id) REFERENCES stars(id),FOREIGN KEY(image_id) REFERENCES images(id),
            UNIQUE (star_id, image_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS cmp_stars (
            id INTEGER PRIMARY KEY,star_id INTEGER NOT NULL,filter_id INTEGER NOT NULL,cstar_id INTEGER NOT NULL,
            stdev REAL NOT NULL,weight REAL NOT NULL,
            FOREIGN KEY(star_id) REFERENCES stars(id),FOREIGN KEY(filter_id) REFERENCES photometric_filters(id),
            UNIQUE (star_id, filter_id, cstar_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS candidate_pparams (
            id INTEGER PRIMARY KEY,filter_id INTEGER NOT NULL,aperture REAL NOT NULL,annulus REAL NOT NULL,
            dannulus REAL NOT NULL,rank INTEGER DEFAULT 0,
            FOREIGN KEY(filter_id) REFERENCES photometric_filters(id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)""")
        cur.execute("CREATE INDEX IF NOT EXISTS img_by_filter_time ON images(filter_id, unix_time)")
        cur.execute("CREATE INDEX IF NOT EXISTS phot_by_star_image ON photometry(star_id, image_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS curve_by_star_image ON light_curves(star_id, image_id)")
        cur.execute("COMMIT")

    # ---------- fast ingest context ----------
    @contextlib.contextmanager
    def fast_transaction(self):
        """Single high-throughput transaction with safe PRAGMA tweaks."""
        cur = self.connection
        orig_journal = cur.execute("PRAGMA journal_mode").fetchone()[0]
        orig_sync = cur.execute("PRAGMA synchronous").fetchone()[0]
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=OFF")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("BEGIN IMMEDIATE")
            yield
            cur.commit()
        finally:
            cur.execute(f"PRAGMA synchronous={orig_sync}")
            cur.execute(f"PRAGMA journal_mode={orig_journal}")

    # ---------- low-level helpers ----------
    def _execute(self, sql: str, params: Sequence = ()):
        self._cursor.execute(sql, params)
        self._rows = self._cursor.fetchall()
        return self._rows

    def commit(self): self.connection.commit()
    def analyze(self): self.connection.execute("ANALYZE"); self.connection.commit()

    # ---------- metadata ----------
    def _meta_set(self, key, value):
        self.connection.execute("INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)", (key, str(value)))
    @property
    def date(self): row = self.connection.execute("SELECT value FROM metadata WHERE key='date'").fetchone(); return None if row is None else float(row[0])
    @date.setter
    def date(self, v): self._meta_set("date", v)
    @property
    def author(self): row = self.connection.execute("SELECT value FROM metadata WHERE key='author'").fetchone(); return None if row is None else str(row[0])
    @author.setter
    def author(self, v): self._meta_set("author", v)
    @property
    def hostname(self): row = self.connection.execute("SELECT value FROM metadata WHERE key='hostname'").fetchone(); return None if row is None else str(row[0])
    @hostname.setter
    def hostname(self, v): self._meta_set("hostname", v)
    @property
    def id(self): row = self.connection.execute("SELECT value FROM metadata WHERE key='id'").fetchone(); return None if row is None else str(row[0])
    @id.setter
    def id(self, v): self._meta_set("id", v)

    # ---------- filters ----------
    def _get_filter_id(self, pfilter) -> Optional[int]:
        if pfilter is None: return None
        name = str(pfilter)
        row = self.connection.execute("SELECT id FROM photometric_filters WHERE name = ?", (name,)).fetchone()
        if row: return int(row[0])
        cur = self.connection.execute("INSERT INTO photometric_filters(name) VALUES (?)", (name,))
        return int(cur.lastrowid)

    def _ensure_filter(self, pfilter):
        if pfilter is None: return
        self.connection.execute("INSERT OR IGNORE INTO photometric_filters(name) VALUES (?)", (str(pfilter),))

    @property
    def pfilters(self) -> List[Passband]:
        rows = self.connection.execute(
            "SELECT DISTINCT f.name FROM photometric_filters f JOIN images i ON i.filter_id = f.id ORDER BY f.name"
        ).fetchall()
        return [Passband(r[0]) for r in rows]

    def filters(self): return list(self.pfilters)

    # ---------- stars ----------
    @property
    def star_ids(self) -> List[int]:
        rows = self.connection.execute("SELECT id FROM stars ORDER BY id").fetchall()
        return [int(r[0]) for r in rows]

    def nstars(self) -> int:
        row = self.connection.execute("SELECT COUNT(*) FROM stars").fetchone()
        return int(row[0]) if row else 0

    def add_star(self, id_, x, y, ra, dec, epoch, pm_ra, pm_dec, imag):
        self.connection.execute(
            "INSERT OR REPLACE INTO stars(id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag) VALUES (?,?,?,?,?,?,?,?,?)",
            (int(id_), float(x), float(y), float(ra), float(dec), float(epoch),
             None if pm_ra is None else float(pm_ra),
             None if pm_dec is None else float(pm_dec),
             float(imag))
        )

    def get_star(self, star_id):
        row = self.connection.execute(
            "SELECT id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag FROM stars WHERE id = ?",
            (int(star_id),)
        ).fetchone()
        if not row: raise UnknownStarError(f"star with ID = {star_id} not in database")
        return row

    # ---------- images ----------
    def _get_image_id(self, unix_time: float, pfilter) -> int:
        row = self.connection.execute(
            "SELECT img.id FROM images img JOIN photometric_filters f ON img.filter_id = f.id "
            "WHERE img.unix_time = ? AND f.name = ?",
            (float(unix_time), str(pfilter)),
        ).fetchone()
        if not row:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, util.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        return int(row[0])

    def add_image(self, img: Image):
        filter_id = None
        if img.pfilter is not None:
            self._ensure_filter(img.pfilter)
            filter_id = self._get_filter_id(img.pfilter)
        self.connection.execute(
            "INSERT OR IGNORE INTO images(path, filter_id, unix_time, object, airmass, gain, ra, dec, sources) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (img.path, filter_id,
             None if img.unix_time is None else float(img.unix_time),
             img.object,
             None if img.airmass is None else float(img.airmass),
             None if img.gain is None else float(img.gain),
             float(img.ra), float(img.dec), int(img.sources))
        )

    def get_or_add_image_id(self, img: Image) -> int:
        self.add_image(img)
        # Prefer key by (filter, time)
        if img.pfilter is not None and img.unix_time is not None:
            return self._get_image_id(img.unix_time, img.pfilter)
        # Fallback by path (should not be used for photometry frames)
        row = self.connection.execute("SELECT id FROM images WHERE path=? ORDER BY id DESC LIMIT 1", (img.path,)).fetchone()
        if not row:
            raise KeyError(f"image not found: {img.path}")
        return int(row[0])

    # ---------- photometry ----------
    def add_photometry(self, star_id, unix_time, pfilter, magnitude, snr):
        image_id = self._get_image_id(unix_time, pfilter)
        try:
            self.connection.execute(
                "INSERT INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                (int(star_id), image_id, float(magnitude), None if snr is None else float(snr)),
            )
        except sqlite3.IntegrityError:
            self.connection.execute(
                "UPDATE photometry SET magnitude=?, snr=? WHERE star_id=? AND image_id=?",
                (float(magnitude), None if snr is None else float(snr), int(star_id), image_id),
            )
        self.connection.execute("UPDATE images SET sources = sources + 1 WHERE id = ?", (image_id,))

    def add_photometry_bulk(self, rows: List[Tuple[int, int, float, Optional[float]]]):
        """
        Bulk upsert photometry.
        rows: list of (star_id, image_id, magnitude, snr)
        """
        if not rows:
            return
        # INSERT OR REPLACE emulation: try insert, on conflict update
        try:
            self.connection.executemany(
                "INSERT INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                [(int(s), int(img), float(m), None if snr is None else float(snr)) for s, img, m, snr in rows],
            )
        except sqlite3.IntegrityError:
            # Fallback path: do updates one-by-one for existing pairs
            for s, img, m, snr in rows:
                try:
                    self.connection.execute(
                        "INSERT INTO photometry(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                        (int(s), int(img), float(m), None if snr is None else float(snr)),
                    )
                except sqlite3.IntegrityError:
                    self.connection.execute(
                        "UPDATE photometry SET magnitude=?, snr=? WHERE star_id=? AND image_id=?",
                        (float(m), None if snr is None else float(snr), int(s), int(img)),
                    )

        # Bump images.sources once per image_id
        cnt = collections.Counter(int(img) for _, img, _, _ in rows)
        self.connection.executemany(
            "UPDATE images SET sources = sources + ? WHERE id = ?",
            [(n, img) for img, n in cnt.items()],
        )

    def get_photometry(self, star_id: int, pfilter) -> DBStar:
        rows = self.connection.execute(
            "SELECT img.unix_time, p.magnitude, p.snr "
            "FROM photometry p JOIN images img ON p.image_id = img.id "
            "JOIN photometric_filters f ON img.filter_id = f.id "
            "WHERE p.star_id = ? AND f.name = ? ORDER BY img.unix_time ASC",
            (int(star_id), str(pfilter)),
        ).fetchall()
        return DBStar.make_star(int(star_id), pfilter, rows, dtype=self.dtype)

    # ---------- differential curves ----------
    def _add_cmp_star(self, star_id, pfilter, cstar_id, cweight, cstdev):
        if int(star_id) == int(cstar_id):
            raise ValueError(f"star with ID = {star_id} cannot use itself as comparison")
        self._ensure_filter(pfilter)
        fid = self._get_filter_id(pfilter)
        try:
            self.connection.execute(
                "INSERT INTO cmp_stars(star_id, filter_id, cstar_id, stdev, weight) VALUES (?, ?, ?, ?, ?)",
                (int(star_id), fid, int(cstar_id), float(cstdev), float(cweight)),
            )
        except sqlite3.IntegrityError:
            self.connection.execute(
                "UPDATE cmp_stars SET stdev=?, weight=? WHERE star_id=? AND filter_id=? AND cstar_id=?",
                (float(cstdev), float(cweight), int(star_id), fid, int(cstar_id)),
            )

    def add_light_curve(self, star_id: int, curve: LightCurve):
        self._ensure_filter(curve.pfilter)
        for cstar_id, w, sd in zip(curve.cstars, curve.cweights, curve.cstdevs):
            self._add_cmp_star(star_id, curve.pfilter, int(cstar_id), float(w), float(sd))
        for t, m, s in curve.points:
            try:
                image_id = self._get_image_id(t, curve.pfilter)
            except KeyError:
                continue
            try:
                self.connection.execute(
                    "INSERT INTO light_curves(star_id, image_id, magnitude, snr) VALUES (?, ?, ?, ?)",
                    (int(star_id), image_id, float(m), None if s is None else float(s)),
                )
            except sqlite3.IntegrityError:
                self.connection.execute(
                    "UPDATE light_curves SET magnitude=?, snr=? WHERE star_id=? AND image_id=?",
                    (float(m), None if s is None else float(s), int(star_id), image_id),
                )

    def get_light_curve(self, star_id: int, pfilter) -> Optional[LightCurve]:
        pts = self.connection.execute(
            "SELECT img.unix_time, lc.magnitude, lc.snr "
            "FROM light_curves lc JOIN images img ON lc.image_id = img.id "
            "JOIN photometric_filters f ON img.filter_id = f.id "
            "WHERE lc.star_id = ? AND f.name = ? ORDER BY img.unix_time ASC",
            (int(star_id), str(pfilter)),
        ).fetchall()
        if not pts: return None
        rows = self.connection.execute(
            "SELECT cstar_id, weight, stdev FROM cmp_stars cs "
            "JOIN photometric_filters f ON cs.filter_id = f.id "
            "WHERE cs.star_id = ? AND f.name = ? ORDER BY cstar_id",
            (int(star_id), str(pfilter)),
        ).fetchall()
        cstars, cweights, cstdevs = zip(*rows) if rows else ([], [], [])
        lc = LightCurve(pfilter, cstars, cweights, cstdevs, dtype=self.dtype)
        for t, m, s in pts:
            lc.add(float(t), float(m), None if s is None else float(s))
        return lc

    # ---------- candidate annuli ----------
    def add_candidate_pparams(self, cand, pfilter, rank: int = 0):
        self._ensure_filter(pfilter)
        fid = self._get_filter_id(pfilter)
        self.connection.execute(
            "INSERT INTO candidate_pparams(filter_id, aperture, annulus, dannulus, rank) VALUES (?, ?, ?, ?, ?)",
            (fid, float(cand.aperture), float(cand.annulus), float(cand.dannulus), int(rank)),
        )

    # ---------- airmass ----------
    def airmasses(self, pfilter) -> Dict[float, Optional[float]]:
        rows = self.connection.execute(
            "SELECT img.unix_time, img.airmass FROM images img "
            "JOIN photometric_filters f ON img.filter_id = f.id WHERE f.name = ?",
            (str(pfilter),),
        ).fetchall()
        return dict((float(t), None if a is None else float(a)) for t, a in rows)

    def __len__(self): return self.nstars()
