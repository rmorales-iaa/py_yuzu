#!/usr/bin/env python3
from __future__ import annotations

import collections
import functools
import hashlib
import logging
import math
import os
import re
import subprocess
import tempfile
from pathlib import Path
from shutil import which

import astropy.coordinates
import astropy.units

# LEMON modules
# If you actually need util.* helpers here, keep the import.
# Otherwise you can remove it.
import util  # noqa: F401  (kept for compatibility)

logger = logging.getLogger(__name__)

# Configuration paths
CONFIG_FILES_DIR = Path(__file__).resolve().parent / "sextractor"
SEXTRACTOR_CONFIG = CONFIG_FILES_DIR / "sextractor.sex"
SEXTRACTOR_PARAMS = CONFIG_FILES_DIR / "sextractor.param"
SEXTRACTOR_FILTER = CONFIG_FILES_DIR / "sextractor.conv"
SEXTRACTOR_STARNNW = CONFIG_FILES_DIR / "sextractor.nnw"

# Executable names to try
SEXTRACTOR_COMMANDS = ("sextractor", "sex")
SEXTRACTOR_REQUIRED_VERSION = (2, 19, 5)


class SExtractorNotInstalled(Exception):
    def __str__(self) -> str:
        return "SExtractor not found in the current environment"


class SExtractorUpgradeRequired(Exception):
    """Raised if a too-old version of SExtractor is installed."""

    def __str__(self) -> str:
        req = ".".join(map(str, SEXTRACTOR_REQUIRED_VERSION))
        try:
            found_tuple = sextractor_version()
            found = ".".join(map(str, found_tuple))
        except Exception:
            found = "unknown"
        return f"SExtractor >= {req} is required, found {found}"


class SExtractorError(subprocess.CalledProcessError):
    pass


#: A pair of immutable x- and y-coordinates.
Pixel = collections.namedtuple("Pixel", "x y")


class Coordinates(collections.namedtuple("Coordinates", "ra dec pm_ra pm_dec")):
    """Immutable celestial coordinates of an astronomical object."""

    def __new__(cls, ra: float, dec: float, pm_ra: float = 0, pm_dec: float = 0):
        return super(Coordinates, cls).__new__(cls, ra, dec, pm_ra, pm_dec)

    def distance(self, another: "Coordinates") -> float:
        """Angular distance in degrees between two coordinates."""
        make_coord = functools.partial(
            astropy.coordinates.SkyCoord, unit=astropy.units.deg
        )
        c1 = make_coord(ra=self.ra, dec=self.dec)
        c2 = make_coord(ra=another.ra, dec=another.dec)
        return c1.separation(c2).deg

    def get_exact_coordinates(self, year: float, epoch: float = 2000) -> "Coordinates":
        """Apply proper motion correction and return exact coordinates.

        pm_* are arcsec/yr; convert to degrees.
        """
        elapsed = year - epoch
        ra = self.ra + (self.pm_ra * elapsed) / 3600.0
        dec = self.dec + (self.pm_dec * elapsed) / 3600.0
        return self.__class__(ra, dec, None, None)


class Star(
    collections.namedtuple(
        "_Star",
        "img_coords sky_coords area mag saturated snr fwhm elongation",
    )
):
    """An immutable class representing a source detected by SExtractor."""

    def __new__(
        cls,
        x: float,
        y: float,
        alpha: float,
        delta: float,
        area: int,
        mag: float,
        satur: bool,
        snr: float,
        fwhm: float,
        elong: float,
    ):
        img_coords = Pixel(x, y)
        sky_coords = Coordinates(alpha, delta)
        args = (img_coords, sky_coords, area, mag, satur, snr, fwhm, elong)
        return super(Star, cls).__new__(cls, *args)

    @property
    def x(self) -> float:
        return self.img_coords.x

    @property
    def y(self) -> float:
        return self.img_coords.y

    @property
    def alpha(self) -> float:
        return self.sky_coords.ra

    @property
    def delta(self) -> float:
        return self.sky_coords.dec


