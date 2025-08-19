# juicer/app.py
from __future__ import annotations
import logging, sys
from pathlib import Path
from typing import Optional, Iterable, Tuple
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gio  # type: ignore

from .window import LEMONJuicerWindow
from .utils import _pick_lemondb_from_args

logger = logging.getLogger(__name__)

class LEMONJuicerApp:
    APP_ID = "org.lemon.juicer"

    def __init__(self, db_path: Optional[str] = None,
                 autoselect_radec: Optional[Tuple[float, float]] = None) -> None:
        self._initial_db_path = db_path
        self._autoselect_radec = autoselect_radec
        self.app = Gtk.Application(application_id=self.APP_ID,
                                   flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.app.connect("activate", self._on_activate)
        self.app.connect("open", self._on_open)
        self._win: Optional[LEMONJuicerWindow] = None

    def _on_activate(self, app: Gtk.Application) -> None:
        if self._win is None:
            self._win = LEMONJuicerWindow(app)
            # Ensure window has the attribute even if __init__ wasn’t patched
            setattr(self._win, "_autoselect_radec", self._autoselect_radec)
            if self._initial_db_path:
                self._win.open_db(self._initial_db_path)
        self._win.present()

    def _on_open(self, app: Gtk.Application, files, n_files: int, hint: str) -> None:
        if self._win is None:
            self._win = LEMONJuicerWindow(app)
            setattr(self._win, "_autoselect_radec", self._autoselect_radec)
        self._win.present()
        for f in files:
            p = f.get_path()
            if p:
                self._win.open_db(p)

    def run(self, argv: Optional[Iterable[str]] = None) -> int:
        return self.app.run(list(argv) if argv is not None else None)


def run_app(db_path: Optional[str] = None,
            argv: Optional[Iterable[str]] = None,
            start_radec: Optional[Tuple[float, float]] = None) -> int:
        # ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^ accept it here
    if db_path and Path(db_path).is_file() and Path(db_path).suffix.lower() == ".lemondb":
        chosen = db_path
    else:
        chosen = _pick_lemondb_from_args(db_path, argv or sys.argv[1:])
    if chosen:
        logger.info("Startup requested DB path: %s", chosen)
    else:
        logger.info("No valid *.LEMONdB provided on startup.")
    app = LEMONJuicerApp(db_path=chosen, autoselect_radec=start_radec)
    return app.run(argv)
