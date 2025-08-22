#!/usr/bin/env python3
from __future__ import annotations, division

"""
This module implements LEMONdB, the interface to the SQLite databases to which
photometric information and light curves are saved. These databases contain all
the information that may be needed for the data analysis.
"""

import collections
import copy
import itertools
import logging
import math
import numbers
import numpy
import operator
import os
import random
import sqlite3
import string
import tempfile

# LEMON modules
import astromatic
import json_parse
import passband
import util

class _ListCallable(list):
    """List that can also be called like a function to return a plain list."""
    def __call__(self):
        return list(self)

class DBStar(object):
    """Encapsulates the instrumental photometric information for a star.

    This class is used as a container for the instrumental photometry of a
    star, observed in a specific photometric filter. It implements both
    high-level and low-level routines, the latter of which are fundamental for
    a scalable implementation of the differential photometry algorithms.
    """

    def __init__(self, id_, pfilter, phot_info, times_indexes, dtype=numpy.longdouble):
        """Instantiation method for the DBStar class.

        This is an abrupt descent in the abstraction ladder, but needed in
        order to compute the light curves fast and minimize the memory usage.

        Arguments:
        id_ - the ID of the star in the LEMONdB.
        pfilter - the photometric filter of the information being stored.
        phot_info - a two-dimensional NumPy array with the photometric
                    information. It *must* have three rows (the first for the
                    time, the second for the magnitude and the last for the
                    SNR) and as many columns as records for which there is
                    photometric information. For example, in order to get the
                    magnitude of the third image, we would do phot_info[1][2]
        times_indexes - a dictionary mapping each Unix time for which the star
                        was observed to its index in phot_info; this gives us
                        O(1) lookups when 'trimming' an instance. See the
                        BDStar.issubset and complete_for for further
                        information. Note that the values in this dictionary
                        are trusted blindly, so they better have the correct
                        values for phot_info!
        """

        self.id = id_
        self.pfilter = pfilter
        if phot_info.shape[0] != 3:  # number of rows
            raise ValueError("'phot_info' must have exactly three rows")
        self._phot_info = phot_info
        self._time_indexes = times_indexes
        self.dtype = dtype

    def __str__(self):
        """The 'informal' string representation."""
        return f"{self.__class__.__name__}(ID = {self.id}, filter = {self.pfilter}, {len(self)} records)"

    def __len__(self):
        """Return the number of records for the star."""
        return self._phot_info.shape[1]  # number of columns

    def time(self, index):
        """Return the Unix time of the index-th record."""
        return self._phot_info[0][index]

    def _time_index(self, unix_time):
        """Return the index of the Unix time in '_phot_info'."""
        return self._time_indexes[unix_time]

    def mag(self, index):
        """Return the magnitude of the index-th record."""
        return self._phot_info[1][index]

    def snr(self, index):
        """Return the SNR of the index-th record."""
        return self._phot_info[2][index]

    @property
    def _unix_times(self):
        """Return the Unix times at which the star was observed."""
        return self._phot_info[0]

    def issubset(self, other):
        """Return True if for each Unix time at which 'self' was observed,
        there is also an observation for 'other'; False otherwise"""
        for unix_time in self._unix_times:
            if unix_time not in other._time_indexes:
                return False
        return True

    def _trim_to(self, other):
        """Return a new DBStar which contains the records of 'self' that were
        observed at the Unix times that can be found in 'other'. KeyError will
        be raised if self if not a subset of other -- so you should check for
        that before trimming anything"""
        phot_info = numpy.empty((3, len(other)), dtype=self.dtype)
        for oindex, unix_time in enumerate(other._unix_times):
            sindex = self._time_index(unix_time)
            phot_info[0][oindex] = self.time(sindex)
            phot_info[1][oindex] = self.mag(sindex)
            phot_info[2][oindex] = self.snr(sindex)
        return DBStar(
            self.id, self.pfilter, phot_info, other._time_indexes, dtype=self.dtype
        )

    def complete_for(self, iterable):
        """Iterate over the supplied DBStars and trim them.

        The method returns a list with the 'trimmed' version of those DBStars
        which are different than 'self' (i.e., a star instance will not be
        considered to be a subset of itself) and of which it it is a subset.
        """
        complete_stars = []
        for star in iterable:
            if self is not star and self.issubset(star):
                complete_stars.append(star._trim_to(self))
        return complete_stars

    @staticmethod
    def make_star(id_, pfilter, rows, dtype=numpy.longdouble):
        """Construct a DBStar instance for some photometric data.

        Feeding the class constructor with NumPy arrays and dictionaries is not
        particularly practical, so most of the time you may want to use instead
        this convenience function. It also receives the star ID and the filter
        of the star, but the photometric records are given as a sequence of
        three-element tuples (Unix time, magnitude and SNR).
        """
        phot_info = numpy.empty((3, len(rows)), dtype=dtype)
        times_indexes = {}
        for index, row in enumerate(rows):
            unix_time, magnitude, snr = row
            phot_info[0][index] = unix_time
            phot_info[1][index] = magnitude
            phot_info[2][index] = snr
            times_indexes[unix_time] = index
        return DBStar(id_, pfilter, phot_info, times_indexes, dtype=dtype)


# The parameters used for aperture photometry
typename = "PhotometricParameters"
field_names = "aperture, annulus, dannulus"
PhotometricParameters = collections.namedtuple(typename, field_names)

