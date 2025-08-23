#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import (
    declarative_base,
    relationship,
    scoped_session,
    sessionmaker,
)

# -----------------------------------------------------------------------------
# ORM base
# -----------------------------------------------------------------------------

Base = declarative_base()

# -----------------------------------------------------------------------------
# ORM models (tables)
# -----------------------------------------------------------------------------

class PhotometricFilterRow(Base):
    __tablename__ = "photometric_filters"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)


class PhotometricParametersRow(Base):
    __tablename__ = "photometric_parameters"
    id = Column(Integer, primary_key=True)
    # Upstream schema says INTEGER; we keep INTEGER and cast from floats on write
    aperture = Column(Integer, nullable=False)
    annulus = Column(Integer, nullable=False)
    dannulus = Column(Integer, nullable=False)

Index("phot_params_all_rows", PhotometricParametersRow.aperture,
      PhotometricParametersRow.annulus, PhotometricParametersRow.dannulus)


class CandidateParametersRow(Base):
    __tablename__ = "candidate_parameters"
    id = Column(Integer, primary_key=True)
    pparams_id = Column(Integer, ForeignKey("photometric_parameters.id"), nullable=False)
    filter_id = Column(Integer, ForeignKey("photometric_filters.id"), nullable=False)
    stdev = Column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("pparams_id", "filter_id"),
    )

Index("cand_filter", CandidateParametersRow.filter_id)


class ImageRow(Base):
    __tablename__ = "images"
    id = Column(Integer, primary_key=True)
    path = Column(String, nullable=False)
    filter_id = Column(Integer, ForeignKey("photometric_filters.id"))
    unix_time = Column(Float)
    object = Column(String)
    airmass = Column(Float)
    gain = Column(Float)
    ra = Column(Float, nullable=False)
    dec = Column(Float, nullable=False)
    sources = Column(Integer, nullable=False, default=0)
    __table_args__ = (
        UniqueConstraint("filter_id", "unix_time"),
    )

Index("img_by_filter_time", ImageRow.filter_id, ImageRow.unix_time)


class PhotometryRow(Base):
    __tablename__ = "photometry"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude = Column(Float, nullable=False)
    snr = Column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
    )

Index("phot_by_star_image", PhotometryRow.star_id, PhotometryRow.image_id)
Index("phot_by_image", PhotometryRow.image_id)


class LightCurve(Base):
    __tablename__ = "light_curves"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude = Column(Float, nullable=False)
    snr = Column(Float)  # nullable in schema
    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
    )

Index("curve_by_star_image", LightCurve.star_id, LightCurve.image_id)


class PmCorrectionRow(Base):
    __tablename__ = "pm_corrections"
    id = Column(Integer, primary_key=True)
    star_id = Column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id = Column(Integer, ForeignKey("images.id"), nullable=False)
    x = Column(Float, nullable=False)
    y = Column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
    )


class RawImageRow(Base):
    __tablename__ = "raw_images"
    id = Column(Integer, ForeignKey("images.id"), primary_key=True)
    fits = Column(LargeBinary, nullable=False)


class StarRow(Base):
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


class Metadata(Base):
    __tablename__ = "metadata"
    # The original schema uses UNIQUE(key); we use key as PK for simplicity.
    key = Column(String, primary_key=True)
    value = Column(LargeBinary)


# -----------------------------------------------------------------------------
# Lightweight DTOs (used by calling code)
# -----------------------------------------------------------------------------

@dataclass
class PhotometricParameters:
    aperture: float
    annulus: float
    dannulus: float


class Image:
    """Lightweight value object the pipeline constructs and passes back here."""
    __slots__ = ("path", "pfilter", "unix_time", "object", "airmass", "gain", "ra", "dec", "sources")

    def __init__(
        self,
        path: str,
        pfilter: Optional[str],
        unix_time: Optional[float],
        object: Optional[str],
        airmass: Optional[float],
        gain: Optional[float],
        ra: float,
        dec: float,
        sources: int = 0,
    ):
        self.path = path
        self.pfilter = pfilter
        self.unix_time = unix_time
        self.object = object
        self.airmass = airmass
        self.gain = gain
        self.ra = float(ra or 0.0)
        self.dec = float(dec or 0.0)
        self.sources = int(sources or 0)


