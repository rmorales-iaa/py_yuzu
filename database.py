#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SQLAlchemy-based, thread-safe implementation of the astronomy database API
aligned to the provided SQLite schemas.

- Uses SQLAlchemy 2.0 style ORM.
- Each thread uses its own Session via scoped_session.
- SQLite pragmas configured on connect: foreign_keys=ON, busy_timeout=5000, WAL.
- Public API mirrors the earlier sqlite3-based version where practical.

Install:
    pip install sqlalchemy

Note: This module defines domain helper classes (Image, LightCurve, DBStar) to keep
the external API similar to the previous implementation.
"""

from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy

from sqlalchemy import (
    BLOB,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Index,
    create_engine,
    event,
    select,
    func,
)
from sqlalchemy.orm import (
    Session,
    declarative_base,
    relationship,
    scoped_session,
    sessionmaker,
)

# ----------------------- domain helpers (compat) -----------------------

PhotometricParametersTuple = tuple  # kept for compatibility if needed

@dataclass
class PhotometricParameters:
    aperture: int
    annulus: int
    dannulus: int

@dataclass
class Image:
    path: str
    pfilter: Optional[str]
    unix_time: Optional[float]
    object: Optional[str]
    airmass: Optional[float]
    gain: Optional[float]
    ra: float
    dec: float
    sources: int = 0

class LightCurve:
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

class DBStar:
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

# ----------------------- SQLAlchemy ORM models -----------------------

Base = declarative_base()

class PhotometricFilter(Base):
    __tablename__ = "photometric_filters"
    id = Column(Integer, primary_key=True)
    name = Column(Text, unique=True, nullable=False)

class Star(Base):
    __tablename__ = "stars"
    id = Column(Integer, primary_key=True)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    ra = Column(Float, nullable=False)
    dec = Column(Float, nullable=False)
    epoch = Column(Float, nullable=False)
    pm_ra = Column(Float)
    pm_dec = Column(Float)
    imag = Column(Float, nullable=False)

class ImageRow(Base):
    __tablename__ = "images"
    id = Column(Integer, primary_key=True)
    path = Column(Text, nullable=False)
    filter_id = Column(Integer, ForeignKey("photometric_filters.id"))
    unix_time = Column(Float)
    object = Column(Text)
    airmass = Column(Float)
    gain = Column(Float)
    ra = Column(Float, nullable=False)
    dec = Column(Float, nullable=False)
    sources = Column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("filter_id", "unix_time"),
        Index("img_by_filter_time", "filter_id", "unix_time"),
    )

class Photometry(Base):
    __tablename__ = "photometry"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude = Column(Float, nullable=False)
    snr = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
        Index("phot_by_star_image", "star_id", "image_id"),
        Index("phot_by_image", "image_id"),
    )

class LightCurveRow(Base):
    __tablename__ = "light_curves"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude = Column(Float, nullable=False)
    snr = Column(Float)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
        Index("curve_by_star_image", "star_id", "image_id"),
    )

class CmpStar(Base):
    __tablename__ = "cmp_stars"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("photometric_filters.id"), nullable=False)
    cstar_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    stdev = Column(Float, nullable=False)
    weight = Column(Float, nullable=False)

    __table_args__ = (
        Index("cstars_by_star_filter", "star_id", "filter_id"),
    )

class PhotometricParametersRow(Base):
    __tablename__ = "photometric_parameters"
    id = Column(Integer, primary_key=True)
    aperture = Column(Integer, nullable=False)
    annulus = Column(Integer, nullable=False)
    dannulus = Column(Integer, nullable=False)

    __table_args__ = (
        Index("phot_params_all_rows", "aperture", "annulus", "dannulus"),
    )

class CandidateParameters(Base):
    __tablename__ = "candidate_parameters"
    id = Column(Integer, primary_key=True)
    pparams_id = Column(Integer, ForeignKey("photometric_parameters.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("photometric_filters.id"), nullable=False)
    stdev = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("pparams_id", "filter_id"),
        Index("cand_filter", "filter_id"),
    )

class PMCorrection(Base):
    __tablename__ = "pm_corrections"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
    )

class RawImage(Base):
    __tablename__ = "raw_images"
    id = Column(Integer, ForeignKey("images.id"), primary_key=True)
    fits = Column(BLOB, nullable=False)

class Metadata(Base):
    __tablename__ = "metadata"
    key = Column(Text, primary_key=True)  # UNIQUE(key)
    value = Column(BLOB)

# ----------------------- DB facade -----------------------

class LEMONSA:
    """
    Thread-safe facade around SQLAlchemy ORM.
    - `scoped_session` gives one Session per thread.
    - Use `with db.session() as s:` or `db.session()` as a context manager.
    """
    def __init__(self, path: str, dtype: numpy.longdouble = numpy.longdouble, echo: bool = False):
        self.path = path
        self.dtype = dtype
        self._db_lock = threading.RLock()

        self.engine = create_engine(
            f"sqlite:///{path}",
            echo=echo,
            future=True,
            pool_pre_ping=True,
        )

        # Set SQLite pragmas per-connection
        @event.listens_for(self.engine, "connect")
        def _sqlite_on_connect(dbapi_conn, conn_rec):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=5000")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        self._SessionFactory = scoped_session(sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False))
        Base.metadata.create_all(self.engine)

    # ------------- session helpers -------------
    def session(self) -> Session:
        """Return the thread-local Session (use as context manager: `with db.session() as s:`)."""
        return self._SessionFactory()

    def close(self):
        """Close current thread's Session."""
        with contextlib.suppress(Exception):
            self._SessionFactory.remove()

    def close_all(self):
        """Best-effort dispose engine and remove sessions (call when fully shutting down)."""
        self._SessionFactory.remove()
        with contextlib.suppress(Exception):
            self.engine.dispose()

    # ------------- metadata -------------
    def _meta_set(self, key: str, value):
        with self.session() as s:
            row = s.get(Metadata, key)
            if row is None:
                s.add(Metadata(key=key, value=value))
            else:
                row.value = value
            s.commit()

    @property
    def date(self):
        with self.session() as s:
            row = s.get(Metadata, "date")
            return None if row is None else float(row.value)

    @date.setter
    def date(self, v): self._meta_set("date", v)

    @property
    def author(self):
        with self.session() as s:
            row = s.get(Metadata, "author")
            return None if row is None else (row.value.decode() if isinstance(row.value, (bytes, bytearray)) else str(row.value))

    @author.setter
    def author(self, v): self._meta_set("author", v)

    @property
    def hostname(self):
        with self.session() as s:
            row = s.get(Metadata, "hostname")
            return None if row is None else (row.value.decode() if isinstance(row.value, (bytes, bytearray)) else str(row.value))

    @hostname.setter
    def hostname(self, v): self._meta_set("hostname", v)

    @property
    def id(self):
        with self.session() as s:
            row = s.get(Metadata, "id")
            return None if row is None else (row.value.decode() if isinstance(row.value, (bytes, bytearray)) else str(row.value))

    @id.setter
    def id(self, v): self._meta_set("id", v)

    # ------------- filters -------------
    def _get_filter_id(self, pfilter: Optional[str]) -> Optional[int]:
        if pfilter is None:
            return None
        name = str(pfilter)
        with self.session() as s:
            row = s.execute(select(PhotometricFilter).where(PhotometricFilter.name == name)).scalar_one_or_none()
            if row is None:
                row = PhotometricFilter(name=name)
                s.add(row)
                s.commit()
                s.refresh(row)
            return int(row.id)

    def _ensure_filter(self, pfilter: Optional[str]):
        if pfilter is None: return
        _ = self._get_filter_id(pfilter)

    @property
    def pfilters(self) -> List[str]:
        with self.session() as s:
            rows = s.execute(
                select(PhotometricFilter.name)
                .join(ImageRow, ImageRow.filter_id == PhotometricFilter.id)
                .distinct()
                .order_by(PhotometricFilter.name)
            ).all()
            return [r[0] for r in rows]

    def filters(self): return list(self.pfilters)

    # ------------- stars -------------
    @property
    def star_ids(self) -> List[int]:
        with self.session() as s:
            rows = s.execute(select(Star.id).order_by(Star.id)).all()
            return [int(r[0]) for r in rows]

    def nstars(self) -> int:
        with self.session() as s:
            return int(s.execute(select(func.count(Star.id))).scalar_one())

    def add_star(self, id_, x, y, ra, dec, epoch, pm_ra, pm_dec, imag):
        with self.session() as s:
            row = s.get(Star, int(id_))
            if row is None:
                row = Star(
                    id=int(id_), x=float(x), y=float(y), ra=float(ra), dec=float(dec),
                    epoch=float(epoch),
                    pm_ra=None if pm_ra is None else float(pm_ra),
                    pm_dec=None if pm_dec is None else float(pm_dec),
                    imag=float(imag),
                )
                s.add(row)
            else:
                row.x=float(x); row.y=float(y); row.ra=float(ra); row.dec=float(dec)
                row.epoch=float(epoch); row.pm_ra=None if pm_ra is None else float(pm_ra)
                row.pm_dec=None if pm_dec is None else float(pm_dec); row.imag=float(imag)
            s.commit()

    def get_star(self, star_id):
        with self.session() as s:
            row = s.get(Star, int(star_id))
            if not row: raise UnknownStarError(f"star with ID = {star_id} not in database")
            return (row.id, row.x, row.y, row.ra, row.dec, row.epoch, row.pm_ra, row.pm_dec, row.imag)

    # ------------- images -------------
    def _get_image_id(self, unix_time: float, pfilter) -> int:
        with self.session() as s:
            fid = self._get_filter_id(pfilter)
            row = s.execute(select(ImageRow.id).where(ImageRow.unix_time == float(unix_time), ImageRow.filter_id == fid)).first()
            if not row:
                raise KeyError(f"image not found for time={unix_time} and filter={pfilter}")
            return int(row[0])

    def add_image(self, img: Image):
        with self.session() as s:
            fid = self._get_filter_id(img.pfilter) if img.pfilter is not None else None
            # Try find by unique key (filter_id, unix_time) if both present
            if fid is not None and img.unix_time is not None:
                row = s.execute(
                    select(ImageRow).where(ImageRow.filter_id == fid, ImageRow.unix_time == float(img.unix_time))
                ).scalar_one_or_none()
            else:
                row = s.execute(select(ImageRow).where(ImageRow.path == img.path)).scalar_one_or_none()

            if row is None:
                row = ImageRow(
                    path=img.path, filter_id=fid,
                    unix_time=None if img.unix_time is None else float(img.unix_time),
                    object=img.object,
                    airmass=None if img.airmass is None else float(img.airmass),
                    gain=None if img.gain is None else float(img.gain),
                    ra=float(img.ra), dec=float(img.dec), sources=int(img.sources)
                )
                s.add(row)
            else:
                row.path = img.path
                row.filter_id = fid
                row.unix_time = None if img.unix_time is None else float(img.unix_time)
                row.object = img.object
                row.airmass = None if img.airmass is None else float(img.airmass)
                row.gain = None if img.gain is None else float(img.gain)
                row.ra = float(img.ra)
                row.dec = float(img.dec)
                row.sources = int(img.sources)
            s.commit()

    def get_or_add_image_id(self, img: Image) -> int:
        self.add_image(img)
        with self.session() as s:
            if img.pfilter is not None and img.unix_time is not None:
                fid = self._get_filter_id(img.pfilter)
                row = s.execute(
                    select(ImageRow.id).where(ImageRow.filter_id == fid, ImageRow.unix_time == float(img.unix_time))
                ).first()
                if row: return int(row[0])
            row = s.execute(select(ImageRow.id).where(ImageRow.path == img.path).order_by(ImageRow.id.desc())).first()
            if not row:
                raise KeyError(f"image not found: {img.path}")
            return int(row[0])

    # ------------- photometry -------------
    def add_photometry(self, star_id, unix_time, pfilter, magnitude, snr):
        if snr is None:
            raise ValueError("photometry.snr is NOT NULL by schema; received None")
        with self.session() as s:
            image_id = self._get_image_id(unix_time, pfilter)
            row = s.execute(select(Photometry).where(
                Photometry.star_id==int(star_id), Photometry.image_id==image_id
            )).scalar_one_or_none()
            if row is None:
                row = Photometry(star_id=int(star_id), image_id=image_id, magnitude=float(magnitude), snr=float(snr))
                s.add(row)
            else:
                row.magnitude = float(magnitude)
                row.snr = float(snr)
            # bump sources
            img = s.get(ImageRow, image_id)
            img.sources = int(img.sources) + 1
            s.commit()

    def add_photometry_bulk(self, rows: List[Tuple[int, int, float, float]]):
        """rows: list of (star_id, image_id, magnitude, snr)"""
        if not rows: return
        with self.session() as s:
            for s_id, img_id, mag, snr in rows:
                r = s.execute(select(Photometry).where(
                    Photometry.star_id==int(s_id), Photometry.image_id==int(img_id)
                )).scalar_one_or_none()
                if r is None:
                    s.add(Photometry(star_id=int(s_id), image_id=int(img_id), magnitude=float(mag), snr=float(snr)))
                else:
                    r.magnitude=float(mag); r.snr=float(snr)
            # bump images.sources once per image_id
            from collections import Counter
            cnt = Counter(int(img) for _, img, _, _ in rows)
            for img_id, n in cnt.items():
                img = s.get(ImageRow, int(img_id))
                img.sources = int(img.sources) + int(n)
            s.commit()

    def get_photometry(self, star_id: int, pfilter) -> DBStar:
        with self.session() as s:
            rows = s.execute(
                select(ImageRow.unix_time, Photometry.magnitude, Photometry.snr)
                .join(Photometry, Photometry.image_id == ImageRow.id)
                .join(PhotometricFilter, PhotometricFilter.id == ImageRow.filter_id)
                .where(Photometry.star_id == int(star_id), PhotometricFilter.name == str(pfilter))
                .order_by(ImageRow.unix_time.asc())
            ).all()
            return DBStar.make_star(int(star_id), pfilter, rows, dtype=self.dtype)

    # ------------- differential curves -------------
    def _add_cmp_star(self, star_id, pfilter, cstar_id, cweight, cstdev):
        if int(star_id) == int(cstar_id):
            raise ValueError(f"star with ID = {star_id} cannot use itself as comparison")
        self._ensure_filter(pfilter)
        fid = self._get_filter_id(pfilter)
        with self.session() as s:
            row = s.execute(select(CmpStar).where(
                CmpStar.star_id==int(star_id),
                CmpStar.filter_id==int(fid),
                CmpStar.cstar_id==int(cstar_id)
            )).scalar_one_or_none()
            if row:
                row.stdev=float(cstdev); row.weight=float(cweight)
            else:
                s.add(CmpStar(star_id=int(star_id), filter_id=int(fid), cstar_id=int(cstar_id),
                              stdev=float(cstdev), weight=float(cweight)))
            s.commit()

    def add_light_curve(self, star_id: int, curve: LightCurve):
        self._ensure_filter(curve.pfilter)
        for cstar_id, w, sd in zip(curve.cstars, curve.cweights, curve.cstdevs):
            self._add_cmp_star(star_id, curve.pfilter, int(cstar_id), float(w), float(sd))
        with self.session() as s:
            fid = self._get_filter_id(curve.pfilter)
            for t, m, snr in curve.points:
                # may skip points missing the image row
                row = s.execute(select(ImageRow.id).where(ImageRow.filter_id==fid, ImageRow.unix_time==float(t))).first()
                if not row:
                    continue
                image_id = int(row[0])
                lc = s.execute(select(LightCurveRow).where(
                    LightCurveRow.star_id==int(star_id), LightCurveRow.image_id==image_id
                )).scalar_one_or_none()
                if lc:
                    lc.magnitude=float(m); lc.snr=None if snr is None else float(snr)
                else:
                    s.add(LightCurveRow(star_id=int(star_id), image_id=image_id, magnitude=float(m),
                                        snr=None if snr is None else float(snr)))
            s.commit()

    def get_light_curve(self, star_id: int, pfilter) -> Optional[LightCurve]:
        with self.session() as s:
            fid = self._get_filter_id(pfilter)
            pts = s.execute(
                select(ImageRow.unix_time, LightCurveRow.magnitude, LightCurveRow.snr)
                .join(LightCurveRow, LightCurveRow.image_id == ImageRow.id)
                .where(LightCurveRow.star_id == int(star_id), ImageRow.filter_id == fid)
                .order_by(ImageRow.unix_time.asc())
            ).all()
            if not pts: return None
            rows = s.execute(
                select(CmpStar.cstar_id, CmpStar.weight, CmpStar.stdev)
                .where(CmpStar.star_id == int(star_id), CmpStar.filter_id == fid)
                .order_by(CmpStar.cstar_id.asc())
            ).all()
            if rows:
                cstars, cweights, cstdevs = zip(*rows)
            else:
                cstars, cweights, cstdevs = ([], [], [])
            lc = LightCurve(pfilter, cstars, cweights, cstdevs, dtype=self.dtype)
            for t, m, snr in pts:
                lc.add(float(t), float(m), None if snr is None else float(snr))
            return lc

    # ------------- photometric parameters & candidates -------------
    def get_or_create_pparams(self, aperture: int, annulus: int, dannulus: int) -> int:
        with self.session() as s:
            row = s.execute(
                select(PhotometricParametersRow).where(
                    PhotometricParametersRow.aperture==int(aperture),
                    PhotometricParametersRow.annulus==int(annulus),
                    PhotometricParametersRow.dannulus==int(dannulus),
                )
            ).scalar_one_or_none()
            if row is None:
                row = PhotometricParametersRow(aperture=int(aperture), annulus=int(annulus), dannulus=int(dannulus))
                s.add(row); s.commit(); s.refresh(row)
            return int(row.id)

    def add_candidate_parameters(self, pfilter, pparams_id: int, stdev: float):
        self._ensure_filter(pfilter)
        fid = self._get_filter_id(pfilter)
        with self.session() as s:
            row = s.execute(select(CandidateParameters).where(
                CandidateParameters.pparams_id==int(pparams_id),
                CandidateParameters.filter_id==int(fid)
            )).scalar_one_or_none()
            if row:
                row.stdev=float(stdev)
            else:
                s.add(CandidateParameters(pparams_id=int(pparams_id), filter_id=int(fid), stdev=float(stdev)))
            s.commit()

    def add_candidate_parameters_by_values(self, pfilter, aperture: int, annulus: int, dannulus: int, stdev: float) -> int:
        pid = self.get_or_create_pparams(aperture, annulus, dannulus)
        self.add_candidate_parameters(pfilter, pid, stdev)
        return pid

    def candidate_parameters_for_filter(self, pfilter) -> List[Tuple[int, int, int, float]]:
        fid = self._get_filter_id(pfilter)
        with self.session() as s:
            rows = s.execute(
                select(PhotometricParametersRow.aperture,
                       PhotometricParametersRow.annulus,
                       PhotometricParametersRow.dannulus,
                       CandidateParameters.stdev)
                .join(CandidateParameters, CandidateParameters.pparams_id == PhotometricParametersRow.id)
                .where(CandidateParameters.filter_id == fid)
                .order_by(CandidateParameters.stdev.asc())
            ).all()
            return [(int(a), int(b), int(c), float(st)) for a, b, c, st in rows]

    # ------------- pm corrections & raw FITS -------------
    def set_pm_correction(self, star_id: int, image_id: int, x: float, y: float):
        with self.session() as s:
            row = s.execute(select(PMCorrection).where(
                PMCorrection.star_id==int(star_id), PMCorrection.image_id==int(image_id)
            )).scalar_one_or_none()
            if row:
                row.x=float(x); row.y=float(y)
            else:
                s.add(PMCorrection(star_id=int(star_id), image_id=int(image_id), x=float(x), y=float(y)))
            s.commit()

    def get_pm_correction(self, star_id: int, image_id: int) -> Optional[Tuple[float, float]]:
        with self.session() as s:
            row = s.execute(select(PMCorrection.x, PMCorrection.y).where(
                PMCorrection.star_id==int(star_id), PMCorrection.image_id==int(image_id)
            )).first()
            return None if row is None else (float(row[0]), float(row[1]))

    def set_raw_image(self, image_id: int, fits_blob: bytes):
        with self.session() as s:
            row = s.get(RawImage, int(image_id))
            if row is None:
                s.add(RawImage(id=int(image_id), fits=fits_blob))
            else:
                row.fits = fits_blob
            s.commit()

    def get_raw_image(self, image_id: int) -> Optional[bytes]:
        with self.session() as s:
            row = s.get(RawImage, int(image_id))
            return None if row is None else row.fits

    # ------------- other helpers -------------
    def airmasses(self, pfilter) -> Dict[float, Optional[float]]:
        with self.session() as s:
            rows = s.execute(
                select(ImageRow.unix_time, ImageRow.airmass)
                .join(PhotometricFilter, PhotometricFilter.id == ImageRow.filter_id)
                .where(PhotometricFilter.name == str(pfilter))
            ).all()
            return {float(t): (None if a is None else float(a)) for t, a in rows}

    def analyze(self):
        with self.engine.begin() as conn:
            conn.exec_driver_sql("ANALYZE")

    def __len__(self): return self.nstars()
