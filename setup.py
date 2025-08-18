#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path

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
Just a shadow of what the actual setup.py will look like, this simple
script, when run, creates the IRAF login script (login.cl file) and the user
parameters directory (uparm) in the directory where LEMON is installed. This
directory is automatically detected as it is assumed to be the same in which
this file is located, so it does not depend on where it is executed from.
"""

import errno
import os
import shutil
import subprocess
import sys

logger = logging.getLogger(__name__)

MKIRAF_BIN = "mkiraf"
TERM_TYPE = "xgterm"
LOGIN_FILE = "login.cl"
UPARM_DIR = "uparm"
PYRAF_CACHE = "pyraf"

# The LEMON configuration file
CONFIG_FILENAME = "~/.lemonrc"
CONFIG_PATH = Path(CONFIG_FILENAME).expanduser()


def mkiraf(path: Path) -> None:
    """Create the IRAF login script and the uparm directory at 'path'."""
    # Ensure mkiraf exists in PATH
    if shutil.which(MKIRAF_BIN) is None:
        raise FileNotFoundError(
            f"'{MKIRAF_BIN}' not found in PATH. Please install IRAF tools or add it to PATH."
        )

    os.chdir(path)

    # Avoid having to answer the "Initialize uparm? (y|n):" question
    if Path(UPARM_DIR).exists():
        shutil.rmtree(UPARM_DIR)

    # Run mkiraf and provide terminal type via stdin
    p = subprocess.Popen(
        [MKIRAF_BIN],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = p.communicate(input=TERM_TYPE + "\n")

    if p.returncode:
        msg = f"Execution of {MKIRAF_BIN} failed (code {p.returncode})."
        if stderr:
            msg += f" Stderr: {stderr.strip()}"
        raise subprocess.CalledProcessError(p.returncode, [MKIRAF_BIN], stdout, stderr)

    # Verify terminal type in the generated login.cl
    with open(LOGIN_FILE, "rt", encoding="utf-8", errors="replace") as fd:
        for line in fd:
            parts = line.split()
            if len(parts) == 2 and parts[0] == "stty":
                if parts[1] == TERM_TYPE:
                    break
                raise ValueError("Terminal type wasn't set correctly in login.cl")
        else:
            raise ValueError(f"Terminal type not defined in {LOGIN_FILE}")


def mkdir_p(path: Path | str) -> None:
    """Create a directory like `mkdir -p` (no error if it already exists)."""
    try:
        os.mkdir(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and Path(path).is_dir():
            return
        raise


if __name__ == "__main__":
    lemon_path = Path(__file__).resolve().parent

    print(f"Setting up IRAF's {LOGIN_FILE} in {lemon_path} ...", end=" ")
    mkiraf(lemon_path)
    print("done.")

    print("Creating pyraf/ directory for cache...", end=" ")
    pyraf_cache_path = lemon_path / PYRAF_CACHE
    mkdir_p(pyraf_cache_path)
    print("done.")
