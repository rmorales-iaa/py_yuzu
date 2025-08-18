#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path

"""
The class Passband encapsulates a photometric filter. The supported systems
are Johnson, Cousins, Gunn, SDSS, 2MASS, Strömgren and H-alpha, but photometric
letters may also be given without a system (e.g., 'V'). The class normalizes
filter naming and provides ordering by spectral position.

User-defined (custom) filters are supported via CONFIG_PATH in the
[custom_filters] section. See the Passband class docs for details.
"""

import configparser
import itertools
import random
import re
import string
import functools

# LEMON module
from setup import CONFIG_PATH

JOHNSON = "Johnson"
COUSINS = "Cousins"
HARRIS = "Harris"
GUNN = "Gunn"
SDSS = "SDSS"
TWOMASS = "2MASS"
STROMGREN = "Strömgren"
HALPHA = "Halpha"
UNKNOWN = "Unknown"
CUSTOM = "Custom"

CUSTOM_SECTION = "custom_filters"


def load_custom_filters(path: str | Path = CONFIG_PATH):
    """Yield (name, description) for user custom photometric filters."""
    parser = configparser.ConfigParser()
    parser.optionxform = str  # preserve case
    if Path(path).exists():
        parser.read([str(path)])
    if parser.has_section(CUSTOM_SECTION):
        for item in parser.items(CUSTOM_SECTION):
            yield item


# Regexes to recognize the photometric system from a filter name
REGEXPS = {
    JOHNSON: r"Johnson|John",
    HARRIS: r"Harris|Har",
    COUSINS: r"Cousins?|Cous?",
    GUNN: r"Gunn|Gun",
    SDSS: r"SDSS|'|prime|Sloan",
    TWOMASS: r"2MASS|2M",
    STROMGREN: r"Strömgren|Stromgren|Stroemgren|Stro",
    HALPHA: r"H(a(lpha)?)?\d{4}",
}

# Map each custom filter to its description.
CUSTOM_FILTERS = dict(load_custom_filters())


class NonRecognizedPassband(ValueError):
    """Raised when the photometric filter cannot be identified."""

    ERROR_NOTE = (
        "If this is a legitimate filter name, and you think LEMON should "
        "recognize it, please open an issue at "
        "http://github.com/vterron/lemon/issues. "
        f"In the meantime, you can define your own filters in {CONFIG_PATH} "
        f"under the [{CUSTOM_SECTION}] section. For an example, see "
        "https://github.com/vterron/lemon/issues/14#issuecomment-43504285"
    )

    def __init__(self, name, path: str | None = None, keyword: str | None = None):
        self.name = name
        self.path = path
        self.keyword = keyword

    def __str__(self):
        msg = f"cannot identify the photometric system of filter '{self.name}'"
        details = []
        if self.path:
            details.append(f"FITS image = '{self.path}'")
        if self.keyword:
            details.append(f"keyword = '{self.keyword}'")
        if details:
            msg += f" ({', '.join(details)}). "
        else:
            msg += ". "
        return msg + self.ERROR_NOTE


class InvalidPassbandLetter(NonRecognizedPassband):
    """Raised if the letter of the filter does not belong to the system."""

    def __init__(self, name, system):
        self.name = name
        self.system = system

    def __str__(self):
        return f"'{self.name}' is not a letter of the {self.system} photometric system. " + self.ERROR_NOTE


