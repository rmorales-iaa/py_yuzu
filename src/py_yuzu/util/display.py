#!/usr/bin/env python3
from __future__ import annotations

import functools
import logging
import sys
import time
import traceback

# LEMON modules
import style


def show_progress(percentage):
    """Print a wget-like progress bar.

    Example:
      51%[===============================>                                 ]

    The bar (including percentage and square brackets) is 79 chars long,
    per PEP 8. It overwrites the current line without a trailing newline.
    `percentage` must be in [0, 100].
    """
    if not 0 <= percentage <= 100:
        raise ValueError("The value of 'percentage' must be in the range [0,100]")

    length = 79  # total line length
    occupied = 3 + len(style.prefix)  # "%" + "[" + "]" + style.prefix

    percent = str(int(percentage))
    available = length - occupied - len(percent)
    bar = int(available * int(percent) / 100.0)
    spaces = available - bar

    sys.stdout.write(
        "\r" + style.prefix + percent + "%[" + ("=" * bar) + ">" + (" " * spaces) + "]"
    )
    sys.stdout.flush()


def utctime(seconds=None, suffix=True):
    """UTC version of time.ctime().

    Convert seconds since the Unix epoch to a string like:
      'Sun Jun 20 23:21:05 1993 UTC'
    Fractions of a second are ignored. If `suffix` is False, omit ' UTC'.
    If `seconds` is None, use the current time.
    """
    utc_ctime = time.asctime(time.gmtime(seconds))
    if suffix:
        utc_ctime += " UTC"
    return utc_ctime


def print_exception_traceback(func):
    """Decorator that prints the traceback for any exception, then re-raises.

    Useful for functions run via multiprocessing, where child exceptions
    otherwise may not print a stack trace.
    """

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            traceback.print_exc()
            print()
            raise

    return wrapper


logger = logging.getLogger(__name__)
