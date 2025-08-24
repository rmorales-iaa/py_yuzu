#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Astrometry wrapper for Astrometry.net (solve-field)

- Pure Python 3 (no StandardError, bytes-safe)
- Verifies the external command is present and executable
- Safer .solved file check (reads a single byte and compares to b"\x01")
- Parallel execution using --cores (defaults.ncores)
- Same CLI/options and console messages as the original
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import warnings
from typing import Optional

# LEMON modules (as in the original)
import util
import customparser
import defaults
import fitsimage
import keywords
import style

description = """
This module uses a local build of the Astrometry.net software in order to
compute the astrometric solution of the input FITS files, saving the new files,
containing the WCS header, to the output directory. It is a thin wrapper over
Astrometry.net's 'solve-field' CLI (must be in PATH, together with the indexes).
"""

ASTROMETRY_COMMAND = "solve-field"
ASTROMETRY_REQUIRED_VERSION = (0, 68)

# A small, process-shared queue for progress updates
queue = util.Queue()


# -------- Exceptions --------
class AstrometryNetNotInstalled(Exception):
    """Raised if Astrometry.net is not installed (or not executable)."""
    pass


class AstrometryNetError(subprocess.CalledProcessError):
    """Raised if the execution of Astrometry.net fails (non-zero exit)."""
    def __str__(self):
        try:
            return "Command: " + " ".join(self.cmd)
        except Exception:
            return super().__str__()


class AstrometryNetUnsolvedField(subprocess.CalledProcessError):
    """Raised if Astrometry.net could not solve the field."""
    def __init__(self, path: str):
        self.path = path

    def __str__(self):
        return f"{self.path}: could not solve field"


class AstrometryNetTimeoutExpired(AstrometryNetUnsolvedField):
    """Raised if the Astrometry.net timeout was reached."""
    def __init__(self, path: str, timeout: Optional[int]):
        super().__init__(path)
        self.timeout = timeout

    def __str__(self):
        return f"{self.path}: could not solve field in less than {self.timeout} seconds"


class AstrometryNetUpgradeRequired(Exception):
    """Raised if a too-old version of Astrometry.net is installed."""
    pass


# -------- Helpers --------
def _which_executable(name: str) -> Optional[str]:
    """Return an absolute path to an executable in PATH (file and executable)."""
    path = util.which(name)
    if not path:
        return None
    # Some environments might return a directory; ensure it's a file you can exec
    try:
        if os.path.isdir(path):
            return None
        if not os.access(path, os.X_OK):
            return None
        return path
    except Exception:
        return None


def astrometry_net_version() -> tuple[int, int]:
    """Return the Astrometry.net version as a tuple, e.g., (0, 78)."""
    # Example: "Revision 0.78, date Mon_Apr_22_12:25:30_2019_-0400."
    PATTERN = r"^Revision (\d\.\d{1,2}), date.*"
    exe = _which_executable(ASTROMETRY_COMMAND)
    if not exe:
        raise AstrometryNetNotInstalled(f"'{ASTROMETRY_COMMAND}' not found in the current environment")
    out = subprocess.check_output([exe, "--help"]).decode("utf-8", "replace")
    m = re.search(PATTERN, out, re.MULTILINE)
    if not m:
        # Be tolerant: if no banner match, assume new enough and return a big version
        return (999, 0)
    version = m.group(1)
    return tuple(int(x) for x in version.split("."))


