#!/usr/bin/env python3
from __future__ import annotations

import logging
import argparse
import re
import textwrap
from typing import Iterable

# LEMON modules
import passband

logger = logging.getLogger(__name__)


# --- Help formatting that preserves blank lines in the description ---

class NewlinesFormatter(argparse.HelpFormatter):
    """Preserve paragraph breaks while still wrapping lines."""
    def _fill_text(self, text: str, width: int, indent: str) -> str:
        paras = [p.strip() for p in text.split("\n\n")]
        wrapped = [
            textwrap.fill(p, width,
                          initial_indent=indent,
                          subsequent_indent=indent)
            for p in paras if p
        ]
        return "\n\n".join(wrapped)


# --- argparse type for Passband ---

def passband_type(value: str) -> passband.Passband:
    try:
        return passband.Passband(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"invalid photometric filter {value!r}: {e}"
        )


def get_parser(description: str) -> argparse.ArgumentParser:
    """Return the ArgumentParser used by LEMON modules."""
    return argparse.ArgumentParser(
        description=description,
        add_help=False,                 # keep behavior consistent with original
        formatter_class=NewlinesFormatter,
    )


def _all_actions(parser: argparse.ArgumentParser) -> Iterable[argparse.Action]:
    yield from parser._actions
    for group in getattr(parser, "_action_groups", []):
        for act in getattr(group, "_group_actions", []):
            yield act


def clear_metavars(parser: argparse.ArgumentParser) -> None:
    """Set all option metavars to a single whitespace for compact help."""
    EMPTY = " "
    for action in _all_actions(parser):
        # Only options that actually take a value should get a metavar
        if action.option_strings and action.nargs not in (0, None):
            action.metavar = EMPTY
        elif action.option_strings and action.nargs is None and action.type:
            # single value options (store) without explicit nargs
            action.metavar = EMPTY


# --- Optional utility: parse a "--name[=value]" string into a dict ---

def parse_additional_option_string(s: str, out_dict: dict) -> None:
    """
    Parse a single option string like '--downsample=2' or '--verbose'
    and store into `out_dict` as { '--downsample': '2' } or { '--verbose': None }.
    """
    regexp = r"=?(?P<option>-+[\w-]+)\s*=?\s*(?P<value>\w+)?"
    match = re.match(regexp, s)
    if not match:
        raise ValueError(
            "cannot parse additional option. Use GNU/POSIX style like "
            "'-o', '-o value', '--name', or '--name=value'."
        )
    opt = match.group("option")
    val = match.group("value")
    out_dict[opt] = val
