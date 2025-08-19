# juicer/app.py
from __future__ import annotations

import logging
import sys
import signal
from pathlib import Path
from typing import Optional, Iterable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gio, GLib  # type: ignore

from .window import LEMONJuicerWindow
from .utils import _pick_lemondb_from_args

logger = logging.getLogger(__name__)


class LEMONJuicerApp:
    APP_ID = "org.lemon.juicer"

    def __init__(self, db_path: Optional[str] = None, autoselect_radec=None):
        # IMPORTANT: use Gtk.Application (not Gio.Application) so windows accept it
        self.app: Gtk.Application = Gtk.Application.new(
            self.APP_ID, Gio.ApplicationFlags.FLAGS_NONE
        )

        # Remember inputs
        self._initial_db_path: Optional[str] = db_path
        self._autoselect_radec = autoselect_radec

        # Main window handle
        self._win: Optional[Gtk.ApplicationWindow] = None

        # Signals
        self.app.connect("activate", self._on_activate)
        self.app.connect("open", self._on_open)

        # Graceful quit on Ctrl+C (POSIX)
        try:
            def _on_sigint():
                try:
                    self.app.quit()
                finally:
                    return False  # run once
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _on_sigint)
        except Exception:
            pass

    def _on_activate(self, app: Gtk.Application) -> None:
        if self._win is None:
            self._win = LEMONJuicerWindow(app)
            # Ensure autoselect coords are present on the window even if ctor didn’t accept them
            try:
                setattr(self._win, "_autoselect_radec", self._autoselect_radec)
            except Exception:
                pass
            if self._initial_db_path:
                self._win.open_db(self._initial_db_path)
        self._win.present()

    def _on_open(self, app: Gtk.Application, files, n_files: int, hint: str) -> None:
        if self._win is None:
            self._win = LEMONJuicerWindow(app)
            try:
                setattr(self._win, "_autoselect_radec", self._autoselect_radec)
            except Exception:
                pass
        self._win.present()
        for f in files:
            p = f.get_path()
            if p:
                self._win.open_db(p)

    def run(self, argv: Optional[Iterable[str]] = None) -> int:
        """Run the Gtk.Application, but exit cleanly on Ctrl+C (return 130)."""
        try:
            code = self.app.run(list(argv) if argv is not None else None)
            return int(code)
        except KeyboardInterrupt:
            logger.info("Interrupted by user (SIGINT).")
            try:
                self.app.quit()
            except Exception:
                pass
            return 130


def run_app(db_path: Optional[str] = None,
            argv: Optional[Iterable[str]] = None,
            start_radec: Optional[tuple[float, float]] = None) -> int:
    # Prefer explicit valid path; otherwise pick from argv
    if db_path and Path(db_path).is_file() and Path(db_path).suffix.lower() == ".lemondb":
        chosen = db_path
    else:
        chosen = _pick_lemondb_from_args(db_path, argv or sys.argv[1:])
    # No startup log here; juicer/main.py already logs it.
    app = LEMONJuicerApp(db_path=chosen, autoselect_radec=start_radec)
    return app.run(argv)