def astrometry_net(path, *, ra=None, dec=None, radius=1, verbosity=0, timeout=None, options=None) -> str:
    """Run Astrometry.net on a FITS image and return the path to the solved FITS.

    Parameters mirror the original LEMON wrapper.
    """
    exe = _which_executable(ASTROMETRY_COMMAND)
    if not exe:
        raise AstrometryNetNotInstalled(f"'{ASTROMETRY_COMMAND}' not found in the current environment")

    if astrometry_net_version() < ASTROMETRY_REQUIRED_VERSION:
        req = ".".join(str(x) for x in ASTROMETRY_REQUIRED_VERSION)
        raise AstrometryNetUpgradeRequired(f"Astrometry.net version {req} or newer is needed")

    basename = os.path.basename(path)
    root, ext = os.path.splitext(basename)

    # Place all output files in a dedicated temp directory
    output_dir = tempfile.mkdtemp(prefix=f"{root}_", suffix="_astrometry.net")

    # Temporary path for the solved FITS target
    with tempfile.NamedTemporaryFile(prefix=f"{root}_astrometry_", suffix=ext, delete=True) as fd:
        output_path = fd.name

    solved_file = os.path.join(output_dir, root + ".solved")

    # Base arguments
    args = [
        exe,
        path,
        "--dir", output_dir,
        "--no-plots",
        "--new-fits", output_path,
        "--overwrite",
    ]

    # Optional constraints (positional guess)
    if ra is not None:
        args += ["--ra", f"{float(ra):.8f}"]
    if dec is not None:
        args += ["--dec", f"{float(dec):.8f}"]
    if radius is not None:
        args += ["--radius", f"{float(radius):.6f}"]

    # Verbosity: 0 => silence; >1 => pass (verbosity-1) "-v"
    if verbosity > 1:
        args.append("-" + "v" * (int(verbosity) - 1))

    # Additional 'solve-field' options
    if options:
        for opt, value in options.items():
            opt = str(opt)
            if value is None:
                args.append(opt)
            else:
                args += [opt, str(value)]

    null_fd = open(os.devnull, "w")
    try:
        kw = dict(timeout=timeout)
        if not verbosity:
            kw["stdout"] = kw["stderr"] = null_fd

        subprocess.check_call(args, **kw)

        # A .solved file must exist and contain a single 1 byte
        try:
            with open(solved_file, "rb") as sf:
                b = sf.read(1)
            if b != b"\x01":
                raise AstrometryNetUnsolvedField(path)
        except FileNotFoundError:
            raise AstrometryNetUnsolvedField(path)

        return output_path

    except subprocess.TimeoutExpired:
        raise AstrometryNetTimeoutExpired(path, timeout)
    except subprocess.CalledProcessError as e:
        raise AstrometryNetError(e.returncode, e.cmd)
    finally:
        null_fd.close()
        util.clean_tmp_files(output_dir)


@util.print_exception_traceback
def parallel_astrometry(args):
    """Function used by multiprocessing.Pool.map_async()."""
    path, output_dir, options = args
    img = fitsimage.FITSImage(path)

    # Output filename: basename + suffix + original extension
    root, ext = os.path.splitext(os.path.basename(path))
    output_filename = root + options.suffix + ext
    dest_path = os.path.join(output_dir, output_filename)

    # Coordinate hints unless --blind
    if options.blind:
        ra = dec = None
        logging.debug("%s: solving blindly (--blind)", img.path)
    else:
        try:
            ra = img.ra(options.rak)
            dec = img.dec(options.deck)
        except (ValueError, KeyError) as e:
            logging.debug("%s: %s", img.path, str(e))
            ra = dec = None
            logging.debug("%s: falling back to blind solution", img.path)

    try:
        solved_tmp = astrometry_net(
            img.path,
            ra=ra, dec=dec, radius=options.radius,
            verbosity=options.verbose,
            timeout=options.timeout,
            options=options.solve_field_options,
        )
    except AstrometryNetUnsolvedField as e:
        msg = "%s exceeded the timeout limit. Ignored." if isinstance(e, AstrometryNetTimeoutExpired) \
              else "%s did not solve. Ignored."
        warnings.warn(msg % img.path, RuntimeWarning)
        queue.put(None)
        logging.debug("%s: None put into global queue", path)
        return

    # Move result into the requested output directory/filename
    try:
        shutil.move(solved_tmp, dest_path)
        logging.debug("%s: solved image saved to %s", path, dest_path)
    except (IOError, OSError) as e:
        logging.debug("%s: can't store solved image (%s)", path, str(e))
        util.clean_tmp_files(solved_tmp)
        queue.put(None)
        return

    # Update HISTORY
    output_img = fitsimage.FITSImage(dest_path)
    output_img.add_history(f"Astrometry done via LEMON on {util.utctime()}")
    output_img.add_history("[Astrometry] WCS solution found by Astrometry.net")
    output_img.add_history(f"[Astrometry] Original image: {img.path}")

    queue.put(output_img.path)
    logging.debug("%s: astrometry result (%r) put into global queue", path, output_img.path)


