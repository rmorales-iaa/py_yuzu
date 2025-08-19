# juicer/main.py
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Iterable, Optional, Tuple, Any

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


def _pick_lemondb_from_args(db_path: Optional[str],
                            argv: Optional[Iterable[str]]) -> Optional[str]:
    """
    Choose a .LEMONdB path from:
      1) explicit db_path if it exists and endswith .LEMONdB
      2) first argv token that points to an existing *.LEMONdB file
    """
    def _ok(p: Optional[str]) -> Optional[str]:
        if not p:
            return None
        pp = Path(p)
        return str(pp) if (pp.is_file() and pp.suffix.lower() == ".lemondb") else None

    chosen = _ok(db_path)
    if chosen:
        return chosen
    if argv:
        for a in argv:
            got = _ok(str(a))
            if got:
                return got
    return None


def _monkeypatch_window_autoselect(start_radec: Optional[Tuple[float, float]]) -> None:
    """
    If the current juicer.window.LEMONJuicerWindow doesn't take an
    'autoselect_radec' kwarg, patch its __init__ to accept it and/or set
    the attribute on the instance after construction.
    Harmless if the class or module layout changes; all exceptions swallowed.
    """
    if not start_radec:
        return
    try:
        from juicer import window as _w  # type: ignore
        if not hasattr(_w, "LEMONJuicerWindow"):
            return
        LJW = _w.LEMONJuicerWindow
        orig_init = LJW.__init__

        def _init_with_autoselect(self, *args, **kwargs):
            # Try passing the kwarg first (newer versions)
            try:
                return orig_init(self, *args, **kwargs, autoselect_radec=start_radec)
            except TypeError:
                # Fall back: call original and then set attribute
                rv = orig_init(self, *args, **kwargs)
                try:
                    setattr(self, "_autoselect_radec", start_radec)
                except Exception:
                    pass
                return rv

        # Only patch once
        if getattr(LJW, "_lemon_autoselect_patched", False) is not True:
            _w.LEMONJuicerWindow.__init__ = _init_with_autoselect  # type: ignore[assignment]
            setattr(LJW, "_lemon_autoselect_patched", True)
    except Exception:
        # Non-fatal if anything goes wrong; the app can still run.
        pass


def run_app(db_path: Optional[str] = None,
            argv: Optional[Iterable[str]] = None,
            start_radec: Optional[Tuple[float, float]] = None) -> int:
    """
    Preferred entry point. Tries to call juicer.app.run_app(...).
    If unavailable, constructs LEMONJuicerApp directly.
    """
    # Ensure the window will receive autoselect coords even on older apps
    _monkeypatch_window_autoselect(start_radec)

    # Pick a database path if needed
    chosen = _pick_lemondb_from_args(db_path, argv or sys.argv[1:])
    if chosen:
        logger.info("Startup requested DB path: %s", chosen)
    else:
        logger.info("No valid *.LEMONdB provided on startup.")

    # Import app and run
    try:
        from juicer import app as app_mod  # type: ignore
    except Exception as e:
        logger.error("Cannot import juicer.app: %s", e)
        return 2

    # If app provides a run_app, prefer that (try modern signature first)
    if hasattr(app_mod, "run_app"):
        try:
            return int(app_mod.run_app(db_path=chosen, argv=None, start_radec=start_radec))  # type: ignore[arg-type]
        except TypeError:
            # Fall back through a few older signatures
            for sig in (
                dict(db_path=chosen, argv=None),
                dict(db_path=chosen),
                dict(),
            ):
                try:
                    return int(app_mod.run_app(**sig))  # type: ignore[misc]
                except TypeError:
                    continue
            logger.error("juicer.app.run_app exists but no compatible signature matched.")
            return 2

    # Otherwise, try to build and run the application directly
    App: Any = getattr(app_mod, "LEMONJuicerApp", None)
    if App is None:
        logger.error("juicer.app has neither run_app() nor LEMONJuicerApp.")
        return 2

    try:
        try:
            app = App(db_path=chosen, autoselect_radec=start_radec)
        except TypeError:
            app = App(db_path=chosen)  # type: ignore[call-arg]
            # window gets patched to hold _autoselect_radec by _monkeypatch_window_autoselect
        return int(app.run(list(argv) if argv is not None else None))
    except Exception as e:
        logger.error("Failed to start LEMONJuicerApp: %s", e)
        return 2


def main(db_path: Optional[str] = None,
         argv: Optional[Iterable[str]] = None,
         start_radec: Optional[Tuple[float, float]] = None) -> int:
    """
    Backwards/forwards compatible main(). Accepts optional db_path,
    argv (iterable), and start_radec=(ra_deg, dec_deg).
    """
    return run_app(db_path=db_path, argv=argv, start_radec=start_radec)


if __name__ == "__main__":
    # Allow direct execution: python -m juicer.main /path/to/file.LEMONdB
    sys.exit(main(argv=sys.argv[1:]))
