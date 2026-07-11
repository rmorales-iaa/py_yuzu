#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

"""
LEMONdB: SQLAlchemy-powered wrapper for LEMON databases.

- Uses SQLite WAL mode for better concurrency.
- All inserts that must be idempotent are done with
  sqlite_insert(...).on_conflict_do_update(...) to avoid syntax issues.
- Provides a 'fast_transaction()' context manager for bulk writes that
  commits once at the end (prevents "database is locked").
- Metadata values (BLOB) are stored as UTF-8 bytes; inputs are coerced
  to str first to avoid 'memoryview requires bytes-like object' errors.
"""

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import os
import threading

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    select,
    text,
    update,
    and_,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    sessionmaker,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert


# -----------------------------------------------------------------------------
# ORM base
# -----------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# -----------------------------------------------------------------------------
# ORM models (match the schema given in the prompt)
# -----------------------------------------------------------------------------

class PhotometricFilter(Base):
    __tablename__ = "photometric_filters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)


class PhotometricParametersRow(Base):
    __tablename__ = "photometric_parameters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # schema says INTEGER, but these are pixel radii; REAL is safer in Python
    aperture: Mapped[float] = mapped_column(Float, nullable=False)
    annulus: Mapped[float] = mapped_column(Float, nullable=False)
    dannulus: Mapped[float] = mapped_column(Float, nullable=False)
    # The original schema had only an index. We keep behavior-compatible lookups.


class ImageRow(Base):
    __tablename__ = "images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    filter_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("photometric_filters.id"), nullable=True
    )
    unix_time: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    object: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    airmass: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    gain: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ra: Mapped[float] = mapped_column(Float, nullable=False)
    dec: Mapped[float] = mapped_column(Float, nullable=False)
    sources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        UniqueConstraint("filter_id", "unix_time", name="uq_images_filter_time"),
    )


class StarRow(Base):
    __tablename__ = "stars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    ra: Mapped[float] = mapped_column(Float, nullable=False)
    dec: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    pm_ra: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    pm_dec: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    imag: Mapped[float] = mapped_column(Float, nullable=False)


class PhotometryRow(Base):
    __tablename__ = "photometry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    snr: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id", name="uq_photometry_star_image"),
    )


class LightCurveRow(Base):
    __tablename__ = "light_curves"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(Integer, ForeignKey("images.id"), nullable=False)
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    snr: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id", name="uq_light_curves_star_image"),
    )


class CandidateParametersRow(Base):
    __tablename__ = "candidate_parameters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pparams_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photometric_parameters.id"), nullable=False
    )
    filter_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("photometric_filters.id"), nullable=False
    )
    stdev: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("pparams_id", "filter_id", name="uq_candidate_params"),
    )


class CMPStarRow(Base):
    __tablename__ = "cmp_stars"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(Integer, ForeignKey("stars.id"), nullable=False)
    filter_id: Mapped[int] = mapped_column(Integer, ForeignKey("photometric_filters.id"), nullable=False)
    cstar_id: Mapped[int] = mapped_column(Integer, ForeignKey("stars.id"), nullable=False)
    stdev: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, nullable=False)