class Catalog(tuple):
    """High-level interface to a SExtractor catalog."""

    @staticmethod
    def _find_column(contents: list[list[str]], parameter: str) -> int:
        """Return the 0-based index of a SExtractor parameter in the catalog."""
        for line in contents:
            if line[0].startswith("#"):
                # Header lines look like: "# 1 ALPHA_SKY ..."
                if len(line) >= 3:
                    param_name = line[2]
                    if param_name.upper() == parameter.upper():
                        return int(line[1]) - 1
        raise ValueError(f"parameter '{parameter}' not found")

    @staticmethod
    def flag_saturated(flag_value: int) -> bool:
        """Return True if FLAGS indicates at least one saturated pixel."""
        if not 0 <= flag_value <= 255:
            raise ValueError("flag value out of range [0, 255]")
        # Bit 2 set → saturated
        return (flag_value & (1 << 2)) != 0

    @classmethod
    def _load_stars(cls, path: Path):
        """Load a SExtractor catalog into memory and yield Star objects."""
        with open(path, "rt") as fd:
            contents = [line.split() for line in fd]

        get_index = functools.partial(cls._find_column, contents)
        x_index = get_index("X_IMAGE")
        y_index = get_index("Y_IMAGE")
        alpha_index = get_index("ALPHA_SKY")
        delta_index = get_index("DELTA_SKY")
        area_index = get_index("ISOAREAF_IMAGE")
        mag_index = get_index("MAG_AUTO")
        flux_index = get_index("FLUX_ISO")
        fluxerr_index = get_index("FLUXERR_ISO")
        try:
            fwhm_index = get_index("FWHM_IMAGE")
        except ValueError:
            fwhm_index = None

        try:
            flux_radius_index = get_index("FLUX_RADIUS")
        except ValueError:
            flux_radius_index = None
        flags_index = get_index("FLAGS")
        elong_index = get_index("ELONGATION")

        for line in contents:
            if line and not line[0].startswith("#"):  # ignore comments

                def get_param(idx: int, type_=float):
                    return type_(line[idx])

                x = get_param(x_index)
                y = get_param(y_index)
                alpha = get_param(alpha_index)
                delta = get_param(delta_index)
                area = get_param(area_index, type_=int)
                mag = get_param(mag_index)
                flux = get_param(flux_index)
                fluxerr = get_param(fluxerr_index)
                fwhm = None
                if fwhm_index is not None:
                    fwhm = get_param(fwhm_index)
                    if not math.isfinite(fwhm) or fwhm <= 0:
                        fwhm = None

                if fwhm is None:
                    if flux_radius_index is None:
                        fwhm = 0.0
                    else:
                        flux_radius = get_param(flux_radius_index)
                        fwhm = flux_radius * 2.0
                flags = get_param(flags_index, type_=int)
                elongation = get_param(elong_index)

                saturated = Catalog.flag_saturated(flags)
                snr = (flux / fluxerr) if fluxerr != 0 else 0.0
                yield Star(
                    x, y, alpha, delta, area, mag, saturated, snr, fwhm, elongation
                )

    def __new__(cls, path: str | Path):
        path = Path(path)
        stars = cls._load_stars(path)
        catalog = super(Catalog, cls).__new__(cls, stars)
        catalog._path = path  # type: ignore[attr-defined]
        return catalog

    @property
    def path(self) -> Path:
        """Read-only 'path' attribute."""
        return self._path  # type: ignore[attr-defined]

    @classmethod
    def from_sequence(cls, *stars):
        # Accept both from_sequence(*seq) and from_sequence(seq)
        if len(stars) == 1 and isinstance(stars[0], (list, tuple)):
            stars = tuple(stars[0])
        return super(Catalog, cls).__new__(cls, stars)

    def get_sky_coordinates(self) -> list[Coordinates]:
        """Return a list with celestial coordinates (Coordinates objects)."""
        return [star.sky_coords for star in self]


def sextractor_md5sum(options: dict[str, object] | None = None) -> str:
    """Return the MD5 hash of the SExtractor configuration (files + overrides)."""
    sex_files = (
        SEXTRACTOR_CONFIG,
        SEXTRACTOR_PARAMS,
        SEXTRACTOR_FILTER,
        SEXTRACTOR_STARNNW,
    )

    md5 = hashlib.md5()
    for path in sex_files:
        with open(path, "rt", encoding="utf-8") as fd:
            for line in fd:
                md5.update(line.encode("utf-8"))

    if options is not None:
        if not isinstance(options, dict):
            raise TypeError("'options' must be a dictionary")
        for key, value in sorted(options.items()):
            md5.update(str(key).encode("utf-8"))
            md5.update(str(value).encode("utf-8"))

    return md5.hexdigest()


def sextractor_version() -> tuple[int, int, int]:
    """Return the SExtractor version as a tuple like (2, 8, 6)."""
    # Expected string sample: "SExtractor version 2.8.6 (2009-04-09)"
    pattern = re.compile(r"\d+(?:\.\d+){2}")

    for executable in SEXTRACTOR_COMMANDS:
        if which(executable):
            break
    else:
        raise SExtractorNotInstalled()

    out = subprocess.run(
        [executable, "--version"], check=True, capture_output=True, text=True
    ).stdout
    m = pattern.search(out)
    if not m:
        raise RuntimeError(f"cannot parse version from output: {out!r}")
    return tuple(int(x) for x in m.group(0).split("."))


def sextractor(
    path: str | Path,
    ext: int = 0,
    options: dict[str, object] | None = None,
    stdout=None,
    stderr=None,
) -> str:
    """Run SExtractor on the image and return the path to the output catalog."""
    if not isinstance(ext, int):
        raise TypeError("'ext' must be an integer")

    for executable in SEXTRACTOR_COMMANDS:
        if which(executable):
            break
    else:
        raise SExtractorNotInstalled()

    if sextractor_version() < SEXTRACTOR_REQUIRED_VERSION:
        raise SExtractorUpgradeRequired()

    path = str(path)

    # Temporary catalog path
    root = Path(path).name
    catalog_fd, catalog_path = tempfile.mkstemp(prefix=f"{root}_", suffix=".cat")
    os.close(catalog_fd)

    # Validate config files exist and are readable
    for config_file in (
        SEXTRACTOR_CONFIG,
        SEXTRACTOR_PARAMS,
        SEXTRACTOR_FILTER,
        SEXTRACTOR_STARNNW,
    ):
        if not Path(config_file).exists():
            raise IOError(f"configuration file {config_file} not found")
        if not os.access(config_file, os.R_OK):
            raise IOError(f"configuration file {config_file} cannot be read")

    args = [
        executable,
        f"{path}[{ext}]",
        "-c",
        str(SEXTRACTOR_CONFIG),
        "-PARAMETERS_NAME",
        str(SEXTRACTOR_PARAMS),
        "-FILTER_NAME",
        str(SEXTRACTOR_FILTER),
        "-STARNNW_NAME",
        str(SEXTRACTOR_STARNNW),
        "-CATALOG_NAME",
        catalog_path,
    ]

    if options is not None:
        if not isinstance(options, dict):
            raise TypeError("'options' must be a dictionary")
        for key, value in options.items():
            args += [f"-{key}", str(value)]

    try:
        subprocess.check_call(args, stdout=stdout, stderr=stderr)
        return catalog_path
    except subprocess.CalledProcessError as e:
        try:
            os.unlink(catalog_path)
        except OSError:
            pass
        raise SExtractorError(e.returncode, e.cmd)