# -------- CLI --------
parser = customparser.get_parser(description)
parser.usage = "%prog [OPTION]... INPUT_IMGS... OUTPUT_DIR"
parser.add_option(
    "--radius", action="store", type="float", dest="radius", default=1.0,
    help=("only search in indexes within this number of degrees of the field center, "
          "whose coordinates are read from the FITS header (see --rak and --deck). "
          "If they cannot be read or are invalid, the image is solved blindly [default: %default]"),
)
parser.add_option(
    "--blind", action="store_true", dest="blind",
    help=("ignore --radius, --rak and --deck and solve the images blindly")
)
parser.add_option(
    "--timeout", action="store", type="int", dest="timeout", default=600,
    help=("maximum seconds to attempt a solution; exceeding this raises timeout "
          "and the image is ignored [default: %default]"),
)
parser.add_option(
    "--suffix", action="store", type="str", dest="suffix", default="a",
    help="string appended to output filenames before the extension [default: %default]",
)
parser.add_option(
    "--cores", action="store", type="int", dest="ncores", default=defaults.ncores,
    help=defaults.desc["ncores"],
)
parser.add_option(
    "-o", action="callback", type="str", dest="solve_field_options", default={},
    callback=customparser.additional_options_callback,
    help=("additional raw options to pass to solve-field; may be repeated, e.g. "
          "-o=--invert  -o '--downsample 2'  -o --sigma=3"),
)
parser.add_option(
    "-v", "--verbose", action="count", dest="verbose", default=defaults.verbosity,
    help=(defaults.desc["verbosity"] + " The first -v enables seeing Astrometry.net stdout/stderr; "
          "further -v's are passed on to solve-field."),
)
key_group = optparse.OptionGroup(parser, "FITS Keywords", keywords.group_description)
key_group.add_option("--rak", action="store", type="str", dest="rak",   default=keywords.rak,  help=keywords.desc["rak"])
key_group.add_option("--deck", action="store", type="str", dest="deck", default=keywords.deck, help=keywords.desc["deck"])
parser.add_option_group(key_group)
customparser.clear_metavars(parser)


def main(arguments=None) -> int:
    if arguments is None:
        arguments = sys.argv[1:]
    (options, args) = parser.parse_args(args=arguments)

    # Logging level from -v count
    lvl = logging.WARNING if options.verbose == 0 else (logging.INFO if options.verbose == 1 else logging.DEBUG)
    logging.basicConfig(format=style.LOG_FORMAT, level=lvl)

    # Need at least one input path and an output directory
    if len(args) < 2:
        parser.print_help()
        return 2

    input_paths = args[:-1]
    output_dir = args[-1]

    if options.radius <= 0:
        print(f"{style.prefix}Error: --radius must a positive number of degrees")
        sys.exit(style.error_exit_message)

    util.determine_output_dir(output_dir)

    print(f"{style.prefix}Using a local build of Astrometry.net.")
    print(f"{style.prefix}Doing astrometry on the {len(input_paths)} paths given as input.")

    # Parallel solve
    pool = multiprocessing.Pool(max(1, int(options.ncores)))
    result = pool.map_async(parallel_astrometry, ((p, output_dir, options) for p in input_paths))

    # Progress loop
    while not result.ready():
        time.sleep(1.0)
        util.show_progress(queue.qsize() / max(1, len(input_paths)) * 100.0)
        if lvl < logging.WARNING:
            print()

    # Raise worker exceptions if any
    result.get()
    util.show_progress(100.0)
    print()

    # Results were only needed for progress; clear now
    queue.clear()
    print(f"{style.prefix}You're done ^_^")
    return 0


if __name__ == "__main__":
    sys.exit(main())
