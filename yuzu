#!/usr/bin/env python3
from __future__ import annotations

import difflib
import importlib
import logging
import os
import sys
import glob
from pathlib import Path
from typing import List, Optional

# Enforce minimum versions via import hooks in sys.meta_path
import check_versions  # noqa: F401

# Allow rebinding the configuration file before importing UI modules
try:
    from conf_manager.conf_manager import cfg
except Exception:
    cfg = None  # type: ignore

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

DEFAULT_CFG_REL = "conf_manager/configuration.txt"


def show_help(name: str) -> None:
    """Help message, listing all commands, that looks like Git's."""
    print(f"usage: {name} [--config PATH] [--help] [--version] [--update] COMMAND [ARGS]\n")
    print("Global options:")
    print("   -c, --config PATH   Path to configuration INI (default: conf_manager/configuration.txt)\n")
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


def _extract_global_config(argv: List[str]) -> tuple[Optional[str], List[str]]:
    """
    Pull a --config PATH / -c PATH (or --config=PATH) option out of argv.
    Returns (config_path, remaining_argv).
    """
    cfg_path: Optional[str] = None
    rem: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--config" or a == "-c":
            if i + 1 < len(argv):
                cfg_path = argv[i + 1]
                i += 2
                continue
            else:
                print("error: --config expects a PATH", file=sys.stderr)
                sys.exit(2)
        elif a.startswith("--config="):
            cfg_path = a.split("=", 1)[1]
            i += 1
            continue
        else:
            rem.append(a)
            i += 1
    return cfg_path, rem


def _rebind_cfg_if_requested(cfg_path_opt: Optional[str]) -> None:
    if cfg is None:
        return
    base_default = Path(__file__).resolve().parent / DEFAULT_CFG_REL
    if cfg_path_opt:
        cfg.rebind(cfg_path_opt)
    else:
        # Ensure default exists relative to this script
        cfg.rebind(base_default)


def main(argv: List[str]) -> None:
    # Normalize/expand variables and globs first so IDEs (which don't glob) work
    argv = _expand_args(argv)

    # Pull global --config/-c before we import any UI modules
    cfg_path_opt, argv = _extract_global_config(argv)
    _rebind_cfg_if_requested(cfg_path_opt)

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
            # args already expanded; first arg (if present) is db path
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
