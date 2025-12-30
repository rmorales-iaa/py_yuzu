#!/usr/bin/env python3
# Copyright (c) 2013 Victor Terron. All rights reserved.
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

"""Use Python import hooks to enforce a minimum version of those modules
defined in the requirements.txt file. With this solution we define the hooks
once here and ensure required versions regardless of where they are imported.
"""

from __future__ import annotations

import logging
import re
import sys
import importlib
import importlib.util
import importlib.abc
import importlib.machinery
from typing import Callable, Tuple

# LEMON module
import astromatic

logger = logging.getLogger(__name__)


def version_to_str(version: Tuple[int, ...]) -> str:
    """From (0, 2, 4) to '0.2.4'."""
    return ".".join(str(x) for x in version)


def str_to_version(version: str) -> Tuple[int, ...]:
    """From '0.2.4' to (0, 2, 4)."""
    return tuple(int(x) for x in version.split("."))


class _RequireModuleVersionLoader(importlib.abc.Loader):
    """Loader that delegates to the real loader and then enforces a min version."""

    def __init__(
        self,
        fullname: str,
        min_version: Tuple[int, ...],
        vfunc: Callable[[object], Tuple[int, ...]],
        real_loader: importlib.abc.Loader,
    ) -> None:
        self.fullname = fullname
        self.min_version = min_version
        self.vfunc = vfunc
        self.real_loader = real_loader

    def create_module(self, spec):
        if hasattr(self.real_loader, "create_module"):
            return self.real_loader.create_module(spec)  # type: ignore[attr-defined]
        return None  # use default module creation

    def exec_module(self, module) -> None:
        # Execute the real module
        self.real_loader.exec_module(module)  # type: ignore[attr-defined]
        # Then verify version
        version = self.vfunc(module)
        if version < self.min_version:
            msg = "%s >= %s is required, found %s"
            args = (
                self.fullname,
                version_to_str(self.min_version),
                version_to_str(version),
            )
            raise ImportError(msg % args)


class RequireModuleVersionHook(importlib.abc.MetaPathFinder):
    """Meta path finder that wraps the real loader to check versions."""

    def __init__(
        self,
        fullname: str,
        min_version: Tuple[int, ...],
        vfunc: Callable[[object], Tuple[int, ...]],
    ) -> None:
        self.fullname = fullname
        self.min_version = min_version
        self.vfunc = vfunc

    def find_spec(self, fullname, path=None, target=None):
        if fullname != self.fullname:
            return None
        # Ask the default PathFinder for the real spec
        real_spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if real_spec is None or real_spec.loader is None:
            return None
        # Wrap the real loader so we can enforce the version after import
        real_loader = real_spec.loader
        real_spec.loader = _RequireModuleVersionLoader(
            fullname=self.fullname,
            min_version=self.min_version,
            vfunc=self.vfunc,
            real_loader=real_loader,
        )
        return real_spec


def get__version__(module) -> Tuple[int, ...]:
    """Return module.__version__ as a tuple of integers."""
    version = getattr(module, "__version__", None)
    if version is None:
        raise Exception(f"module {module!r} has no __version__ attribute")
    # Extract '2.1.1' from '2.1.1-r1785' or '3.2' from '3.2.dev'
    regexp = r"\d+(?:\.\d+)+"
    match = re.match(regexp, str(version))
    if match is None:
        raise Exception(f"cannot extract version from '{version}' ({module})")
    return str_to_version(match.group(0))


# For each module whose minimum version has been defined in requirements.txt,
# create an import hook and add it to sys.meta_path, which is searched before
# any implicit default finders or sys.path.
for module_name, min_ver in [
    ("numpy", (1, 7, 1)),
    ("aplpy", (0, 9, 9)),
    ("scipy", (0, 12, 0)),
    ("matplotlib", (1, 2, 1)),
    ("mock", (1, 0, 1)),
    ("pyfits", (3, 1, 2)),
    ("pyraf", (2, 1, 1)),
    ("uncertainties", (2, 4, 1)),
]:
    sys.meta_path.append(RequireModuleVersionHook(module_name, min_ver, get__version__))

# If the minimum version is not met, raise SExtractorUpgradeRequired ASAP.
if astromatic.sextractor_version() < astromatic.SEXTRACTOR_REQUIRED_VERSION:
    raise astromatic.SExtractorUpgradeRequired()