# A FITS image
typename = "Image"
field_names = "path pfilter unix_time object airmass gain ra dec"
Image = collections.namedtuple(typename, field_names)


class LightCurve(object):
    """The data points of a graph of light intensity of a celestial object.

    Encapsulates a series of Unix times linked to a differential magnitude with
    a signal-to-noise ratio. Internally stored as a list of three-element
    tuples, but we are implementing the add method so that we can interact with
    it as if it were a set, moving us up one level in the abstraction ladder.
    """

    def __init__(self, pfilter, cstars, cweights, cstdevs, dtype=numpy.longdouble):
        """Initialize a new LightCurve object.

        The 'cstars' argument is a sequence or iterable with the IDs in the
        LEMONdB of the stars that were used as comparison stars when the light
        curve was computed. 'cweights' is another sequence or iterable with the
        corresponding weights, while 'cstdevs' contains the standard deviation
        of their light curves, and from which (it is assumed) the weights were
        calculated. The i-th comparison star (cstars) is assigned the i-th
        weight (cweights) and standard deviation (cstdevs). The sum of all
        weights should equal one.
        """
        if len(cstars) != len(cweights):
            msg = "number of weights must equal that of comparison stars"
            raise ValueError(msg)
        if not cstars:
            msg = "at least one comparison star is needed"
            raise ValueError(msg)

        self._data = []
        self.pfilter = pfilter
        self.cstars = cstars
        self.cweights = cweights
        self.cstdevs = cstdevs
        self.dtype = dtype

    def add(self, unix_time, magnitude, snr):
        """Add a data point to the light curve."""
        self._data.append((unix_time, magnitude, snr))

    def __len__(self):
        return len(self._data)

    def __getitem__(self, index):
        return self._data[index]

    def __iter__(self):
        """Return a copy of the (unix_time, magnitude, snr) tuples, chronologically sorted."""
        return iter(sorted(self._data, key=operator.itemgetter(0)))

    @property
    def stdev(self):
        if not self:
            raise ValueError("light curve is empty")
        magnitudes = [mag for unix_time, mag, snr in self._data]
        return numpy.std(numpy.array(magnitudes, dtype=self.dtype))

    def weights(self):
        """Return a generator over the comparison stars and their weights.

        This method returns a generator of three-element tuples, with (a) the
        comparison star, (b) its weight and (c) the standard deviation of its
        light curve, respectively.
        """
        return zip(self.cstars, self.cweights, self.cstdevs)

    def amplitude(self, npoints=1, median=True):
        """Compute the peak-to-peak amplitude of the light curve."""
        if not self:
            raise ValueError("light curve is empty")

        magnitudes = sorted(mag for unix_time, mag, snr in self._data)
        func = numpy.median if median else numpy.mean
        return func(magnitudes[-npoints:]) - func(magnitudes[:npoints])

    def ignore_noisy(self, snr):
        """Return a copy of the LightCurve without noisy points."""
        curve = copy.deepcopy(self)
        curve._data = [x for x in curve._data if x[-1] >= snr]
        return curve


class DuplicateImageError(sqlite3.IntegrityError):
    """Raised if two Images with the same Unix time are added to a LEMONdB"""
    pass


class DuplicateStarError(sqlite3.IntegrityError):
    """Raised if two stars with the same ID are added to a LEMONdB"""
    pass


class UnknownStarError(sqlite3.IntegrityError):
    """Raised when a star foreign key constraint fails"""
    pass


class UnknownImageError(sqlite3.IntegrityError):
    """Raised when an image foreign key constraint fails"""
    pass


class DuplicatePhotometryError(sqlite3.IntegrityError):
    """Raised if more than one record for the same star and image is added"""
    pass


class DuplicateLightCurvePointError(sqlite3.IntegrityError):
    """If more than one curve point for the same star and image is added"""
    pass


StarInfo = collections.namedtuple(
    "_StarInfo",
    [
        "x",      # the x- and y- coordinates of the star...
        "y",      # ... in the image where it was detected.
        "ra",     # right ascension
        "dec",    # declination
        "epoch",  # astronomical epoch
        "pm_ra",  # proper motions in right ascension...
        "pm_dec", # ... and declination.
        "imag",   # instrumental magnitude in the sources image.
    ],
)