@functools.total_ordering
class Passband:
    """Encapsulates a passband (filter) of a photometric system."""

    SYSTEM_LETTERS = {
        JOHNSON: tuple("UBVRIJHKLMN"),
        COUSINS: tuple("VRI"),
        HARRIS: tuple("UBVRI"),
        GUNN: tuple("UVGR"),
        SDSS: tuple("UGRIZ"),
        TWOMASS: ("J", "H", "KS"),
        STROMGREN: ("U", "V", "B", "Y", "NARROW", "N", "WIDE", "W"),
    }

    ALL_SYSTEMS = set(SYSTEM_LETTERS) | {HALPHA, CUSTOM}
    ALL_LETTERS = set(itertools.chain(*SYSTEM_LETTERS.values()))

    # Spectral order of letters, regardless of system
    LETTERS_ORDER = [
        "U",
        "B",
        "NARROW",
        "WIDE",
        "V",
        "G",
        "R",
        "I",
        "Z",
        "Y",
        "J",
        "H",
        "KS",
        "K",
        "L",
        "M",
        "N",
    ]

    @staticmethod
    def _identify_system(name: str) -> str:
        """Return the photometric system to which a filter belongs."""
        for system, regexp in REGEXPS.items():
            if re.search(regexp, name, re.IGNORECASE):
                return system
        return UNKNOWN

    @classmethod
    def _parse_halpha_filter(cls, name: str) -> str | None:
        """Extract the wavelength from a H-alpha filter name."""
        regexp = r".*H(a(lpha)?)?(?P<wavelength>\d{4})(?P<bandwidth>/\d{2})?.*"
        match = re.match(regexp, name, re.IGNORECASE)
        if match is not None:
            return match.group("wavelength")
        return None

    @classmethod
    def _parse_name(cls, name: str, system: str) -> str:
        """Extract the letter from the name of a filter of a known system."""
        if system == HALPHA:
            raise ValueError(
                "Passband._parse_name() does not support H-alpha; "
                "use Passband._parse_halpha_filter()"
            )

        def fix_stromgren_letter(txt: str) -> str:
            # Normalize Strömgren HB-narrow / HB-wide conventions
            txt = re.sub(r"H[\-\s]*B(ETA)?", "", txt.upper())
            if txt == "N":
                return "NARROW"
            if txt == "W":
                return "WIDE"
            return txt

        # Remove the system token(s) from the name (case-insensitive)
        letter = re.sub(REGEXPS[system], "", name, flags=re.IGNORECASE).upper().strip()

        if system == STROMGREN:
            letter = fix_stromgren_letter(letter)

        # There should only be one token
        if len(letter.split()) != 1:
            raise NonRecognizedPassband(name)

        # Validate letter belongs to system
        if letter not in cls.SYSTEM_LETTERS[system]:
            all_letters = set(itertools.chain(cls.ALL_LETTERS, string.ascii_uppercase))
            if letter in all_letters:
                raise InvalidPassbandLetter(letter, system)
            raise NonRecognizedPassband(name)

        return letter

    def __init__custom(self, filter_name: str) -> bool:
        """Initialize from custom filters; return True if matched."""
        for name, description in CUSTOM_FILTERS.items():
            pattern = r"^(?:%s|%s)$" % (re.escape(name), re.escape(description))
            if re.match(pattern, filter_name, re.IGNORECASE):
                self.letter = description  # store description in 'letter'
                self.system = CUSTOM
                return True
        return False

    def __init__(self, filter_name: str):
        # Custom filters first
        if self.__init__custom(filter_name):
            return

        # E.g., from "_Johnson_(V)_" to "JohnsonV"
        name = re.sub(r"[\s\-_\(\)]", "", filter_name)

        system = self._identify_system(name)

        if system == UNKNOWN:
            letter = name.strip().upper()
            if letter not in self.ALL_LETTERS:
                raise NonRecognizedPassband(filter_name)
        elif system == HALPHA:
            letter = self._parse_halpha_filter(name)
            if letter is None:
                raise NonRecognizedPassband(filter_name)
        else:
            letter = self._parse_name(name, system)

        self.system = system
        self.letter = letter

    @classmethod
    def all(cls):
        """Return a Passband for each known (system, letter) plus customs."""
        pfilters: list[str] = []
        for system, letters in cls.SYSTEM_LETTERS.items():
            for letter in letters:
                # Avoid duplicates: 'N'/'W' are aliases for 'NARROW'/'WIDE'
                if system == STROMGREN and letter in ["N", "W"]:
                    continue
                pfilters.append(f"{system} {letter}")
        for name in CUSTOM_FILTERS.keys():
            pfilters.append(name)
        return [Passband(x) for x in pfilters]

    def __str__(self) -> str:
        """Human-friendly string (e.g., 'Johnson V', 'SDSS g'' or 'Ha 6563')."""
        system = self.system
        letter = self.letter

        if letter == "KS":
            letter = "Ks"

        if system in (CUSTOM, UNKNOWN):
            return letter

        if system in (GUNN, SDSS):
            letter = letter.lower()

        if system == STROMGREN:
            system = "Stromgren"
            if letter in ("NARROW", "WIDE"):
                letter = "HB " + letter.lower()
            else:
                letter = letter.lower()
        elif system == SDSS:
            letter = f"{letter}'"
        elif system == HALPHA:
            system = "Ha"

        return f"{system} {letter}"

    def __repr__(self) -> str:
        return f'{self.__class__.__name__}("{self}")'

    def __hash__(self) -> int:
        return hash((self.system, self.letter))

    def __eq__(self, other) -> bool:
        if not isinstance(other, Passband):
            return NotImplemented
        return (self.system, self.letter) == (other.system, other.letter)

    def _sort_key(self):
        """Build a tuple that encodes the ordering rules."""
        # custom (smallest) -> normal -> H-alpha (largest)
        if self.system == CUSTOM:
            return (0, None, str(self.letter).lower(), None)
        if self.system == HALPHA:
            # letter is wavelength string like "6563"
            return (2, None, int(self.letter), None)
        # normal systems: by spectral order of letter, then system name
        idx = self.LETTERS_ORDER.index(self.letter)
        return (1, idx, str(self.system).lower(), None)

    def __lt__(self, other) -> bool:
        if not isinstance(other, Passband):
            return NotImplemented
        return self._sort_key() < other._sort_key()

    @classmethod
    def random(cls) -> Passband:
        """Return a random Passband object."""
        MIN_WAVELENGTH = 6000
        MAX_WAVELENGTH = 7000

        keys = list(cls.SYSTEM_LETTERS.keys()) + [HALPHA]
        if CUSTOM_FILTERS:
            keys += [CUSTOM]

        system = random.choice(keys)
        if system == CUSTOM:
            name = random.choice(list(CUSTOM_FILTERS.keys()))
            return cls(name)
        if system == HALPHA:
            wavelength = random.randrange(MIN_WAVELENGTH, MAX_WAVELENGTH)
            return cls(f"{system} {wavelength:04d}")
        letter = random.choice(list(cls.SYSTEM_LETTERS[system]))
        return cls(f"{system} {letter}")

    def different(self) -> Passband:
        """Return a random Passband object different than this one."""
        while True:
            p = self.random()
            if p != self:
                return p


logger = logging.getLogger(__name__)
