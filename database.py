# database.py
# -*- coding: utf-8 -*-
from __future__ import annotations

import contextlib
import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence, Tuple

from sqlalchemy import (
    BLOB,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    create_engine,
    event,
    insert,
    select,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
)

# -----------------------------------------------------------------------------
# Value objects used by the rest of the code
# -----------------------------------------------------------------------------

@dataclass
class PhotometricParameters:
    aperture: float
    annulus: float
    dannulus: float


@dataclass
class Image:
    path: str
    pfilter: str | None
    unix_time: float | None
    object: str | None
    airmass: float | None
    gain: float | None
    ra: float
    dec: float


# -----------------------------------------------------------------------------
# SQLAlchemy ORM
# -----------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata_obj = MetaData(naming_convention=NAMING_CONVENTION)


class PhotometricFilter(Base):
    __tablename__ = "photometric_filters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)

    images: Mapped[list["DBImage"]] = relationship(back_populates="filter")


class DBImage(Base):
    __tablename__ = "images"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(String, nullable=False)
    filter_id: Mapped[Optional[int]] = mapped_column(ForeignKey("photometric_filters.id"))
    unix_time: Mapped[Optional[float]] = mapped_column(Float)
    object: Mapped[Optional[str]] = mapped_column(String)
    airmass: Mapped[Optional[float]] = mapped_column(Float)
    gain: Mapped[Optional[float]] = mapped_column(Float)
    ra: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    dec: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sources: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    filter: Mapped[Optional[PhotometricFilter]] = relationship(back_populates="images")

    __table_args__ = (
        UniqueConstraint("filter_id", "unix_time"),
        Index("img_by_filter_time", "filter_id", "unix_time"),
    )


class Star(Base):
    __tablename__ = "stars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)
    ra: Mapped[float] = mapped_column(Float, nullable=False)
    dec: Mapped[float] = mapped_column(Float, nullable=False)
    epoch: Mapped[float] = mapped_column(Float, nullable=False)
    pm_ra: Mapped[Optional[float]] = mapped_column(Float)
    pm_dec: Mapped[Optional[float]] = mapped_column(Float)
    imag: Mapped[float] = mapped_column(Float, nullable=False)


class Photometry(Base):
    __tablename__ = "photometry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"), nullable=False)
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    snr: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
        Index("phot_by_star_image", "star_id", "image_id"),
        Index("phot_by_image", "image_id"),
    )


class LightCurve(Base):
    __tablename__ = "light_curves"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"), nullable=False)
    magnitude: Mapped[float] = mapped_column(Float, nullable=False)
    snr: Mapped[Optional[float]] = mapped_column(Float)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
        Index("curve_by_star_image", "star_id", "image_id"),
    )


class PhotometricParametersRow(Base):
    __tablename__ = "photometric_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # legacy schema uses INTEGER ? keep that to stay compatible
    aperture: Mapped[int] = mapped_column(Integer, nullable=False)
    annulus: Mapped[int] = mapped_column(Integer, nullable=False)
    dannulus: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("phot_params_all_rows", "aperture", "annulus", "dannulus"),
    )


class CandidateParameters(Base):
    __tablename__ = "candidate_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pparams_id: Mapped[int] = mapped_column(ForeignKey("photometric_parameters.id"), nullable=False)
    filter_id: Mapped[int] = mapped_column(ForeignKey("photometric_filters.id"), nullable=False)
    stdev: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("pparams_id", "filter_id"),
        Index("cand_filter", "filter_id"),
    )


class PMCorrection(Base):
    __tablename__ = "pm_corrections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    star_id: Mapped[int] = mapped_column(ForeignKey("stars.id"), nullable=False)
    image_id: Mapped[int] = mapped_column(ForeignKey("images.id"), nullable=False)
    x: Mapped[float] = mapped_column(Float, nullable=False)
    y: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("star_id", "image_id"),
    )


class RawImage(Base):
    __tablename__ = "raw_images"

    id: Mapped[int] = mapped_column(ForeignKey("images.id"), primary_key=True)
    fits: Mapped[bytes] = mapped_column(BLOB, nullable=False)


class Metadata(Base):
    __tablename__ = "metadata"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[Optional[bytes]] = mapped_column(BLOB)  # store JSON-encoded bytes


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------

def _json_to_bytes(v: Any) -> bytes:
    return json.dumps(v).encode("utf-8")


def _bytes_to_json(b: Optional[bytes]) -> Any:
    if b is None:
        return None
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return None


# -----------------------------------------------------------------------------
# Public DB facade (ONLY class your code should import/use)
# -----------------------------------------------------------------------------