class LEMONdB(object):
    """Interface to the SQLite database used to store our results"""

    def __init__(self, path, dtype=numpy.longdouble):
        self.path = path
        self.dtype = dtype
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self._cursor = self.connection.cursor()

        # Enable foreign key support (SQLite >= 3.6.19)
        self._execute("PRAGMA foreign_keys = ON")
        self._execute("PRAGMA foreign_keys")
        if not list(self._rows)[0]:
            raise sqlite3.NotSupportedError("foreign key support is not enabled")

        self._start()
        self._create_tables()
        self.commit()

    def nstars(self):
        return len(self)

    def filters(self):
        return list(self.pfilters)

    def _close(self):
        self._cursor.close()
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self._close()

    def _execute(self, query, t=()):
        """Execute SQL query; returns nothing."""
        self._cursor.execute(query, t)

    @property
    def _rows(self):
        """Return an iterator over the rows returned by the last query."""
        return self._cursor

    def _start(self):
        """Start a new transaction."""
        self._execute("BEGIN TRANSACTION")

    def _end(self):
        """End the current transaction."""
        self._execute("END TRANSACTION")

    def commit(self):
        """Make the changes of the current transaction permanent.
        Automatically starts a new transaction."""
        self._end()
        self._start()

    def _savepoint(self, name=None):
        """Start a new savepoint, use a random name if not given any.
        Returns the name of the savepoint that was started."""
        if not name:
            name = "".join(random.sample(string.ascii_letters, 12))
        self._execute(f"SAVEPOINT {name}")
        return name

    def _rollback_to(self, name):
        """Revert the state of the database to a savepoint."""
        self._execute(f"ROLLBACK TO {name}")

    def _release(self, name):
        """Remove from the transaction stack all savepoints back to and
        including the most recent savepoint with this name."""
        self._execute(f"RELEASE {name}")

    def analyze(self):
        """Run the ANALYZE command and commit automatically."""
        self._execute("ANALYZE")
        self.commit()

    def _create_tables(self):
        """Create, if needed, the tables used by the database."""
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key   TEXT NOT NULL,
                value BLOB,
                UNIQUE (key))
            """
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS stars (
                id     INTEGER PRIMARY KEY,
                x      REAL NOT NULL,
                y      REAL NOT NULL,
                ra     REAL NOT NULL,
                dec    REAL NOT NULL,
                epoch  REAL NOT NULL,
                pm_ra  REAL,
                pm_dec REAL,
                imag   REAL NOT NULL)
            """
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS photometric_filters (
                id    INTEGER PRIMARY KEY,
                name  TEXT UNIQUE NOT NULL)
            """
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS photometric_parameters (
                id       INTEGER PRIMARY KEY,
                aperture INTEGER NOT NULL,
                annulus  INTEGER NOT NULL,
                dannulus INTEGER NOT NULL)
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS phot_params_all_rows "
            "ON photometric_parameters(aperture, annulus, dannulus)"
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS candidate_parameters (
                id         INTEGER PRIMARY KEY,
                pparams_id INTEGER NOT NULL,
                filter_id  INTEGER NOT NULL,
                stdev      REAL NOT NULL,
                FOREIGN KEY (pparams_id) REFERENCES photometric_parameters(id),
                FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
                UNIQUE (pparams_id, filter_id))
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS cand_filter "
            "ON candidate_parameters(filter_id)"
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS images (
                id         INTEGER PRIMARY KEY,
                path       TEXT NOT NULL,
                filter_id  INTEGER,
                unix_time  REAL,
                object     TEXT,
                airmass    REAL,
                gain       REAL,
                ra         REAL NOT NULL,
                dec        REAL NOT NULL,
                sources    INTEGER NOT NULL,
                FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
                UNIQUE (filter_id, unix_time))
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS img_by_filter_time "
            "ON images(filter_id, unix_time)"
        )

        # Enforce a maximum of one sources image (SOURCES == 1)
        for index, when in enumerate(("INSERT", "UPDATE OF sources")):
            stmt = """CREATE TRIGGER IF NOT EXISTS single_sources_%d
                      AFTER %s ON images
                      BEGIN
                          SELECT RAISE(ABORT, 'only one SOURCES column may be = 1')
                          WHERE (SELECT COUNT(*)
                                 FROM images
                                 WHERE sources = 1) > 1;
                      END; """ % (
                index,
                when,
            )
            self._execute(stmt)

        # Although FILTER_ID, UNIX_TIME, AIRMASS and GAIN may be NULL, we only
        # allow this for the sources image (SOURCES == 1).
        for field in ("FILTER_ID", "UNIX_TIME", "AIRMASS", "GAIN"):
            for index, where in enumerate(("INSERT", "UPDATE OF " + field)):
                stmt = """CREATE TRIGGER IF NOT EXISTS {0}_not_null_{1}
                           AFTER {2} ON images
                           FOR EACH ROW
                           WHEN NEW.{0} is NULL AND NEW.sources != 1
                           BEGIN
                               SELECT RAISE(ABORT, '{0} may not be NULL unless SOURCES = 1');
                           END; """.format(
                    field, index, where
                )
                self._execute(stmt)

        # Require RA to be in range [0, 360[
        for index, when in enumerate(["INSERT", "UPDATE OF ra"]):
            stmt = """CREATE TRIGGER IF NOT EXISTS ra_within_range_%d
                       AFTER %s ON images
                       FOR EACH ROW
                       WHEN (NEW.ra NOT BETWEEN 0 AND 360) OR (NEW.ra = 360)
                       BEGIN
                           SELECT RAISE(ABORT, 'RA out of range [0, 360[');
                       END; """ % (
                index,
                when,
            )
            self._execute(stmt)

        # Require DEC to be in range [-90, 90]
        for index, when in enumerate(["INSERT", "UPDATE OF dec"]):
            stmt = """CREATE TRIGGER IF NOT EXISTS dec_within_range_%d
                       AFTER %s ON images
                       FOR EACH ROW
                       WHEN NEW.dec NOT BETWEEN -90 AND 90
                       BEGIN
                           SELECT RAISE(ABORT, 'DEC out of range [-90, 90]');
                       END; """ % (
                index,
                when,
            )
            self._execute(stmt)

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS raw_images (
                id   INTEGER PRIMARY KEY,
                fits BLOB NOT NULL,
                FOREIGN KEY (id) REFERENCES images(id))
            """
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS pm_corrections (
                id         INTEGER PRIMARY KEY,
                star_id    INTEGER NOT NULL,
                image_id   INTEGER NOT NULL,
                x          REAL NOT NULL,
                y          REAL NOT NULL,
                FOREIGN KEY (star_id)  REFERENCES stars(id),
                FOREIGN KEY (image_id) REFERENCES images(id),
                UNIQUE (star_id, image_id))
            """
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS photometry (
                id         INTEGER PRIMARY KEY,
                star_id    INTEGER NOT NULL,
                image_id   INTEGER NOT NULL,
                magnitude  REAL NOT NULL,
                snr        REAL NOT NULL,
                FOREIGN KEY (star_id)  REFERENCES stars(id),
                FOREIGN KEY (image_id) REFERENCES images(id),
                UNIQUE (star_id, image_id))
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS phot_by_star_image "
            "ON photometry(star_id, image_id)"
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS phot_by_image ON photometry(image_id)"
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS light_curves (
                id         INTEGER PRIMARY KEY,
                star_id    INTEGER NOT NULL,
                image_id   INTEGER NOT NULL,
                magnitude  REAL NOT NULL,
                snr        REAL,
                FOREIGN KEY (star_id)  REFERENCES stars(id),
                FOREIGN KEY (image_id) REFERENCES images(id),
                UNIQUE (star_id, image_id))
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS curve_by_star_image "
            "ON light_curves(star_id, image_id)"
        )

        self._execute(
            """
            CREATE TABLE IF NOT EXISTS cmp_stars (
                id        INTEGER PRIMARY KEY,
                star_id   INTEGER NOT NULL,
                filter_id INTEGER NOT NULL,
                cstar_id  INTEGER NOT NULL,
                stdev     REAL NOT NULL,
                weight    REAL NOT NULL,
                FOREIGN KEY (star_id)    REFERENCES stars(id),
                FOREIGN KEY (filter_id) REFERENCES photometric_filters(id),
                FOREIGN KEY (cstar_id)   REFERENCES stars(id))
            """
        )

        self._execute(
            "CREATE INDEX IF NOT EXISTS cstars_by_star_filter "
            "ON cmp_stars(star_id, filter_id)"
        )

    def _table_count(self, table):
        """Return the number of rows in 'table'."""
        self._execute(f"SELECT COUNT(*) FROM {table}")
        rows = list(self._rows)
        assert len(rows) == 1
        return rows[0][0]

    def _add_pfilter(self, pfilter):
        """Store a photometric filter in the database."""
        if not isinstance(pfilter, passband.Passband):
            msg = "'pfilter' must be a Passband object"
            raise ValueError(msg)
        t = (hash(pfilter), str(pfilter))
        self._execute("INSERT OR IGNORE INTO photometric_filters VALUES (?, ?)", t)

    @property
    def _pparams_ids(self):
        """Return the ID of the photometric parameters, in ascending order."""
        self._execute("SELECT id FROM photometric_parameters ORDER BY id ASC")
        return [x[0] for x in self._rows]

    def _get_pparams(self, id_):
        """Return the PhotometricParamaters with this ID. Raises KeyError if missing."""
        self._execute(
            "SELECT aperture, annulus, dannulus "
            "FROM photometric_parameters "
            "WHERE id = ?",
            (id_,),
        )
        rows = list(self._rows)
        if not rows:
            raise KeyError(f"{id_}")
        args = rows[0]
        return PhotometricParameters(*args)

    def _add_pparams(self, pparams):
        """Add a PhotometricParameters instance and return its ID (or existing ID)."""
        t = [pparams.aperture, pparams.annulus, pparams.dannulus]
        self._execute(
            "SELECT id "
            "FROM photometric_parameters "
            "     INDEXED BY phot_params_all_rows "
            "WHERE aperture = ? "
            "  AND annulus  = ? "
            "  AND dannulus = ?",
            t,
        )
        try:
            return list(self._rows)[0][0]
        except IndexError:
            t.insert(0, None)
            self._execute("INSERT INTO photometric_parameters VALUES (?, ?, ?, ?)", t)
            return self._cursor.lastrowid

    def add_candidate_pparams(self, candidate_annuli, pfilter):
        """Store a CandidateAnnuli instance into the LEMONdB."""
        pparams_id = self._add_pparams(candidate_annuli)
        self._add_pfilter(pfilter)
        t = (None, pparams_id, hash(pfilter), candidate_annuli.stdev)
        self._execute(
            "INSERT OR REPLACE INTO candidate_parameters VALUES (?, ?, ?, ?)", t
        )

    def get_candidate_pparams(self, pfilter):
        """Return all the CandidateAnnuli for a photometric filter."""
        t = (hash(pfilter),)
        self._execute(
            "SELECT p.aperture, p.annulus, p.dannulus, c.stdev "
            " FROM candidate_parameters AS c "
            "      INDEXED BY cand_filter, "
            "      photometric_parameters AS p "
            "ON c.pparams_id = p.id "
            "WHERE c.filter_id = ? "
            "ORDER BY c.stdev ASC",
            t,
        )
        return [json_parse.CandidateAnnuli(*args) for args in self._rows]

    def _get_simage_id(self):
        """Return the ID of the image on which sources were detected."""
        self._execute("SELECT id FROM images WHERE sources = 1")
        rows = list(self._rows)
        if not rows:
            raise KeyError("sources image has not yet been set")
        assert len(rows) == 1
        return rows[0][0]

    @property
    def simage(self):
        """Return the FITS image on which sources were detected."""
        try:
            id_ = self._get_simage_id()
        except KeyError:
            return None

        t = (id_,)
        self._execute(
            "SELECT i.path, p.name, i.unix_time, i.object, "
            "       i.airmass, i.gain, i.ra, i.dec "
            "FROM images AS i, photometric_filters AS p "
            "ON i.filter_id = p.id "
            "WHERE i.id = ?",
            t,
        )

        rows = list(self._rows)
        assert len(rows) == 1
        args = list(rows[0])
        args[1] = passband.Passband(args[1])
        return Image(*args)

    @simage.setter
    def simage(self, image):
        """Set the FITS image on which sources were detected."""
        self.add_image(image, _is_sources_img=True)

        with open(image.path, "rb") as fd:
            blob = fd.read()

        # Get the ID of the sources image and store it as a blob
        self._execute("SELECT id FROM images WHERE sources = 1")
        rows = list(self._rows)
        assert len(rows) == 1
        id_ = rows[0][0]
        t = (id_, sqlite3.Binary(blob))
        self._execute("INSERT OR REPLACE INTO raw_images VALUES (?, ?)", t)

    def add_image(self, image, _is_sources_img=False):
        """Store information about a FITS image in the database."""
        mark = self._savepoint()

        if image.pfilter is None and not _is_sources_img:
            msg = "image.pfilter may not be NULL unless SOURCES = 1"
            raise sqlite3.IntegrityError(msg)

        if image.pfilter:
            self._add_pfilter(image.pfilter)

        t = (
            None,
            image.path,
            hash(image.pfilter) if image.pfilter else None,
            image.unix_time,
            image.object,
            image.airmass,
            image.gain,
            image.ra,
            image.dec,
            int(_is_sources_img),
        )

        try:
            if _is_sources_img:
                self._execute("UPDATE images SET sources = 0 WHERE sources = 1")

            self._execute(
                "INSERT INTO images VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", t
            )
            self._release(mark)

        except Exception as e:
            self._rollback_to(mark)

            unix_time = image.unix_time
            pfilter = image.pfilter

            t = (unix_time, hash(pfilter))
            self._execute(
                """SELECT *
                   FROM images
                   WHERE unix_time = ?
                     AND filter_id = ? """,
                t,
            )

            if list(self._rows):
                msg = (
                    "Image with Unix time %.4f (%s) and filter %s already "
                    "in database"
                )
                args = (unix_time, util.utctime(unix_time), pfilter)
                raise DuplicateImageError(msg % args)

            raise e

    def _get_image_id(self, unix_time, pfilter):
        """Return the ID of the Image with this Unix time and filter."""
        t = (float(unix_time), hash(pfilter))
        self._execute(
            "SELECT id "
            "FROM images INDEXED BY img_by_filter_time "
            "WHERE unix_time = ? "
            "  AND filter_id = ?",
            t,
        )
        rows = list(self._rows)
        if not rows:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, util.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        assert len(rows) == 1
        assert len(rows[0]) == 1
        return rows[0][0]

    def get_image(self, unix_time, pfilter):
        """Return the Image observed at a Unix time and photometric filter."""
        image_id = self._get_image_id(unix_time, pfilter)
        self._execute(
            "SELECT i.path, p.name, i.unix_time, i.object, "
            "       i.airmass, i.gain, i.ra, i.dec "
            "FROM images AS i, photometric_filters AS p "
            "ON i.filter_id = p.id "
            "WHERE i.id = ?",
            (image_id,),
        )

        rows = list(self._rows)
        if not rows:
            msg = "%.4f (%s) and filter %s"
            args = unix_time, util.utctime(unix_time), pfilter
            raise KeyError(msg % args)
        args = list(rows[0])
        args[1] = passband.Passband(args[1])
        return Image(*args)

    def add_star(self, star_id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag):
        """Add a star to the database."""
        t = (star_id, x, y, ra, dec, epoch, pm_ra, pm_dec, imag)
        try:
            stmt = "INSERT INTO stars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            self._execute(stmt, t)
        except sqlite3.IntegrityError:
            if __debug__:
                self._execute("SELECT id FROM stars")
                assert (star_id,) in self._rows
            msg = f"star with ID = {star_id} already in database"
            raise DuplicateStarError(msg)

    def get_star(self, star_id):
        """Returns a StarInfo namedtuple with information about the star."""
        t = (star_id,)
        self._execute(
            "SELECT x, y, ra, dec, epoch, pm_ra, pm_dec, imag "
            "FROM stars "
            "WHERE id = ?",
            t,
        )
        try:
            return StarInfo(*next(self._rows))
        except StopIteration:
            raise KeyError(f"star with ID = {star_id} not in database")

    def __len__(self):
        """Return the number of stars in the database."""
        return self._table_count("STARS")

    @property
    def star_ids(self):
        """Return a list with the ID of the stars, in ascending order."""
        self._execute("SELECT id FROM stars ORDER BY id ASC")
        return _ListCallable([x[0] for x in self._rows])

    def images(self, pfilter, star_ids=None):
        """Return a sorted list of Unix times for images in a photometric filter.

        If 'star_ids' is provided (iterable of ints), only return the Unix times
        of images in which *all* those stars have photometry in that filter.
        """
        # Accept strings too; normalize to Passband because DB stores hash(pfilter)
        if not isinstance(pfilter, passband.Passband):
            pfilter = passband.Passband(str(pfilter))

        if not star_ids:
            # All images for the filter
            self._execute(
                "SELECT unix_time "
                "FROM images INDEXED BY img_by_filter_time "
                "WHERE filter_id = ? "
                "ORDER BY unix_time ASC",
                (hash(pfilter),),
            )
            return [row[0] for row in self._rows]

        # Ensure we have a concrete list and it’s not empty
        star_ids = list(star_ids)
        if not star_ids:
            return []

        # Images in this filter that have photometry for *all* given stars.
        # We group by image and require count(distinct star_id) == number of stars.
        placeholders = ",".join("?" for _ in star_ids)
        params = [hash(pfilter)] + star_ids + [len(star_ids)]
        self._execute(
            "SELECT img.unix_time "
            "FROM images AS img INDEXED BY img_by_filter_time "
            "JOIN photometry AS phot INDEXED BY phot_by_image "
            "  ON phot.image_id = img.id "
            f"WHERE img.filter_id = ? AND phot.star_id IN ({placeholders}) "
            "GROUP BY img.id "
            "HAVING COUNT(DISTINCT phot.star_id) = ? "
            "ORDER BY img.unix_time ASC",
            tuple(params),
        )
        return [row[0] for row in self._rows]

    def add_pm_correction(self, star_id, unix_time, pfilter, pm_x, pm_y):
        """Store the proper-motion corrected pixel coordinates of a star."""
        try:
            if None in self.get_star(star_id)[5:7]:
                msg = (
                    "astronomical object with ID = %d does not have proper "
                    "motions, so we cannot store proper-motion corrections "
                    "for it. Where do these values come from?" % star_id
                )
                raise ValueError(msg)
        except KeyError:
            msg = f"star with ID = {star_id} not in database"
            raise UnknownStarError(msg)

        try:
            image_id = self._get_image_id(unix_time, pfilter)
        except KeyError as e:
            raise UnknownImageError(str(e))

        t = (None, star_id, image_id, float(pm_x), float(pm_y))
        stmt = "INSERT INTO pm_corrections VALUES (?, ?, ?, ?, ?)"
        self._execute(stmt, t)

    def get_pm_correction(self, star_id, unix_time, pfilter):
        """Return the proper-motion correction of a star in an image."""
        image_id = self._get_image_id(unix_time, pfilter)
        t = (int(star_id), image_id)
        self._execute(
            """SELECT x, y
               FROM pm_corrections
               WHERE  star_id = ?
                 AND image_id = ?""",
            t,
        )
        rows = tuple(self._rows)
        try:
            return rows[0]
        except IndexError:
            if star_id not in self.star_ids:
                msg = f"star with ID = {star_id} not in database"
                raise KeyError(msg)
            else:
                return None, None

    def add_photometry(self, star_id, unix_time, pfilter, magnitude, snr):
        """Store the photometric record of a star at a given time and filter."""
        try:
            image_id = self._get_image_id(unix_time, pfilter)
            t = (None, star_id, image_id, float(magnitude), float(snr))
            self._execute("INSERT INTO photometry VALUES (?, ?, ?, ?, ?)", t)

        except KeyError as e:
            raise UnknownImageError(str(e))

        except sqlite3.IntegrityError:
            if star_id not in self.star_ids:
                msg = f"star with ID = {star_id} not in database"
                raise UnknownStarError(msg)

            msg = (
                "photometry for star ID = %d, Unix time = %4.f "
                "(%s) and filter %s already in database"
            )
            args = (star_id, unix_time, util.utctime(unix_time), pfilter)
            raise DuplicatePhotometryError(msg % args)

    def get_photometry(self, star_id, pfilter):
        """Return the photometric information of the star."""
        if star_id not in self.star_ids:
            msg = f"star with ID = {star_id} not in database"
            raise KeyError(msg)

        t = (int(star_id), hash(pfilter))
        self._execute(
            "SELECT img.unix_time, phot.magnitude, phot.snr "
            "FROM photometry AS phot INDEXED BY phot_by_star_image, "
            "     images AS img INDEXED BY img_by_filter_time "
            "ON phot.image_id = img.id "
            "WHERE phot.star_id = ? "
            "  AND img.filter_id = ? "
            "ORDER BY img.unix_time ASC",
            t,
        )

        args = star_id, pfilter, list(self._rows)
        return DBStar.make_star(*args, dtype=self.dtype)

    def _star_pfilters(self, star_id):
        """Return the photometric filters for which the star has data."""
        if star_id not in self.star_ids:
            msg = f"star with ID = {star_id} not in database"
            raise KeyError(msg)

        t = (star_id,)
        self._execute(
            """SELECT DISTINCT f.name
               FROM (SELECT DISTINCT image_id
                     FROM photometry INDEXED BY phot_by_star_image
                     WHERE star_id = ?) AS phot
               INNER JOIN images AS img
               ON phot.image_id = img.id
               INNER JOIN photometric_filters AS f
               ON img.filter_id = f.id """,
            t,
        )

        return sorted(passband.Passband(x[0]) for x in self._rows)

    @property
    def pfilters(self):
        """Return the photometric filters for which there is data."""
        self._execute(
            """SELECT DISTINCT f.name
               FROM (SELECT DISTINCT image_id
                     FROM photometry INDEXED BY phot_by_image)
                     AS phot
               INNER JOIN images AS img
               ON phot.image_id = img.id
               INNER JOIN photometric_filters AS f
               ON img.filter_id = f.id """
        )
        return sorted(passband.Passband(x[0]) for x in self._rows)

    def _add_curve_point(self, star_id, unix_time, pfilter, magnitude, snr):
        """Store a point of the light curve of a star."""
        try:
            image_id = self._get_image_id(unix_time, pfilter)
            t = (None, star_id, image_id, float(magnitude), float(snr))
            self._execute("INSERT INTO light_curves VALUES (?, ?, ?, ?, ?)", t)

        except KeyError as e:
            raise UnknownImageError(str(e))

        except sqlite3.IntegrityError:
            if star_id not in self.star_ids:
                msg = f"star with ID = {star_id} not in database"
                raise UnknownStarError(msg)

            msg = (
                "light curve point for star ID = %d, Unix time = %4.f "
                "(%s) and filter %s already in database"
            )
            args = (star_id, unix_time, util.utctime(unix_time), pfilter)
            raise DuplicateLightCurvePointError(msg % args)

    def _add_cmp_star(self, star_id, pfilter, cstar_id, cweight, cstdev):
        """Add a comparison star to the light curve of a star."""
        if star_id == cstar_id:
            msg = f"star with ID = {star_id} cannot use itself as comparison"
            raise ValueError(msg)

        mark = self._savepoint()
        try:
            self._add_pfilter(pfilter)
            cweight = float(cweight)
            cstdev = float(cstdev)
            t = (None, star_id, hash(pfilter), cstar_id, cstdev, cweight)
            self._execute("INSERT INTO cmp_stars VALUES (?, ?, ?, ?, ?, ?)", t)
            self._release(mark)

        except sqlite3.IntegrityError:
            self._rollback_to(mark)
            if star_id not in self.star_ids:
                msg = f"star with ID = {star_id} not in database"
                raise UnknownStarError(msg)
            else:
                msg = f"comparison star with ID = {cstar_id} not in database"
                raise UnknownStarError(msg)

    def add_light_curve(self, star_id, light_curve):
        """Store the light curve of a star (atomic transaction)."""
        mark = self._savepoint()
        try:
            for unix_time, magnitude, snr in light_curve:
                args = star_id, unix_time, light_curve.pfilter, magnitude, snr
                self._add_curve_point(*args)
            for weight in light_curve.weights():
                self._add_cmp_star(star_id, light_curve.pfilter, *weight)
            self._release(mark)
        except Exception:
            self._rollback_to(mark)
            raise

    def get_light_curve(self, star_id, pfilter):
        """Return the light curve of a star."""
        err_msg = f"star with ID = {star_id} "

        t = (star_id, hash(pfilter))
        self._execute(
            "SELECT img.unix_time, curve.magnitude, curve.snr "
            "FROM light_curves AS curve INDEXED BY curve_by_star_image, "
            "     images AS img INDEXED BY img_by_filter_time "
            "ON curve.image_id = img.id "
            "WHERE curve.star_id = ? "
            "  AND img.filter_id = ? "
            "ORDER BY img.unix_time ASC",
            t,
        )
        curve_points = list(self._rows)

        if curve_points:
            self._execute(
                "SELECT cstar_id, weight, stdev "
                "FROM cmp_stars INDEXED BY cstars_by_star_filter "
                "WHERE star_id = ? "
                "  AND filter_id = ? "
                "ORDER BY cstar_id",
                t,
            )

            rows = list(self._rows)
            if not rows:
                msg = err_msg + f"has no comparison stars (?) in {pfilter}"
                raise sqlite3.IntegrityError(msg)
            cstars, cweights, cstdevs = zip(*rows)
        else:
            if star_id not in self.star_ids:
                msg = err_msg + "not in database"
                raise KeyError(msg)
            return None

        curve = LightCurve(pfilter, cstars, cweights, cstdevs, dtype=self.dtype)
        for point in curve_points:
            curve.add(*point)
        return curve

    def get_instrumental_magnitudes(self, star_id, pfilter):
        """Return the instrumental magnitudes of an astronomical object."""
        t = (star_id, hash(pfilter))
        self._execute(
            """
            SELECT i.unix_time, p.magnitude, p.snr
            FROM photometry AS p, images AS i
            ON p.image_id = i.id
            WHERE p.star_id = ? AND
                  i.filter_id = ?
            """,
            t,
        )

        cls = collections.namedtuple("InstrumentalMagnitude", "magnitude snr")
        return dict((r[0], cls(*r[1:])) for r in self._rows)

    def airmasses(self, pfilter):
        """Return the airmasses of the images in a photometric filter."""
        t = (hash(pfilter),)
        self._execute(
            "SELECT unix_time, airmass "
            "FROM images INDEXED BY img_by_filter_time "
            "WHERE filter_id = ? ",
            t,
        )
        return dict(self._rows)

    def get_phase_diagram(self, star_id, pfilter, period, repeat=1):
        """Return the folded light curve of a star."""
        curve = self.get_light_curve(star_id, pfilter)
        if curve is None:
            return None

        phase = LightCurve(
            pfilter, curve.cstars, curve.cweights, curve.cstdevs, dtype=curve.dtype
        )

        unix_times, magnitudes, snrs = zip(*curve)
        zero_t = min(unix_times)

        phased_x = []
        for utime, mag, snr in zip(unix_times, magnitudes, snrs):
            fractional_part = math.modf((utime - zero_t) / period)[0]
            phased_x.append(fractional_part)
        assert len(phased_x) == len(unix_times)

        x_max = 1
        phased_unix_times = phased_x[:]
        for _ in range(repeat - 1):  # -1 as there is already one (phased_x)
            phased_unix_times += [utime + x_max for utime in phased_x]
            x_max += 1

        assert len(phased_unix_times) == len(unix_times) * repeat
        phased_magnitudes = magnitudes * repeat
        phased_snr = snrs * repeat

        for utime, mag, snr in zip(phased_unix_times, phased_magnitudes, phased_snr):
            phase.add(utime, mag, snr)

        assert len(phase) == len(curve) * repeat
        return phase

    def most_similar_magnitude(self, star_id, pfilter):
        """Iterate over the stars sorted by their similarity in magnitude."""
        magnitudes = [
            (id_, self.get_star(id_)[-1]) for id_ in self.star_ids if id_ != star_id
        ]

        rmag = self.get_star(star_id)[-1]
        magnitudes.sort(key=lambda x: abs(rmag - x[1]))
        for id_, imag in magnitudes:
            if self.get_light_curve(id_, pfilter):
                yield id_, imag

    @property
    def field_name(self):
        """Determine the name of the field observed during a campaign."""
        self._execute("SELECT object FROM images")
        object_names = (x[0] for x in self._rows)

        object_names = [name for name in object_names if name is not None]
        if not object_names:
            raise ValueError("database contains no images")

        substrings = collections.defaultdict(int)
        for name in object_names:
            for index in range(1, len(name) + 1):
                substrings[name[:index]] += 1

        def startswith_counter(prefix, names):
            """Return the number of strings in 'names' starting with 'prefix'."""
            return sum(name.startswith(prefix) for name in names)

        longest = sorted(substrings.keys(), key=len, reverse=True)
        minimum_matches = len(object_names) // 2 + 1
        for prefix in longest:
            if startswith_counter(prefix, object_names) >= minimum_matches:
                return prefix.strip(" _")  # e.g., "NGC 2276_" to "NGC 2264"

    def _set_metadata(self, key, value):
        """Set (or replace) the value of a record in the METADATA table."""
        if not isinstance(key, str):
            raise ValueError("key must be a string")

        if not ((value is None) or (isinstance(value, (str, numbers.Real)))):
            raise ValueError("value must be a string, number or None")

        t = (str(key), value)
        self._execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", t)

    def _get_metadata(self, key):
        """Return the value of a record in the METADATA table."""
        t = (key,)
        self._execute("SELECT value FROM metadata WHERE key = ?", t)
        rows = tuple(self._rows)
        if not rows:
            msg = "METADATA table has no record '%s'" % key
            raise AttributeError(msg)
        assert len(rows) == 1
        assert len(rows[0]) == 1
        return rows[0][0]

    def _del_metadata(self, key):
        """Delete a record in the METADATA table."""
        self._get_metadata(key)
        t = (key,)
        self._execute("DELETE FROM metadata WHERE key = ?", t)

    @property
    def mosaic(self):
        """Save to disk the FITS file on which sources were detected."""
        try:
            id_ = self._get_simage_id()
        except KeyError:
            return None

        self._execute("SELECT fits FROM raw_images WHERE id = ?", (id_,))
        rows = list(self._rows)
        assert len(rows) == 1
        assert len(rows[0]) == 1
        blob = rows[0][0]
        fd, path = tempfile.mkstemp(suffix=".fits")
        os.write(fd, blob)
        os.close(fd)
        return path

    def star_closest_to_world_coords(self, ra, dec):
        """Find the star closest to a right ascension and declination."""
        if not len(self):
            raise ValueError("database is empty")

        self._execute("SELECT id, ra, dec FROM stars")

        closest_id = None
        closest_distance = float("inf")
        coordinates = astromatic.Coordinates(ra, dec)
        for star_id, star_ra, star_dec in self._rows:
            star_coords = astromatic.Coordinates(star_ra, star_dec)
            star_distance = coordinates.distance(star_coords)
            if star_distance < closest_distance:
                closest_id = star_id
                closest_distance = star_distance

        return closest_id, closest_distance


def _add_metadata_property(name):
    """Dynamically add a property to the LEMONdB class."""
    def getter(self):
        try:
            return self._get_metadata(name)
        except AttributeError:
            msg = "'LEMONdB' object has no attribute '%s'" % name.lower()
            raise AttributeError(msg)

    setter = lambda self, value: self._set_metadata(name, value)
    deleter = lambda self: self._del_metadata(name)
    setattr(LEMONdB, name.lower(), property(getter, setter, deleter))


_add_metadata_property("DATE")      # date of creation of the LEMONdB
_add_metadata_property("AUTHOR")    # who ran LEMON to create the LEMONdB
_add_metadata_property("HOSTNAME")  # where the LEMONdB was created
_add_metadata_property("ID")        # unique identifier of the LEMONdB
_add_metadata_property("VMIN")      # values for the log scale (APLpy)
_add_metadata_property("VMAX")

logger = logging.getLogger(__name__)