# -----------------------------------------------------------------------------
# SQLite tuning
# -----------------------------------------------------------------------------

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-20000")  # ~20MB
        cursor.close()
    except Exception:
        # non-SQLite or older SQLite ? ignore
        pass


# -----------------------------------------------------------------------------
# Serialization helpers for metadata.value (BLOB)
# -----------------------------------------------------------------------------

def _serialize_meta(value) -> bytes:
    if value is None:
        return b"null"
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    try:
        return json.dumps(value).encode("utf-8")
    except Exception:
        return str(value).encode("utf-8")


def _deserialize_meta(blob):
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray)):
        try:
            return json.loads(blob.decode("utf-8"))
        except Exception:
            try:
                return blob.decode("utf-8")
            except Exception:
                return blob
    return blob


# -----------------------------------------------------------------------------
# Main DB facade (thread-safe SQLAlchemy implementation)
# -----------------------------------------------------------------------------

class LEMONSA:
    """Thread-safe SQLAlchemy implementation of the LEMON DB schema."""

    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        # SQLite file; allow cross-thread usage
        url = f"sqlite:///{self.path}"
        self.engine = create_engine(
            url,
            future=True,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)

        # Sessions: new session per thread, auto-closed in context manager
        self._session_factory = scoped_session(
            sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        )

        # Session used inside fast_transaction()
        self._txn_local = threading.local()

    # -- context manager API --------------------------------------------------

    def __enter__(self) -> "LEMONSA":
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close_all()

    # -- legacy compatibility -------------------------------------------------

    def commit(self):
        """No-op for compatibility with old code that called commit() on the facade."""
        return None

    # -- session helpers ------------------------------------------------------

    @contextmanager
    def session(self):
        """Yield an ORM session.

        If we're inside fast_transaction(), reuse the transactional session
        bound to the current thread; otherwise create/close a new one.
        """
        s: Optional[scoped_session] = getattr(self._txn_local, "session", None)
        if s is not None:
            yield s
            return

        s = self._session_factory()
        try:
            yield s
        finally:
            try:
                s.close()
            except Exception:
                pass

    @contextmanager
    def fast_transaction(self):
        """Single long transaction context (faster bulk inserts)."""
        if getattr(self._txn_local, "session", None) is not None:
            # Nested: just yield
            yield self
            return
        s = self._session_factory()
        self._txn_local.session = s
        trans = s.begin()
        try:
            yield self
            trans.commit()
        except Exception:
            trans.rollback()
            raise
        finally:
            try:
                s.close()
            finally:
                self._txn_local.session = None

    # -- general ops ----------------------------------------------------------

    def analyze(self):
        with self.engine.connect() as conn:
            conn.exec_driver_sql("ANALYZE")

    def close_all(self):
        try:
            self._session_factory.remove()
        finally:
            try:
                self.engine.dispose()
            except Exception:
                pass

    # -- metadata convenience -------------------------------------------------

    def _meta_set(self, key: str, value):
        payload = _serialize_meta(value)
        with self.session() as s:
            row = s.get(Metadata, key)
            if row is None:
                s.add(Metadata(key=key, value=payload))
            else:
                row.value = payload
            s.commit()

    def _meta_get(self, key: str):
        with self.session() as s:
            row = s.get(Metadata, key)
            return _deserialize_meta(row.value) if row else None

    @property
    def date(self) -> Optional[float]:
        v = self._meta_get("date")
        return None if v is None else float(v)

    @date.setter
    def date(self, v):
        self._meta_set("date", float(v) if v is not None else None)

    @property
    def author(self) -> Optional[str]:
        v = self._meta_get("author")
        return None if v is None else str(v)

    @author.setter
    def author(self, v):
        self._meta_set("author", v)

    @property
    def hostname(self) -> Optional[str]:
        v = self._meta_get("hostname")
        return None if v is None else str(v)

    @hostname.setter
    def hostname(self, v):
        self._meta_set("hostname", v)

    @property
    def id(self) -> Optional[str]:
        v = self._meta_get("id")
        return None if v is None else str(v)

    @id.setter
    def id(self, v):
        self._meta_set("id", v)

    # Store a few handy details about the sources image
    @property
    def simage(self) -> Optional[str]:
        return self._meta_get("simage_path")

    @simage.setter
    def simage(self, img: Image):
        # Ensure row exists and persist minimal metadata
        self.add_image(img)
        self._meta_set("simage_path", img.path)
        self._meta_set("simage_filter", img.pfilter)
        self._meta_set("simage_unix_time", img.unix_time)
        self._meta_set("simage_ra", img.ra)
        self._meta_set("simage_dec", img.dec)

    # -- photometric filters --------------------------------------------------

    def get_or_create_filter_id(self, name: Optional[str]) -> Optional[int]:
        if name is None:
            return None
        with self.session() as s:
            row = s.query(PhotometricFilterRow).filter_by(name=str(name)).one_or_none()
            if row is None:
                row = PhotometricFilterRow(name=str(name))
                s.add(row)
                s.commit()
            return int(row.id)

    # -- photometric parameters / candidates ---------------------------------

    def get_or_create_pparams(self, aperture: float, annulus: float, dannulus: float) -> int:
        # Schema uses INTEGER; keep behavior by rounding
        ap_i = int(round(aperture))
        an_i = int(round(annulus))
        dn_i = int(round(dannulus))
        with self.session() as s:
            row = (
                s.query(PhotometricParametersRow)
                .filter_by(aperture=ap_i, annulus=an_i, dannulus=dn_i)
                .one_or_none()
            )
            if row is None:
                row = PhotometricParametersRow(aperture=ap_i, annulus=an_i, dannulus=dn_i)
                s.add(row)
                s.commit()
            return int(row.id)

    def add_candidate_parameters_by_values(
        self,
        pfilter: Optional[str],
        aperture: float,
        annulus: float,
        dannulus: float,
        stdev: float,
    ) -> int:
        pparams_id = self.get_or_create_pparams(aperture, annulus, dannulus)
        filter_id = self.get_or_create_filter_id(pfilter)
        with self.session() as s:
            row = (
                s.query(CandidateParametersRow)
                .filter_by(pparams_id=pparams_id, filter_id=filter_id)
                .one_or_none()
            )
            if row is None:
                row = CandidateParametersRow(pparams_id=pparams_id, filter_id=filter_id, stdev=float(stdev))
                s.add(row)
                s.commit()
            else:
                row.stdev = float(stdev)
                s.commit()
            return int(row.id)

    def add_candidate_pparams(self, cand, pfilter: Optional[str]) -> int:
        """Compatibility method (photometry uses this)."""
        ap = float(getattr(cand, "aperture", None) or cand["aperture"])
        an = float(getattr(cand, "annulus", None) or cand["annulus"])
        dn = float(getattr(cand, "dannulus", None) or cand["dannulus"])
        st = float(getattr(cand, "stdev", 0.0) or cand.get("stdev", 0.0))
        return self.add_candidate_parameters_by_values(pfilter, ap, an, dn, st)

    # -- images ---------------------------------------------------------------

    def add_image(self, img: Image) -> int:
        """Upsert an image row; returns image_id."""
        filter_id = self.get_or_create_filter_id(img.pfilter)
        with self.session() as s:
            # Resolve by (filter_id, unix_time) if available; otherwise by path
            row = None
            if filter_id is not None and img.unix_time is not None:
                row = (
                    s.query(ImageRow)
                    .filter_by(filter_id=filter_id, unix_time=float(img.unix_time))
                    .one_or_none()
                )
            if row is None:
                row = s.query(ImageRow).filter_by(path=str(img.path)).one_or_none()
            if row is None:
                row = ImageRow(
                    path=str(img.path),
                    filter_id=filter_id,
                    unix_time=float(img.unix_time) if img.unix_time is not None else None,
                    object=img.object,
                    airmass=float(img.airmass) if img.airmass is not None else None,
                    gain=float(img.gain) if img.gain is not None else None,
                    ra=float(img.ra or 0.0),
                    dec=float(img.dec or 0.0),
                    sources=int(img.sources or 0),
                )
                s.add(row)
                try:
                    s.commit()
                except IntegrityError:
                    s.rollback()
                    # try to fetch the existing one now that UNIQUE may have collided
                    row = (
                        s.query(ImageRow)
                        .filter_by(filter_id=filter_id, unix_time=float(img.unix_time))
                        .one()
                    )
            else:
                # Update mutable fields
                row.object = img.object
                row.airmass = float(img.airmass) if img.airmass is not None else row.airmass
                row.gain = float(img.gain) if img.gain is not None else row.gain
                row.ra = float(img.ra or row.ra)
                row.dec = float(img.dec or row.dec)
                if img.sources is not None:
                    row.sources = int(img.sources)
                s.commit()
            return int(row.id)

    def get_or_add_image_id(self, img: Image) -> int:
        return self.add_image(img)

    # Fallback used by older code paths
    def add_photometry(
        self,
        star_id: int,
        unix_time: float,
        pfilter: Optional[str],
        magnitude: float,
        snr: float,
        path: str = "",
    ) -> Optional[int]:
        """Insert a single photometry row, resolving/creating image if needed."""
        image = Image(
            path=path or "",
            pfilter=pfilter,
            unix_time=unix_time,
            object=None,
            airmass=None,
            gain=None,
            ra=0.0,
            dec=0.0,
            sources=0,
        )
        image_id = self.get_or_add_image_id(image)
        with self.session() as s:
            try:
                s.add(
                    PhotometryRow(
                        star_id=int(star_id),
                        image_id=int(image_id),
                        magnitude=float(magnitude),
                        snr=float(snr),
                    )
                )
                s.commit()
                return image_id
            except IntegrityError:
                s.rollback()
                return None

    # -- bulk operations (fast) ----------------------------------------------

    def add_photometry_bulk(self, rows: Sequence[Tuple[int, int, float, float]]):
        """rows: (star_id, image_id, magnitude, snr)"""
        if not rows:
            return
        with self.session() as s:
            # Use SQLite executemany with OR IGNORE for speed + idempotency
            s.connection().exec_driver_sql(
                "INSERT OR IGNORE INTO photometry (star_id, image_id, magnitude, snr) VALUES (?,?,?,?)",
                rows,
            )
            s.commit()

    def add_light_curves_bulk(self, rows: Sequence[Tuple[int, int, float, Optional[float]]]):
        """rows: (star_id, image_id, magnitude, snr_or_None)"""
        if not rows:
            return
        with self.session() as s:
            s.connection().exec_driver_sql(
                "INSERT OR REPLACE INTO light_curves (star_id, image_id, magnitude, snr) VALUES (?,?,?,?)",
                rows,
            )
            s.commit()

    def add_light_curve(self, star_id: int, image_id: int, magnitude: float, snr: Optional[float]):
        with self.session() as s:
            s.connection().exec_driver_sql(
                "INSERT OR REPLACE INTO light_curves (star_id, image_id, magnitude, snr) VALUES (?,?,?,?)",
                (int(star_id), int(image_id), float(magnitude), (None if snr is None else float(snr))),
            )
            s.commit()

    # -- stars ----------------------------------------------------------------

    def add_star(
        self,
        star_id: int,
        x: float,
        y: float,
        ra: float,
        dec: float,
        epoch: float,
        pm_ra: Optional[float],
        pm_dec: Optional[float],
        imag: float,
    ):
        with self.session() as s:
            row = s.get(StarRow, int(star_id))
            if row is None:
                row = StarRow(
                    id=int(star_id),
                    x=float(x),
                    y=float(y),
                    ra=float(ra),
                    dec=float(dec),
                    epoch=float(epoch),
                    pm_ra=(None if pm_ra is None else float(pm_ra)),
                    pm_dec=(None if pm_dec is None else float(pm_dec)),
                    imag=float(imag),
                )
                s.add(row)
            else:
                row.x = float(x)
                row.y = float(y)
                row.ra = float(ra)
                row.dec = float(dec)
                row.epoch = float(epoch)
                row.pm_ra = None if pm_ra is None else float(pm_ra)
                row.pm_dec = None if pm_dec is None else float(pm_dec)
                row.imag = float(imag)
            s.commit()

    # -- raw image blobs ------------------------------------------------------

    def add_raw_image(self, image_id: int, fits_bytes: bytes):
        with self.session() as s:
            row = s.get(RawImageRow, int(image_id))
            if row is None:
                row = RawImageRow(id=int(image_id), fits=bytes(fits_bytes))
                s.add(row)
            else:
                row.fits = bytes(fits_bytes)
            s.commit()


# -----------------------------------------------------------------------------
# Backwards-compatible names
# -----------------------------------------------------------------------------

LightCurveItem = LightCurve # some modules import LightCurveItem