class LEMONdB:
    """
    SQLAlchemy-backed facade with the legacy method names your code expects.
    Use as a context manager: `with LEMONdB(path) as db: ...`
    """

    def __init__(self, path: str):
        # Path is always SQLite file for LEMON
        uri = f"sqlite:///{os.path.abspath(path)}"
        # check_same_thread=False to play nice with occasional threaded use
        self.engine = create_engine(
            uri,
            future=True,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self._session = Session(self.engine, future=True)
        self._lock = threading.RLock()

    # ----- Context manager -----
    def __enter__(self) -> "LEMONdB":
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()

    # ----- Legacy no-op-safe commit() -----
    def commit(self) -> None:
        with self._lock:
            self._session.commit()

    # ----- Metadata helpers -----
    def _meta_get(self, key: str) -> Any:
        with self._lock:
            row = self._session.get(Metadata, key)
            return _bytes_to_json(row.value) if row else None

    def _meta_set(self, key: str, value: Any) -> None:
        with self._lock:
            b = _json_to_bytes(value)
            existing = self._session.get(Metadata, key)
            if existing:
                existing.value = b
            else:
                self._session.add(Metadata(key=key, value=b))
            self._session.commit()

    # Exposed as properties (compat with your code)
    @property
    def date(self) -> Any:
        return self._meta_get("date")

    @date.setter
    def date(self, v: Any) -> None:
        self._meta_set("date", v)

    @property
    def author(self) -> Any:
        return self._meta_get("author")

    @author.setter
    def author(self, v: Any) -> None:
        self._meta_set("author", v)

    @property
    def hostname(self) -> Any:
        return self._meta_get("hostname")

    @hostname.setter
    def hostname(self, v: Any) -> None:
        self._meta_set("hostname", v)

    @property
    def id(self) -> Any:
        return self._meta_get("id")

    @id.setter
    def id(self, v: Any) -> None:
        self._meta_set("id", v)

    # ----- Filters -----
    def _get_or_create_filter_id(self, name: Optional[str]) -> Optional[int]:
        if name is None:
            return None
        with self._lock:
            # Fast path: lookup
            q = select(PhotometricFilter.id).where(PhotometricFilter.name == name)
            rid = self._session.execute(q).scalar_one_or_none()
            if rid is not None:
                return int(rid)
            # Create
            pf = PhotometricFilter(name=name)
            self._session.add(pf)
            self._session.commit()
            return int(pf.id)

    # ----- Stars -----
    def add_star(
        self,
        id_: int,
        x: float,
        y: float,
        ra0: float,
        dec0: float,
        epoch: float,
        pm_ra: Optional[float],
        pm_dec: Optional[float],
        imag: float,
    ) -> None:
        with self._lock:
            # Upsert-by-id: if exists, update; else insert
            row = self._session.get(Star, id_)
            if row:
                row.x = float(x)
                row.y = float(y)
                row.ra = float(ra0)
                row.dec = float(dec0)
                row.epoch = float(epoch)
                row.pm_ra = None if pm_ra is None else float(pm_ra)
                row.pm_dec = None if pm_dec is None else float(pm_dec)
                row.imag = float(imag)
            else:
                self._session.add(
                    Star(
                        id=int(id_),
                        x=float(x),
                        y=float(y),
                        ra=float(ra0),
                        dec=float(dec0),
                        epoch=float(epoch),
                        pm_ra=None if pm_ra is None else float(pm_ra),
                        pm_dec=None if pm_dec is None else float(pm_dec),
                        imag=float(imag),
                    )
                )

    # ----- Images -----
    def add_image(self, img: Image) -> None:
        """Insert or update the image row (upsert on (filter_id, unix_time))."""
        f_id = self._get_or_create_filter_id(img.pfilter)
        with self._lock:
            q = select(DBImage).where(
                DBImage.filter_id == f_id,
                DBImage.unix_time == img.unix_time,
            )
            row = self._session.execute(q).scalar_one_or_none()
            if row is None:
                row = DBImage(
                    path=str(img.path),
                    filter_id=f_id,
                    unix_time=None if img.unix_time is None else float(img.unix_time),
                    object=None if img.object is None else str(img.object),
                    airmass=None if img.airmass is None else float(img.airmass),
                    gain=None if img.gain is None else float(img.gain),
                    ra=float(img.ra),
                    dec=float(img.dec),
                    sources=0,
                )
                self._session.add(row)
            else:
                row.path = str(img.path)
                row.object = None if img.object is None else str(img.object)
                row.airmass = None if img.airmass is None else float(img.airmass)
                row.gain = None if img.gain is None else float(img.gain)
                row.ra = float(img.ra)
                row.dec = float(img.dec)
            # Do not commit here; let caller batch/commit or use fast_transaction()

    def get_or_add_image_id(self, img: Image) -> int:
        """Return image.id for (filter, unix_time), inserting if needed."""
        f_id = self._get_or_create_filter_id(img.pfilter)
        with self._lock:
            q = select(DBImage.id).where(
                DBImage.filter_id == f_id,
                DBImage.unix_time == img.unix_time,
            )
            rid = self._session.execute(q).scalar_one_or_none()
            if rid is not None:
                return int(rid)

            # Insert now (in case add_image() was not called)
            row = DBImage(
                path=str(img.path),
                filter_id=f_id,
                unix_time=None if img.unix_time is None else float(img.unix_time),
                object=None if img.object is None else str(img.object),
                airmass=None if img.airmass is None else float(img.airmass),
                gain=None if img.gain is None else float(img.gain),
                ra=float(img.ra),
                dec=float(img.dec),
                sources=0,
            )
            self._session.add(row)
            self._session.flush()  # allocate PK without full commit
            return int(row.id)

    # ----- Photometry -----
    def add_photometry_bulk(self, rows: Sequence[Tuple[int, int, float, float]]) -> None:
        """
        Efficient bulk insert into photometry.
        rows: iterable of (star_id, image_id, magnitude, snr)
        """
        if not rows:
            return
        with self._lock:
            payload = [
                dict(
                    star_id=int(s),
                    image_id=int(i),
                    magnitude=float(m),
                    snr=float(snr),
                )
                for (s, i, m, snr) in rows
            ]
            self._session.execute(insert(Photometry), payload)

    def add_photometry(self, star_id: int, unix_time: float, pfilter: str,
                       magnitude: float, snr: float) -> None:
        """
        Legacy slow-path used only if bulk isn?t available by the caller.
        We create/find the image row by (filter, unix_time) with blank path.
        """
        f_id = self._get_or_create_filter_id(pfilter)
        with self._lock:
            # ensure image row exists
            q = select(DBImage).where(DBImage.filter_id == f_id, DBImage.unix_time == float(unix_time))
            img_row = self._session.execute(q).scalar_one_or_none()
            if img_row is None:
                img_row = DBImage(
                    path="",
                    filter_id=f_id,
                    unix_time=float(unix_time),
                    object=None, airmass=None, gain=None,
                    ra=0.0, dec=0.0,
                    sources=0,
                )
                self._session.add(img_row)
                self._session.flush()
            # insert photometry
            self._session.add(
                Photometry(
                    star_id=int(star_id),
                    image_id=int(img_row.id),
                    magnitude=float(magnitude),
                    snr=float(snr),
                )
            )

    # ----- Photometric Parameters -----
    def get_or_create_pparams(self, aperture: float, annulus: float, dannulus: float) -> int:
        a = int(round(aperture))
        b = int(round(annulus))
        c = int(round(dannulus))
        with self._lock:
            q = select(PhotometricParametersRow.id).where(
                PhotometricParametersRow.aperture == a,
                PhotometricParametersRow.annulus == b,
                PhotometricParametersRow.dannulus == c,
            )
            rid = self._session.execute(q).scalar_one_or_none()
            if rid is not None:
                return int(rid)
            row = PhotometricParametersRow(aperture=a, annulus=b, dannulus=c)
            self._session.add(row)
            self._session.flush()
            return int(row.id)

    def add_candidate_pparams(self, cand: Any, pfilter: str) -> None:
        """
        Legacy helper: cand has attributes .aperture, .annulus, .dannulus, .stdev
        """
        ap = float(getattr(cand, "aperture"))
        an = float(getattr(cand, "annulus"))
        dn = float(getattr(cand, "dannulus"))
        st = float(getattr(cand, "stdev", 0.0))

        pid = self.get_or_create_pparams(ap, an, dn)
        fid = self._get_or_create_filter_id(pfilter)
        if fid is None:
            return
        with self._lock:
            # Unique(pparams_id, filter_id) ? ignore if exists, else insert
            q = select(CandidateParameters.id).where(
                CandidateParameters.pparams_id == pid,
                CandidateParameters.filter_id == fid,
            )
            rid = self._session.execute(q).scalar_one_or_none()
            if rid is not None:
                return
            self._session.add(CandidateParameters(pparams_id=pid, filter_id=fid, stdev=float(st)))

    # ----- Light curves (diffphot can use this) -----
    def add_light_curves_bulk(self, rows: Sequence[Tuple[int, int, float, Optional[float]]]) -> None:
        """
        rows: (star_id, image_id, magnitude, snr_or_None)
        """
        if not rows:
            return
        with self._lock:
            payload = [
                dict(
                    star_id=int(s),
                    image_id=int(i),
                    magnitude=float(m),
                    snr=None if snr is None else float(snr),
                )
                for (s, i, m, snr) in rows
            ]
            self._session.execute(insert(LightCurve), payload)

    # ----- ANALYZE -----
    def analyze(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(text("ANALYZE"))

    # ----- Bulk-transaction helper -----
    @contextlib.contextmanager
    def fast_transaction(self) -> Iterator[None]:
        """
        Context manager to group many writes in a single commit.
        """
        with self._lock:
            try:
                yield
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise

    # ----- Close helpers for compatibility -----
    def close_all(self) -> None:
        with self._lock:
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()
            finally:
                self._session.close()

    def close(self) -> None:
        self.close_all()
