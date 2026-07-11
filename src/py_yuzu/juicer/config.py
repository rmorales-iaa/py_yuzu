#!/usr/bin/env python3
from __future__ import annotations
from pathlib import Path

# Copyright (c) 2012 Victor Terron. All rights reserved.
# Institute of Astrophysics of Andalusia, IAA-CSIC
#
# This file is part of LEMON.
#
# LEMON is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
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

import configparser as ConfigParser
import os.path

CONFIG_FILENAME = ".juicerc"
CONFIG_PATH = (Path.home() / CONFIG_FILENAME).expanduser()

VIEW_SECTION = "view"
VIEW_SEXAGESIMAL = "sexagesimal"
VIEW_DECIMAL = "decimal"
PLOT_AIRMASSES = "airmasses"
PLOT_JULIAN = "julian_dates"
PLOT_MIN_SNR = "snr_threshold"

DEFAULT_VIEW_SEXAGESIMAL = True
DEFAULT_VIEW_DECIMAL = False
DEFAULT_PLOT_AIRMASSES = True
DEFAULT_PLOT_JULIAN = False
DEFAULT_PLOT_MIN_SNR = 100

# The color codes can use any of the following formats supported by matplotlib:
# abbreviations ('g'), full names ('green'), hexadecimal strings ('#008000') or
# a string encoding float on the 0-1 range ('0.75') for gray shades.
COLOR_SECTION = "colors"
DEFAULT_COLORS = dict(
    U="violet",
    B="blue",
    V="green",
    R="#ff4246",  # light red
    I="#e81818",  # dark red
    Z="cyan",
    Y="brown",
    J="yellow",
    H="pink",
    KS="orange",
    K="orange",
    L="0.75",  # light gray
    M="0.50",  # dark gray
)

# The options for how light curves are dumped to plain-text files
CURVEDUMP_SECTION = "curve-export"
DEFAULT_CURVEDUMP_OPTS = dict(
    dump_date_text=1,
    dump_date_julian=1,
    dump_date_seconds=1,
    dump_magnitude=1,
    dump_snr=1,
    dump_max_merr=1,
    dump_min_merr=1,
    dump_instrumental_magnitude=1,
    dump_instrumental_snr=1,
    decimal_places=8,
)


class Configuration(ConfigParser.ConfigParser):
    """Wrapper that auto-loads the configuration and can write defaults."""

    # Build a full default config file as text
    DEFAULT_CONFIG = (
        f"[{VIEW_SECTION}]\n"
        f"{VIEW_SEXAGESIMAL} = {int(DEFAULT_VIEW_SEXAGESIMAL)}\n"
        f"{VIEW_DECIMAL} = {int(DEFAULT_VIEW_DECIMAL)}\n"
        f"{PLOT_AIRMASSES} = {int(DEFAULT_PLOT_AIRMASSES)}\n"
        f"{PLOT_JULIAN} = {int(DEFAULT_PLOT_JULIAN)}\n"
        f"{PLOT_MIN_SNR} = {int(DEFAULT_PLOT_MIN_SNR)}\n"
        f"\n[{COLOR_SECTION}]\n"
        + "\n".join(f"{k} = {v}" for k, v in DEFAULT_COLORS.items())
        + f"\n\n[{CURVEDUMP_SECTION}]\n"
        + "\n".join(f"{k} = {v}" for k, v in DEFAULT_CURVEDUMP_OPTS.items())
        + "\n"
    )

    def __init__(self, path: str | os.PathLike, update: bool = True):
        """Parse a configuration file, creating it with defaults if missing."""
        super().__init__()

        path = Path(path).expanduser()
        # Ensure parent directory exists (usually HOME, but safe anyway)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

        if not path.exists():
            with open(path, "wt", encoding="utf-8") as fd:
                fd.write(self.DEFAULT_CONFIG)

        self.read([str(path)], encoding="utf-8")
        self.path = str(path)

    def color(self, letter: str) -> str:
        """Return the color code to be used for a photometric filter."""
        return self.get(COLOR_SECTION, letter.upper())

    def update(self) -> None:
        """Write the configuration back to disk."""
        with open(self.path, "wt", encoding="utf-8") as fd:
            self.write(fd)

    # SafeConfigParser is an old-style class (does not support properties)
    def get_minimum_snr(self) -> int:
        """Return the PLOT_MIN_SNR option in the VIEW_SECTION section."""
        return self.getint(VIEW_SECTION, PLOT_MIN_SNR)

    def set_minimum_snr(self, snr: int | float) -> None:
        """Set the value of the PLOT_MIN_SNR option in the VIEW_SECTION."""
        self.set(VIEW_SECTION, PLOT_MIN_SNR, str(int(snr)))

    def dumpint(self, option: str) -> int:
        """Coerce 'option' in the curves export section to an integer."""
        return self.getint(CURVEDUMP_SECTION, option)

    def dumpset(self, option: str, value: int | str) -> None:
        """Set 'option' to 'value' in the curves export section."""
        self.set(CURVEDUMP_SECTION, option, str(value))
