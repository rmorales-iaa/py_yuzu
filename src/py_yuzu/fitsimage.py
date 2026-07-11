#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
from util import memoize

# Copyright (c) 2012 Victor Terron. All rights reserved.
# Institute of Astrophysics of Andalusia, IAA-CSIC
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""
Tell PyRAF to skip all graphics initialization and run in terminal-only mode.
Otherwise we may get warning messages (such as "could not open XWindow display"
or "No graphics display available for this session") when working at a remote
terminal or a terminal without any X Windows support. Any tasks which attempt to
display graphics will fail, but we don't need them here anyway.
"""

import calendar
import collections
import datetime
import fnmatch
import hashlib
import itertools
import logging
import numbers
import os
import re
import warnings

import numpy
from astropy.io import fits as pyfits
import astropy.io
import astropy.wcs

# LEMON modules
import util
import keywords
import passband
import style

# Ignore the "adding a HIERARCH keyword" PyFITS warning that is emitted when
# the keyword name does not start with 'HIERARCH' and is greater than eight
# characters or contains spaces. This solution is taken from:
# https://github.com/geminiutil/geminiutil/commit/9aa46fd9cd3
warnings.filterwarnings("ignore", message=r".+ a HIERARCH card will be created\.")

logger = logging.getLogger(__name__)


class NonStandardFITS(IOError):
    """Raised when a non-standard file is attempted to be opened."""
    pass


class NoWCSInformationError(ValueError):
    """Raised if WCS information is not found in a FITS header."""
    pass


class FITSImage:
    """Encapsulates a FITS image located in the filesystem."""

    def __init__(self, path: str | Path):
        """Load basic header info and validate FITS standard compliance."""
        path = str(path)
        if not Path(path).exists():
            raise IOError("file '%s' does not exist" % path)

        self.path = path

        try:
            handler = pyfits.open(self.path, mode="readonly")
            try:
                # pyfits.info(..., output=False) returns a summary table
                try:
                    info = pyfits.info(self.path, output=False)
                    # Column 2 is HDU type name for first HDU
                    type_ = info[0][2]
                    if type_ == "NonstandardHDU":
                        msg = "%s: value of 'SIMPLE' keyword is not 'T'"
                        raise NonStandardFITS(msg % self.path)
                except AttributeError as e:
                    # Older behavior path (no summary available)
                    error_msg = "'_ValidHDU' object has no attribute '_summary'"
                    if error_msg in str(e):
                        msg = "%s: 'SIMPLE' keyword missing from header"
                        raise NonStandardFITS(msg % self.path)
                    raise

                # Cache size and header
                self.size = handler[0].data.shape[::-1]
                self._header = handler[0].header
            finally:
                handler.close(output_verify="ignore")

        except IOError as e:
            pyfits_msg = "Block does not begin with SIMPLE or XTENSION"
            if str(e) == pyfits_msg:
                msg = "%s: 'SIMPLE' keyword missing from primary header"
                raise NonStandardFITS(msg % self.path)
            elif "Permission denied" in str(e):
                raise
            else:
                msg = f"{self.path} ({str(e)})"
                raise NonStandardFITS(msg)

    def __repr__(self) -> str:
        return "%s(%r)" % (self.__class__.__name__, self.path)

    def read_keyword(self, keyword: str):
        """Read a keyword from the header (case-insensitive)."""
        if keyword is None:
            raise TypeError("keyword cannot be None")
        if not keyword:
            raise ValueError("keyword cannot be empty")
        try:
            return self._header[keyword.upper()]
        except KeyError:
            msg = "%s: keyword '%s' not found" % (self.path, keyword)
            raise KeyError(msg)

    def update_keyword(self, keyword: str, value, comment: str | None = None):
        """Update or add a keyword in the FITS header."""
        if len(keyword) > 8:
            msg = (
                "%s: keyword '%s' is longer than eight characters or "
                "contains spaces; a HIERARCH card will be created"
            )
            logging.debug(msg % (self.path, keyword))

        handler = pyfits.open(self.path, mode="update")
        logging.debug("%s: file opened to update '%s' keyword", self.path, keyword)

        try:
            header = handler[0].header
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                header[keyword] = (value, comment)
                msg = "%s: keyword '%s' updated to '%s'" % (self.path, keyword, value)
                if comment:
                    msg += " with comment '%s'" % comment
                logging.debug(msg)

            # Update in-memory copy
            self._header = header

        except ValueError as e:
            pattern = r"The keyword .*? with its value is too long"
            if re.match(pattern, str(e)):
                assert len(keyword) > 8
                msg = (
                    "%s: keyword '%s' could not be updated (\"%s\"). Note "
                    "that PyFITS does not support CONTINUE for HIERARCH. "
                    "If your keyword has more than eight characters or contains spaces, "
                    "the total length of the keyword with its value cannot be longer than %d characters."
                )
                args = self.path, keyword, str(e), pyfits.Card.length
                logging.warning(msg % args)
                raise ValueError(msg % args)
            else:
                logging.warning("%s: keyword '%s' could not be updated (%s)", self.path, keyword, e)
                raise
        except Exception as e:
            logging.warning("%s: keyword '%s' could not be updated (%s)", self.path, keyword, e)
            raise
        finally:
            handler.close(output_verify="ignore")
            logging.debug("%s: file closed", self.path)

    def delete_keyword(self, keyword: str) -> None:
        """Delete a keyword from the header (no error if missing)."""
        handler = pyfits.open(self.path, mode="update")
        try:
            header = handler[0].header
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    del header[keyword]
                self._header = header
            except KeyError:
                # Future astropy may raise KeyError for missing keys; ignore
                pass
        finally:
            handler.close(output_verify="ignore")

    def add_history(self, history: str) -> None:
        """Append a HISTORY record (header copy in memory is not updated)."""
        handler = pyfits.open(self.path, mode="update")
        try:
            handler[0].header.add_history(history)
        finally:
            handler.close(output_verify="ignore")

    def date(
        self,
        date_keyword: str = "DATE-OBS",
        time_keyword: str = "TIME-OBS",
        exp_keyword: str = "EXPTIME",
    ) -> float:
        """Return the UTC mid-exposure time (Unix seconds)."""
        start_date_str = self.read_keyword(date_keyword).strip()

        format_str = "%Y-%m-%dT%H:%M:%S"

        old_date_regexp = r"\d{2}/\d{2}/\d{2}"
        date_regexp = r"\d{4}-\d{2}-\d{2}"
        time_regexp = r"\d{2}:\d{2}:\d{2}(?P<secs_fraction>\.\d{0,4})?"

        # Ideal: combined date-time in DATE-OBS
        complete_regexp = rf"{date_regexp}T{time_regexp}"
        match = re.match(complete_regexp, start_date_str)
        if match:
            if match.group("secs_fraction"):
                format_str += ".%f"
        else:
            # Only date present: read time separately
            regexp = rf"^((?P<old>{old_date_regexp})|(?P<new>{date_regexp}))$"
            match = re.match(regexp, start_date_str)
            if match:
                if match.group("old"):
                    # Old yy/mm/dd -> 19yy-mm-dd (restricted to 1900-1999)
                    start_date_str = "19" + start_date_str.replace("/", "-")

                assert re.match(rf"^{date_regexp}$", start_date_str)

                start_time_str = self.read_keyword(time_keyword).strip()
                time_match = re.match(rf"^{time_regexp}$", start_time_str)
                if not time_match:
                    msg = (
                        "'%s' keyword (%s) does not follow the FITS "
                        "standard: 'HH:MM:SS[.sss]'"
                    )
                    raise NonStandardFITS(msg % (time_keyword, start_time_str))

                start_date_str += f"T{start_time_str}"
                if time_match.group("secs_fraction"):
                    format_str += ".%f"
            else:
                msg = (
                    "'%s' keyword (%s) does not follow the FITS standard: "
                    "yyyy-mm-dd[THH:MM:SS[.sss]] or yy/dd/mm (deprecated)"
                )
                raise NonStandardFITS(msg % (date_keyword, start_date_str))

        start_date = datetime.datetime.strptime(start_date_str, format_str)

        try:
            exposure_time = self.read_keyword(exp_keyword)
            half_exp_time = float(exposure_time) / 2.0
        except KeyError:
            raise
        except ValueError:
            msg = "'%s' keyword (%s) is not a floating-point number"
            raise NonStandardFITS(msg % (exp_keyword, exposure_time))

        seconds_fraction = float(start_date.strftime(".%f"))
        start_struct_time = start_date.utctimetuple()
        start_epoch = calendar.timegm(start_struct_time) + seconds_fraction
        return start_epoch + half_exp_time

    def year(self, **kwargs) -> float:
        """Return the observation date as a fractional year (UTC)."""

        def since_epoch(d: datetime.datetime) -> int:
            return calendar.timegm(d.timetuple())

        timestamp = self.date(**kwargs)
        date = datetime.datetime.utcfromtimestamp(timestamp)

        year = date.year
        t0 = datetime.datetime(year=year, month=1, day=1)
        t1 = datetime.datetime(year=year + 1, month=1, day=1)

        year_elapsed = since_epoch(date) - since_epoch(t0)
        year_duration = since_epoch(t1) - since_epoch(t0)
        fraction = year_elapsed / year_duration
        return year + fraction

    def pfilter(self, keyword: str) -> passband.Passband:
        """Return the photometric filter as a Passband instance."""
        try:
            pfilter_str = self.read_keyword(keyword)
            return passband.Passband(pfilter_str)
        except passband.NonRecognizedPassband:
            kwargs = dict(path=self.path, keyword=keyword)
            raise passband.NonRecognizedPassband(pfilter_str, **kwargs)

    def ra(self, ra_keyword: str = keywords.rak) -> float:
        """Return right ascension (decimal degrees)."""
        logging.debug("%s: reading α from FITS header (keyword '%s')", self.path, ra_keyword)
        ra_str = self.read_keyword(ra_keyword)

        try:
            ra = float(ra_str)
            logging.debug("%s: α = %.5f", self.path, ra)
            return ra
        except ValueError as e:
            logging.debug("%s: %s", self.path, str(e))
            logging.debug("%s: parsing α as sexagesimal", self.path)
            logging.debug("%s: α = '%s'", self.path, ra_str)

            regexp = r"^(?P<hh>\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2}(\.\d{0,3})?)$"
            match = re.match(regexp, ra_str.strip())
            if match:
                hh = int(match.group("hh"))
                mm = int(match.group("mm"))
                ss = float(match.group("ss"))

                logging.debug("%s: hours = %d (α)", self.path, hh)
                logging.debug("%s: minutes = %d (α)", self.path, mm)
                logging.debug("%s: seconds = %.3f (α)", self.path, ss)

                ra = util.HMS_to_DD(hh, mm, ss)
                logging.debug("%s: α = %.5f", self.path, ra)
                return ra
            else:
                msg = "{}: '{}' not in decimal degrees or 'HH:MM:SS.sss' format"
                raise ValueError(msg.format(self.path, ra_keyword))

    def dec(self, dec_keyword: str = keywords.deck) -> float:
        """Return declination (decimal degrees)."""
        logging.debug("%s: reading δ from FITS header (keyword '%s')", self.path, dec_keyword)
        dec_str = self.read_keyword(dec_keyword)

        try:
            dec = float(dec_str)
            logging.debug("%s: δ = %.5f", self.path, dec)
            return dec
        except ValueError as e:
            logging.debug("%s: %s", self.path, str(e))
            logging.debug("%s: parsing δ as sexagesimal", self.path)
            logging.debug("%s: δ = '%s'", self.path, dec_str)

            regexp = r"^(?P<dd>([-+])?\d{2}):(?P<mm>\d{2}):(?P<ss>\d{2}(\.\d{0,3})?)$"
            match = re.match(regexp, dec_str.strip())
            if match:
                dd = int(match.group("dd"))
                mm = int(match.group("mm"))
                ss = float(match.group("ss"))

                logging.debug("%s: degrees = %d (δ)", self.path, dd)
                logging.debug("%s: minutes = %d (δ)", self.path, mm)
                logging.debug("%s: seconds = %.3f (δ)", self.path, ss)

                dec = util.DMS_to_DD(dd, mm, ss)
                logging.debug("%s: δ = %.5f", self.path, dec)
                return dec
            else:
                msg = "{}: '{}' not in decimal degrees or 'DD:MM:SS.sss' format"
                raise ValueError(msg.format(self.path, dec_keyword))

    @property
    def prefix(self) -> str:
        """Leftmost non-numeric substring of the image base name (no extension)."""
        s = ""
        basename = Path(self.path).name
        root = Path(basename).stem
        for ch in root:
            try:
                int(ch)
                return s
            except ValueError:
                s += ch
        return s

    @property
    def x_size(self) -> int:
        """Number of pixels along x-axis."""
        return self.size[0]

    @property
    def y_size(self) -> int:
        """Number of pixels along y-axis."""
        return self.size[1]

    @property
    def center(self) -> list[int]:
        """x, y coordinates of the central pixel (rounded)."""
        return [int(round(x / 2)) for x in self.size]

    def _get_wcs(self) -> astropy.wcs.WCS:
        """Return the astropy.wcs.WCS object for the header of this image."""
        with astropy.io.fits.open(self.path) as hdulist:
            header = hdulist[0].header
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return astropy.wcs.WCS(header)

    def pix2world(self, x: float, y: float) -> tuple[float, float]:
        """Transform pixel coordinates to (RA, Dec)."""
        coords = (x, y)
        wcs = self._get_wcs()
        pixcrd = numpy.array([coords])
        ra, dec = wcs.all_pix2world(pixcrd, 1)[0]

        # Simple sanity check: if no WCS, transform returns input coords
        if (ra, dec) == coords:
            msg = (
                f"{self.path}: the FITS header does not seem to contain WCS "
                "information. Ensure the image has been solved astrometrically "
                "e.g. with the 'astrometry' LEMON command."
            )
            raise NoWCSInformationError(msg)

        return ra, dec

    def center_wcs(self) -> tuple[float, float]:
        """World coordinates (RA, Dec) of the image center."""
        return self.pix2world(*tuple(self.center))

    def has_wcs(self) -> bool:
        """True if the header contains WCS information."""
        try:
            self.center_wcs()
            return True
        except NoWCSInformationError:
            return False

    def saturation(self, maximum: float, coaddk: str = keywords.coaddk) -> float:
        """Effective saturation level (ADUs) accounting for coadds."""
        if not isinstance(maximum, numbers.Real):
            raise TypeError("'maximum' must be an integer or real number")

        logging.debug("%s: saturation level of a single image: %d ADUs", self.path, maximum)

        try:
            ncoadds = self.read_keyword(coaddk)
        except KeyError:
            ncoadds = 1
            logging.debug("%s: keyword '%s' not in header, assuming value of one", self.path, coaddk)

        error_msg = "%s: value of keyword '%s' must be a positive integer" % (self.path, coaddk)

        if not isinstance(ncoadds, numbers.Integral):
            raise TypeError(error_msg)
        if ncoadds < 1:
            raise ValueError(error_msg)

        logging.debug("%s: number of effective coadds (%s keyword) = %d", self.path, coaddk, ncoadds)

        saturation = maximum * ncoadds
        logging.debug("%s: effective saturation level = %d x %d = %d", self.path, maximum, ncoadds, saturation)
        return saturation

    @property
    def sha1sum(self) -> str:
        """Hexadecimal SHA-1 checksum of the FITS image."""
        sha1 = hashlib.sha1()
        with open(self.path, "rb") as fd:
            for chunk in iter(lambda: fd.read(8192), b""):
                sha1.update(chunk)
        return sha1.hexdigest()


class InputFITSFiles(collections.defaultdict):
    """Map each photometric filter to a list of FITS files and iterate over all."""

    def __init__(self):
        super(InputFITSFiles, self).__init__(list)

    def __iter__(self):
        """Iterate over the FITS files, regardless of their filter."""
        return itertools.chain.from_iterable(self.values())

    def __len__(self):
        """Number of FITS files, in any filter."""
        return sum(len(pfilter) for pfilter in self.values())

    def remove(self, img: FITSImage) -> int:
        """Remove all occurrences of the FITS file (any filter); return count."""
        discarded = 0
        for pfilter_imgs in self.values():
            for index in reversed(range(len(pfilter_imgs))):
                if pfilter_imgs[index] == img:
                    del pfilter_imgs[index]
                    discarded += 1
                    warnings.warn("%s %s excluded." % (style.prefix, img))
        return discarded


def find_files(
    paths: list[str | Path],
    followlinks: bool = True,
    pattern: str | None = None,
) -> list[str]:
    """Find all regular files under the given paths (recursing into directories)."""
    files_paths: list[str] = []
    for path in sorted(map(str, paths)):
        if Path(path).is_file():
            basename = Path(path).name
            if not pattern or fnmatch.fnmatch(basename, pattern):
                files_paths.append(path)
        elif Path(path).is_dir():
            for dirpath, dirnames, filenames in os.walk(path, followlinks=followlinks):
                dirnames.sort()
                for basename in sorted(filenames):
                    abs_path = Path(dirpath) / basename
                    files_paths += find_files([abs_path], followlinks=followlinks, pattern=pattern)
    return files_paths
