#!/usr/bin/env python3
from __future__ import annotations

import difflib
import importlib
import logging
import os
import sys
import glob
from pathlib import Path
from typing import List

# Enforce minimum versions via import hooks in sys.meta_path
import check_versions  # noqa: F401

try:
    from conf_manager.conf_manager import cfg
    cfg.load()
except Exception:
    pass
from conf_manager.conf_manager import cfg

API_QUERY_TIMEOUT = 2  # seconds

LEMON_COMMANDS = [
    "annuli",
    "astrometry",
    "diffphot",
    "export",
    "import",
    "juicer",
    "mosaic",
    "offsets",
    "photometry",
    "seeing",
]


def show_help(name: str) -> None:
    """Help message, listing all commands, that looks like Git's."""
    print(f"usage: {name} [--help] [--version] [--update] COMMAND [ARGS]\n")
    print("The essential commands are:")
    print("   astrometry   Calibrate the images astrometrically")
    print("   mosaic       Assemble the images into a mosaic")
    print("   photometry   Perform aperture photometry")
    print("   diffphot     Generate light curves")
    print("   juicer       LEMONdB browser and variability analyzer")
    print("   export       Print the light curve of an object\n")
    print("The auxiliary, not-always-necessary commands are:")
    print("   import       Group the images of an observing campaign")
    print("   seeing       Discard images with bad seeing or elongated")
    print("   annuli       Find optimal parameters for photometry\n")
    print(f"See '{name} COMMAND' for more information on a specific command.")


def _expand_arg(arg: str) -> list[str]:
    """Expand env vars, ~, and globs. If a glob has no matches, keep literal."""
    expanded = os.path.expandvars(os.path.expanduser(arg))
    if glob.has_magic(expanded):
        matches = sorted(glob.glob(expanded))
        return matches if matches else [expanded]
    return [expanded]


def _expand_args(argv: List[str]) -> List[str]:
    out: List[str] = []
    for a in argv:
        out.extend(_expand_arg(a))
    return out


def main(argv: List[str]) -> None:
    # Normalize/expand variables and globs first so IDEs (which don?t glob) work
    argv = _expand_args(argv)

    name = Path(sys.argv[0]).name

    if len(argv) == 0 or "--help" in argv:
        show_help(name)
        sys.exit(0)

    command, *args = argv

    if command not in LEMON_COMMANDS:
        print(f"{name}: '{command}' is not a lemon command. See 'lemon --help'.")
        # Show suggestions, if any
        matches = difflib.get_close_matches(command, LEMON_COMMANDS)
        if matches:
            print("Did you mean", end=" ")
            print("this?" if len(matches) == 1 else "one of these?")
            for match in matches:
                print(" " * 8 + match)
        sys.exit(1)

    if command == "juicer":
        # Local import to avoid heavy deps on startup
        import juicer.main  # type: ignore
        kwargs = {}
        if args:
            # args are already expanded; first arg (if present) is db path
            kwargs["db_path"] = args[0]
        juicer.main.main(**kwargs)
        return

    # Make subcommand visible in child module's usage
    sys.argv[0] = f"{name} {command}"

    # Import subcommand module and run its main()
    try:
        module = importlib.import_module(command)
    except ModuleNotFoundError as e:
        logging.error("Cannot load command module '%s': %s", command, e)
        sys.exit(2)

    if hasattr(module, "main"):
        module.main(args)  # type: ignore[attr-defined]
    else:
        logging.error("Module '%s' has no main(args) function.", command)
        sys.exit(2)


if __name__ == "__main__":
    import sys
    sys.exit(main(sys.argv[1:]))