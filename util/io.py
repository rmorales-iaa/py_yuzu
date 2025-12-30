#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import logging
import os
import shutil
import stat
import tempfile
from typing import Iterable, List

# Copyright (c) 2019 Victor Terron. All rights reserved.
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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import style  # LEMON module

logger = logging.getLogger(__name__)


def determine_output_dir(
    output_directory: str | os.PathLike[str] | None,
    dir_suffix: str | None = None,
    quiet: bool = False,
) -> str:
    """Ensure that the specified directory exists and is writable.

    If None is given, create a temporary directory (0700). If the path does not
    exist, create it. Always return the directory path used.

    Args:
        output_directory: The desired output directory, or None for a temp dir.
        dir_suffix: Suffix for the temp directory name (when output_directory is None).
        quiet: If True, do not print user-facing messages.

    Returns:
        The absolute path to the output directory (string).
    """
    if not output_directory:
        if not quiet:
            print(f"{style.prefix}No output directory was specified.")
        temporary_directory = tempfile.mkdtemp(suffix=dir_suffix or "")
        if not quiet:
            print(
                f"{style.prefix}Images will be saved to temporary directory "
                f"{temporary_directory}"
            )
        return temporary_directory

    # Path was specified: ensure it exists, is a directory, and is writable.
    output_directory = str(output_directory)
    p = Path(output_directory)

    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise IOError(
                f"The output directory '{output_directory}' could not be created. "
                "Is the directory writable?"
            )
        if not quiet:
            print(
                f"{style.prefix}The output directory '{output_directory}' did not "
                f"exist, so it had to be created."
            )
    else:
        if not p.is_dir():
            raise IOError(f"{output_directory} is not a directory")
        if not os.access(output_directory, os.W_OK):
            raise IOError(f"The output directory '{output_directory}' is not writable.")
        if not quiet:
            print(f"{style.prefix}Images will be saved to directory '{output_directory}'")

    return output_directory


def owner_writable(path: str | os.PathLike[str], add: bool) -> None:
    """Add or remove write permission for the file's owner (chmod u+/-w)."""
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if add:
        mode |= stat.S_IWUSR           # add write bit
    else:
        mode &= ~stat.S_IWUSR          # remove write bit (was XOR before; this is correct)
    os.chmod(path, mode)


def which(*names: str) -> List[Path]:
    """Search PATH for executable files with the given names.

    Returns a list of full paths (as Path objects) to executables that would be
    run for those names in the current environment. If none are found, returns
    an empty list.

    Note: On Linux this ignores PATHEXT (Windows behavior).
    """
    result: list[Path] = []
    path_dirs: Iterable[str] = os.environ.get("PATH", "").split(os.pathsep)

    for directory in path_dirs:
        if not directory:
            continue
        d = Path(directory)
        for name in names:
            candidate = d / name
            try:
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    result.append(candidate)
            except OSError:
                # Directory might be unreadable; skip silently
                continue
    return result


def clean_tmp_files(*paths: str | os.PathLike[str]) -> None:
    """Try to remove multiple temporary files and directories.

    Files -> os.unlink; directories -> shutil.rmtree. Errors do not raise,
    they're logged at DEBUG level.
    """
    for path in paths:
        p = Path(path)

        if p.is_dir():
            logging.debug("Cleaning up temporary directory '%s'", p)

            error_count = [0]

            def log_error(function, pth, excinfo):
                # Mutable counter pattern
                error_count[0] += 1
                logging.debug("%s: error deleting '%s' (%s)", function, pth, excinfo[1])

            try:
                shutil.rmtree(p, ignore_errors=False, onerror=log_error)
            finally:
                msg = "Temporary directory '%s' deleted"
                if error_count[0] > 0:
                    msg += " (but there were failed removals)"
                logging.debug(msg, p)

        else:
            logging.debug("Cleaning up temporary file '%s'", p)
            try:
                os.unlink(p)
            except OSError as e:
                logging.debug("Cannot delete '%s' (%s)", p, e)
            else:
                logging.debug("Temporary file '%s' removed", p)
