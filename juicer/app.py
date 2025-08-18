from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional, Iterable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gio  # type: ignore

from .window import LEMONJuicerWindow
from .utils import _pick_lemondb_from_args

# Basic logging setup if not already configured by host app
logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

class LEMONJuicerApp:
    """Wrapper around Gtk.Application for a clean CLI API."""
    APP_ID = "org.lemon.juicer"

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._initial_db_path = db_path
        self.app = Gtk.Application(
            application_id=self.APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        self.app.connect("activate", self._on_activate)
        self.app.connect("open", self._on_open)
        self._win: Optional[LEMONJuicerWindow] = None

    def _on_activate(self, app: Gtk.Application) -> None:
        if not self._win:
            self._win = LEMONJuicerWindow(app)
        self._win.present()

        if self._initial_db_path is not None:
            hint = self._initial_db_path
            chosen = (
                hint if (Path(hint).is_file() and Path(hint).suffix.lower() == ".lemondb")
                else _pick_lemondb_from_args(hint, sys.argv[1:])
            )
            if chosen:
                self._win.open_db(chosen)
            self._initial_db_path = None

    def _on_open(self, app: Gtk.Application, files, n_files: int, hint: str) -> None:
        if self._win is None:
            self._win = LEMONJuicerWindow(app)
        self._win.present()
        for f in files:
            try:
                path = f.get_path() if isinstance(f, Gio.File) else None
            except Exception:
                path = None
            if path and Path(path).is_file() and Path(path).suffix.lower() == ".lemondb":
                try:
                    self._win.open_db(path)
                except Exception as e:
                    self._win._show_error(f"Failed to open database:\n{path}\n\n{e}")
                break

    def run(self, argv: Optional[list[str]] = None) -> int:
        return self.app.run(argv or sys.argv)

def run_app(db_path: Optional[str] = None,
            argv: Optional[Iterable[str]] = None) -> int:
    chosen = None
    if db_path and Path(db_path).is_file() and Path(db_path).suffix.lower() == ".lemondb":
        chosen = db_path
    else:
        chosen = _pick_lemondb_from_args(db_path, argv or sys.argv[1:])
    if chosen:
        logger.info("Startup requested DB path: %s", chosen)
    else:
        logger.info("No valid *.LEMONdB provided on startup.")
    app = LEMONJuicerApp(db_path=chosen)
    return app.run(list(argv) if argv is not None else None)