class PMCorrectionRow(Base):
    __tablename__ = "pm_corrections"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(Integer, ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(Integer, ForeignKey("images.id"), nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    __table_args__ = (
        UniqueConstraint("star_id", "image_id", name="uq_pm_corrections"),
    )


class RawImageRow(Base):
    __tablename__ = "raw_images"
    id: Mapped[int] = mapped_column(Integer, ForeignKey("images.id"), primary_key=True)
    fits: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class MetadataRow(Base):
    __tablename__ = "metadata"
    # schema: key TEXT NOT NULL, value BLOB, UNIQUE(key)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    value: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)


# -----------------------------------------------------------------------------
# Simple public data structures (used by photometry.py)
# -----------------------------------------------------------------------------

@dataclass
class PhotometricParameters:
    aperture: float
    annulus: float
    dannulus: float


@dataclass
class Image:
    path: str
    pfilter: str | Any  # we str() it safely
    unix_time: Optional[float]
    object: Optional[str]
    airmass: Optional[float]
    gain: Optional[float]
    ra: float
    dec: float
    sources: int = 0  # not all callers set this; default to zero

# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------

def _to_bytes(val: Any) -> bytes:
    """Serialize arbitrary metadata values to bytes for the BLOB column."""
    if val is None:
        return b""
    if isinstance(val, bytes):
        return val
    return str(val).encode("utf-8")


def _str_name(x: Any) -> str:
    """Ensure filter name is a plain string (avoid Passband objects binding)."""
    try:
        return str(x)
    except Exception:
        return f"{x}"


# -----------------------------------------------------------------------------
# Main DB wrapper
# -----------------------------------------------------------------------------

class LEMONdB:
    """Context-managed LEMON database powered by SQLAlchemy."""

    def __init__(self, path: str, echo: bool = False, timeout: float = 60.0) -> None:
        self.path = os.path.abspath(path)
        self._engine: Engine = create_engine(
            f"sqlite+pysqlite:///{self.path}",
            future=True,
            echo=echo,
            connect_args={"timeout": timeout, "check_same_thread": False},
        )

        # Enable WAL, FK constraints, etc., on every new DB-API connection.
        @event.listens_for(self._engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, _conn_record) -> None:  # pragma: no cover
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
            finally:
                cur.close()

        Base.metadata.create_all(self._engine)

        self._Session = sessionmaker(bind=self._engine, future=True, expire_on_commit=False)

        # Cached table refs for bulk dialect inserts
        self._t_filters = PhotometricFilter.__table__
        self._t_pparams = PhotometricParametersRow.__table__
        self._t_images = ImageRow.__table__
        self._t_stars = StarRow.__table__
        self._t_photometry = PhotometryRow.__table__
        self._t_lcurves = LightCurveRow.__table__
        self._t_cand = CandidateParametersRow.__table__
        self._t_cmp = CMPStarRow.__table__
        self._t_pm = PMCorrectionRow.__table__
        self._t_meta = MetadataRow.__table__

        # Transaction session (used by fast_transaction)
        self._tx_session: Optional[Session] = None
        self._tx_lock = threading.RLock()

    # -- Context management ----------------------------------------------------

    def __enter__(self) -> "LEMONdB":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # nothing persistent to close; sessions are scoped per operation
        pass

    # -- Session handling ------------------------------------------------------

    def _session(self) -> Tuple[Session, bool]:
        """Return (session, owns). If a transaction session is active, reuse it."""
        if self._tx_session is not None:
            return self._tx_session, False
        return self._Session(), True

    class _Txn:
        def __init__(self, outer: "LEMONdB") -> None:
            self.outer = outer

        def __enter__(self) -> "LEMONdB":
            with self.outer._tx_lock:
                if self.outer._tx_session is not None:
                    return self.outer
                self.outer._tx_session = self.outer._Session()
                return self.outer

        def __exit__(self, exc_type, exc, tb) -> None:
            with self.outer._tx_lock:
                if self.outer._tx_session is None:
                    return
                try:
                    if exc_type is None:
                        self.outer._tx_session.commit()
                    else:
                        self.outer._tx_session.rollback()
                finally:
                    self.outer._tx_session.close()
                    self.outer._tx_session = None

    def fast_transaction(self) -> "LEMONdB._Txn":
        """Group many writes into a single commit."""
        return LEMONdB._Txn(self)

    # -- Metadata convenience properties --------------------------------------

    def _meta_set(self, key: str, value: Any) -> None:
        s, owns = self._session()
        try:
            stmt = sqlite_insert(self._t_meta).values({"key": key, "value": _to_bytes(value)})
            stmt = stmt.on_conflict_do_update(
                index_elements=[self._t_meta.c.key],
                set_={"value": _to_bytes(value)},
            )
            s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    def _meta_get(self, key: str) -> Optional[bytes]:
        s, owns = self._session()
        try:
            res = s.execute(select(MetadataRow.value).where(MetadataRow.key == key)).scalar_one_or_none()
            return res
        finally:
            if owns:
                s.close()

    @property
    def date(self) -> Optional[str]:
        v = self._meta_get("date")
        return v.decode("utf-8") if v else None

    @date.setter
    def date(self, v: Any) -> None:
        self._meta_set("date", v)

    @property
    def author(self) -> Optional[str]:
        v = self._meta_get("author")
        return v.decode("utf-8") if v else None

    @author.setter
    def author(self, v: Any) -> None:
        self._meta_set("author", v)

    @property
    def hostname(self) -> Optional[str]:
        v = self._meta_get("hostname")
        return v.decode("utf-8") if v else None

    @hostname.setter
    def hostname(self, v: Any) -> None:
        self._meta_set("hostname", v)

    @property
    def id(self) -> Optional[str]:
        v = self._meta_get("id")
        return v.decode("utf-8") if v else None

    @id.setter
    def id(self, v: Any) -> None:
        self._meta_set("id", v)

    # sources image: store its ImageRow id in metadata key 'simage'
    @property
    def simage(self) -> Optional[int]:
        v = self._meta_get("simage")
        if not v:
            return None
        try:
            return int(v.decode("utf-8"))
        except Exception:
            return None

    @simage.setter
    def simage(self, img: Image) -> None:
        iid = self.get_or_add_image_id(img)
        self._meta_set("simage", str(iid))

    # -- Basic maintenance -----------------------------------------------------

    def analyze(self) -> None:
        """Run ANALYZE to refresh stats (useful after heavy writes)."""
        s, owns = self._session()
        try:
            s.execute(text("ANALYZE"))
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    # -- ID helpers ------------------------------------------------------------

    def _get_or_create_filter_id(self, name: Any) -> int:
        """Return id of the photometric filter with this name (create if needed)."""
        name_str = _str_name(name)
        s, owns = self._session()
        try:
            rid = s.execute(select(PhotometricFilter.id).where(PhotometricFilter.name == name_str)).scalar_one_or_none()
            if rid is not None:
                return int(rid)

            stmt = sqlite_insert(self._t_filters).values({"name": name_str})
            stmt = stmt.on_conflict_do_nothing(index_elements=[self._t_filters.c.name])
            s.execute(stmt)
            rid = s.execute(select(PhotometricFilter.id).where(PhotometricFilter.name == name_str)).scalar_one()
            if owns:
                s.commit()
            return int(rid)
        finally:
            if owns:
                s.close()

    def _get_or_create_pparams_id(self, aperture: float, annulus: float, dannulus: float) -> int:
        """Return id of the (aperture,annulus,dannulus) row; create if missing."""
        s, owns = self._session()
        try:
            rid = s.execute(
                select(PhotometricParametersRow.id).where(
                    and_(
                        PhotometricParametersRow.aperture == float(aperture),
                        PhotometricParametersRow.annulus == float(annulus),
                        PhotometricParametersRow.dannulus == float(dannulus),
                    )
                )
            ).scalar_one_or_none()
            if rid is not None:
                return int(rid)

            s.execute(
                sqlite_insert(self._t_pparams).values(
                    {
                        "aperture": float(aperture),
                        "annulus": float(annulus),
                        "dannulus": float(dannulus),
                    }
                )
            )
            rid = s.execute(
                select(PhotometricParametersRow.id).where(
                    and_(
                        PhotometricParametersRow.aperture == float(aperture),
                        PhotometricParametersRow.annulus == float(annulus),
                        PhotometricParametersRow.dannulus == float(dannulus),
                    )
                )
            ).scalar_one()
            if owns:
                s.commit()
            return int(rid)
        finally:
            if owns:
                s.close()

    # -- Public insert/upsert operations --------------------------------------

    def add_star(
        self,
        id_: int,
        x: float,
        y: float,
        ra: float,
        dec: float,
        epoch: float,
        pm_ra: Optional[float],
        pm_dec: Optional[float],
        imag: float,
    ) -> None:
        """Upsert a star by primary key id."""
        s, owns = self._session()
        try:
            vals = {
                "id": int(id_),
                "x": float(x),
                "y": float(y),
                "ra": float(ra),
                "dec": float(dec),
                "epoch": float(epoch),
                "pm_ra": float(pm_ra) if pm_ra is not None else None,
                "pm_dec": float(pm_dec) if pm_dec is not None else None,
                "imag": float(imag),
            }
            stmt = sqlite_insert(self._t_stars).values(vals)
            stmt = stmt.on_conflict_do_update(
                index_elements=[self._t_stars.c.id],
                set_={
                    "x": stmt.excluded.x,
                    "y": stmt.excluded.y,
                    "ra": stmt.excluded.ra,
                    "dec": stmt.excluded.dec,
                    "epoch": stmt.excluded.epoch,
                    "pm_ra": stmt.excluded.pm_ra,
                    "pm_dec": stmt.excluded.pm_dec,
                    "imag": stmt.excluded.imag,
                },
            )
            s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    def add_candidate_pparams(self, cand: Any, pf_name: Any) -> None:
        """
        Upsert candidate photometric parameters (unique by (pparams_id, filter_id)).
        'cand' must have: aperture, annulus, dannulus, stdev
        """
        aperture = float(getattr(cand, "aperture"))
        annulus = float(getattr(cand, "annulus"))
        dannulus = float(getattr(cand, "dannulus"))
        stdev = float(getattr(cand, "stdev"))

        pf_id = self._get_or_create_filter_id(pf_name)
        pid = self._get_or_create_pparams_id(aperture, annulus, dannulus)

        s, owns = self._session()
        try:
            stmt = sqlite_insert(self._t_cand).values(
                {"pparams_id": pid, "filter_id": pf_id, "stdev": stdev}
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[self._t_cand.c.pparams_id, self._t_cand.c.filter_id],
                set_={"stdev": stmt.excluded.stdev},
            )
            s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    def add_image(self, img: Image) -> int:
        """
        Upsert an image. Prefer conflict target (filter_id, unix_time).
        Fallback to path when unix_time is NULL.
        Returns image id.
        """
        pf_id = self._get_or_create_filter_id(img.pfilter)
        vals = {
            "path": str(img.path),
            "filter_id": pf_id,
            "unix_time": float(img.unix_time) if img.unix_time is not None else None,
            "object": str(img.object) if img.object is not None else None,
            "airmass": float(img.airmass) if img.airmass is not None else None,
            "gain": float(img.gain) if img.gain is not None else None,
            "ra": float(img.ra),
            "dec": float(img.dec),
            "sources": int(getattr(img, "sources", 0) or 0),
        }

        s, owns = self._session()
        try:
            if vals["unix_time"] is not None:
                stmt = sqlite_insert(self._t_images).values(vals)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[self._t_images.c.filter_id, self._t_images.c.unix_time],
                    set_={
                        "path": stmt.excluded.path,
                        "object": stmt.excluded.object,
                        "airmass": stmt.excluded.airmass,
                        "gain": stmt.excluded.gain,
                        "ra": stmt.excluded.ra,
                        "dec": stmt.excluded.dec,
                        "sources": stmt.excluded.sources,
                    },
                )
                s.execute(stmt)
                # resolve id via unique key
                rid = s.execute(
                    select(ImageRow.id).where(
                        and_(
                            ImageRow.filter_id == pf_id,
                            ImageRow.unix_time == vals["unix_time"],
                        )
                    )
                ).scalar_one()
            else:
                # Fallback path-based upsert (no UNIQUE constraint on path, so emulate)
                rid = s.execute(
                    select(ImageRow.id)
                    .where(ImageRow.path == vals["path"])
                    .order_by(ImageRow.id.desc())
                    .limit(1)
                ).scalar_one_or_none()
                if rid is None:
                    s.execute(sqlite_insert(self._t_images).values(vals))
                    rid = s.execute(
                        select(ImageRow.id)
                        .where(ImageRow.path == vals["path"])
                        .order_by(ImageRow.id.desc())
                        .limit(1)
                    ).scalar_one()
                else:
                    s.execute(update(self._t_images).where(ImageRow.id == rid).values(vals))
            if owns:
                s.commit()
            return int(rid)
        finally:
            if owns:
                s.close()

    def get_or_add_image_id(self, img: Image) -> int:
        """Return the id of an image, inserting or updating as required."""
        pf_id = self._get_or_create_filter_id(img.pfilter)
        s, owns = self._session()
        try:
            if img.unix_time is not None:
                rid = s.execute(
                    select(ImageRow.id).where(
                        and_(
                            ImageRow.filter_id == pf_id,
                            ImageRow.unix_time == float(img.unix_time),
                        )
                    )
                ).scalar_one_or_none()
                if rid is not None:
                    return int(rid)
            # fallback to path
            rid = s.execute(
                select(ImageRow.id)
                .where(ImageRow.path == str(img.path))
                .order_by(ImageRow.id.desc())
                .limit(1)
            ).scalar_one_or_none()
            if rid is not None:
                return int(rid)
        finally:
            if owns:
                s.close()
        # not found ? insert
        return self.add_image(img)

    def add_photometry(self, star_id: int, image_id: int, magnitude: float, snr: float) -> None:
        """Upsert a single photometry row."""
        s, owns = self._session()
        try:
            stmt = sqlite_insert(self._t_photometry).values(
                {
                    "star_id": int(star_id),
                    "image_id": int(image_id),
                    "magnitude": float(magnitude),
                    "snr": float(snr),
                }
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[self._t_photometry.c.star_id, self._t_photometry.c.image_id],
                set_={
                    "magnitude": stmt.excluded.magnitude,
                    "snr": stmt.excluded.snr,
                },
            )
            s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    def add_photometry_bulk(self, rows: Iterable[Tuple[int, int, float, float]], chunk_size: int = 800) -> None:
        """
        Upsert many photometry rows at once.
        rows = [(star_id, image_id, magnitude, snr), ...]
        """
        rows = list(rows)
        if not rows:
            return

        tbl = self._t_photometry
        s, owns = self._session()
        try:
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                values = [
                    {
                        "star_id": int(star_id),
                        "image_id": int(image_id),
                        "magnitude": float(mag),
                        "snr": float(snr),
                    }
                    for (star_id, image_id, mag, snr) in chunk
                ]
                stmt = sqlite_insert(tbl).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[tbl.c.star_id, tbl.c.image_id],
                    set_={
                        "magnitude": stmt.excluded.magnitude,
                        "snr": stmt.excluded.snr,
                    },
                )
                s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()

    def add_light_curves_bulk(self, rows: Iterable[Tuple[int, int, float, Optional[float]]], chunk_size: int = 800) -> None:
        """
        Upsert many light_curve rows:
        rows = [(star_id, image_id, magnitude, snr_or_None), ...]
        """
        rows = list(rows)
        if not rows:
            return

        tbl = self._t_lcurves
        s, owns = self._session()
        try:
            for i in range(0, len(rows), chunk_size):
                chunk = rows[i : i + chunk_size]
                values = [
                    {
                        "star_id": int(star_id),
                        "image_id": int(image_id),
                        "magnitude": float(mag),
                        "snr": (float(snr) if snr is not None else None),
                    }
                    for (star_id, image_id, mag, snr) in chunk
                ]
                stmt = sqlite_insert(tbl).values(values)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[tbl.c.star_id, tbl.c.image_id],
                    set_={
                        "magnitude": stmt.excluded.magnitude,
                        "snr": stmt.excluded.snr,
                    },
                )
                s.execute(stmt)
            if owns:
                s.commit()
        finally:
            if owns:
                s.close()
