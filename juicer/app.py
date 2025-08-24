# juicer/app.py ? GTK4 application entrypoint using external .ui files
# - Loads UI from /mnt/uxmal_groups/common_data/apps/py_yuzu/juicer/gui/main.ui (or $LEMON_GLADE_DIR)
# - Hooks ConfigManager (if available) for theme + window geometry
# - Optionally persists ColumnView column widths

from __future__ import annotations

import logging
import os
import sys
import re
from pathlib import Path
from typing import Optional, Iterable, Tuple, Callable, List

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk  # type: ignore

from .window import LEMONJuicerWindow
from .utils import _pick_lemondb_from_args

# --- Config manager: try relative first, then top-level, else disable ---
try:
    from .conf_manager import cfg  # type: ignore
except Exception:
    try:
        from conf_manager import cfg  # type: ignore
    except Exception:
        cfg = None  # type: ignore

logger = logging.getLogger(__name__)

# ----- UI directory resolution -----
_DEFAULT_UI_DIR = Path("/mnt/uxmal_groups/common_data/apps/py_yuzu/juicer/gui")
_ENV_UI_DIR = Path(os.environ.get("LEMON_GLADE_DIR", str(_DEFAULT_UI_DIR)))


def _candidate_paths(name: str) -> list[Path]:
    return [p for p in (_ENV_UI_DIR / name, _DEFAULT_UI_DIR / name) if True]


def _load_main_builder() -> Gtk.Builder:
    for p in _candidate_paths("main.ui"):
        if p.is_file():
            try:
                b = Gtk.Builder.new_from_file(str(p))
                logger.info("Loaded main UI: %s", p)
                return b
            except Exception as e:
                logger.warning("Failed loading main UI at %s: %s", p, e)
    for p in _candidate_paths("main.glade"):
        if p.is_file():
            try:
                b = Gtk.Builder.new_from_file(str(p))
                logger.info("Loaded legacy main UI (glade): %s", p)
                return b
            except Exception as e:
                logger.warning("Failed loading legacy main.glade at %s: %s", p, e)
    return Gtk.Builder()


def _maybe_set_menubar(builder: Gtk.Builder, app: Gtk.Application) -> None:
    try:
        menu = builder.get_object("app-menu")
        if isinstance(menu, Gio.MenuModel):
            app.set_menubar(menu)
    except Exception:
        pass


def _slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[\s/|]+", "_", s)
    s = re.sub(r"[^a-z0-9_]+", "", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "col"


def _iter_children(widget: Gtk.Widget) -> List[Gtk.Widget]:
    try:
        model = widget.observe_children()
        out: List[Gtk.Widget] = []
        n = model.get_n_items()
        for i in range(n):
            w = model.get_item(i)
            if isinstance(w, Gtk.Widget):
                out.append(w)
        return out
    except Exception:
        return []


def _walk_widgets(root: Gtk.Widget) -> List[Gtk.Widget]:
    out: List[Gtk.Widget] = []
    stack = [root]
    while stack:
        w = stack.pop()
        out.append(w)
        stack.extend(_iter_children(w))
    return out


def _attach_column_width_persistence(window: Gtk.Window) -> None:
    """
    Persist GtkColumnView column widths via cfg.attach_column if available.
    Safe no-op if cfg is None or no ColumnViews exist.
    """
    if cfg is None:
        return
    try:
        root = window.get_child()
        if not isinstance(root, Gtk.Widget):
            return
        widgets = _walk_widgets(root)
        column_views = [w for w in widgets if w.__class__.__name__ in ("ColumnView", "GtkColumnView")]
        if not column_views:
            return

        def get_default_width(key: str, fallback: int = 100) -> int:
            try:
                return int(cfg.getint("main_columns", key, fallback))  # type: ignore[attr-defined]
            except Exception:
                return fallback

        attach = getattr(cfg, "attach_column", None)
        if not callable(attach):
            return

        for cv in column_views:
            try:
                cols_model = cv.get_columns()
                n = cols_model.get_n_items()
                for i in range(n):
                    col = cols_model.get_item(i)
                    key = None
                    try:
                        title = col.get_title()  # type: ignore[attr-defined]
                        if isinstance(title, str) and title.strip():
                            key = _slugify(title)
                    except Exception:
                        pass
                    if not key:
                        try:
                            name = Gtk.Buildable.get_buildable_id(col)  # type: ignore[attr-defined]
                            if isinstance(name, str) and name.strip():
                                key = _slugify(name)
                        except Exception:
                            key = f"col_{i+1}"
                    default_px = get_default_width(key, 100)
                    try:
                        attach(col, key, default_px=default_px)  # type: ignore[misc]
                    except Exception:
                        try:
                            attach(col, key, default_px)  # type: ignore[misc]
                        except Exception:
                            pass
            except Exception:
                continue
    except Exception:
        logger.debug("Column width persistence skipped", exc_info=True)


class LEMONJuicerApp(Gtk.Application):
    """Main GTK4 Application using external .ui files + optional ConfigManager."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        autoselect_radec: Optional[Tuple[float, float]] = None,
        **kwargs,
    ) -> None:
        super().__init__(
            application_id="org.vik-20.lemon.juicer",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
            **kwargs,
        )
        self._win: Optional[LEMONJuicerWindow] = None
        self._opening_db_dialog: Optional[Gtk.FileDialog] = None
        self._initial_db_path: Optional[str] = db_path
        self._autoselect_radec: Optional[Tuple[float, float]] = autoselect_radec

        GLib.set_application_name("LEMON")
        Gtk.Window.set_default_icon_name("lemon-juicer")

        # Actions
        self._add_action("toggle_window_height", self._toggle_window_height, param_type="s")
        self._add_action("mark_reference", self._mark_as_reference)
        self._add_action("accept_lightcurve", self._accept_lightcurve)
        self._add_action("reject_lightcurve", self._reject_lightcurve)
        self._add_action("open_db", self._open_db_menu)
        self._add_action("open", self._on_open)
        self._add_action("quit", self._on_quit)
        self._add_action("goto", self._goto_id)
        self._add_action("goto_selection", self._goto_selection)

    # ---- Lifecycle ----
    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)

    def do_activate(self) -> None:
        Gtk.Application.do_activate(self)
        logger.info("Activating")

        if self._win is None:
            builder = _load_main_builder()
            _maybe_set_menubar(builder, self)

            self._win = self._construct_window(builder)

            # Theme & geometry via config manager (optional)
            try:
                if cfg is not None:
                    cfg.install_css_for_display()           # global theme colors
                    cfg.attach_window(self._win, "main_window")  # restore/save geometry
            except Exception:
                logger.debug("ConfigManager setup failed", exc_info=True)

            # Optional CSS next to main.ui
            try:
                css_path = _ENV_UI_DIR / "style.css"
                if css_path.is_file():
                    css = Gtk.CssProvider()
                    css.load_from_path(str(css_path.resolve()))
                    Gtk.StyleContext.add_provider_for_display(
                        Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    logger.info("Loaded CSS: %s", css_path)
            except Exception as e:
                logger.warning("Failed to load CSS: %s", e)

            # Optional: persist ColumnView widths
            _attach_column_width_persistence(self._win)

            self._win.present()

            if self._initial_db_path:
                self._win.open_db(self._initial_db_path)
                try:
                    GLib.idle_add(lambda: (self._try_open_star_details() or False))
                except Exception:
                    pass
        else:
            self._win.present()

    # ---- Helpers ----
    def _add_action(self, name: str, handler, *, param_type: Optional[str] = None):
        act = Gio.SimpleAction.new(name, GLib.VariantType.new(param_type) if param_type else None)
        act.connect("activate", handler)
        self.add_action(act)

    def _construct_window(self, builder: Gtk.Builder) -> LEMONJuicerWindow:
        attempts: list[Callable[[], LEMONJuicerWindow]] = [
            lambda: LEMONJuicerWindow(self, builder),           # (app, builder)
            lambda: LEMONJuicerWindow(self, builder=builder),   # (app, builder=...)
            lambda: LEMONJuicerWindow(app=self, builder=builder),
            lambda: LEMONJuicerWindow(builder=builder, app=self),
            lambda: LEMONJuicerWindow(builder=builder),
            lambda: LEMONJuicerWindow(builder),
            lambda: LEMONJuicerWindow(),
        ]
        last_err: Optional[Exception] = None
        for make in attempts:
            try:
                win = make()
                try:
                    if not hasattr(win, "builder"):
                        setattr(win, "builder", builder)
                    if self._autoselect_radec is not None and not hasattr(win, "autoselect_radec"):
                        setattr(win, "autoselect_radec", self._autoselect_radec)
                        setattr(win, "_autoselect_radec", self._autoselect_radec)
                except Exception:
                    pass
                return win
            except Exception as e:
                last_err = e
        raise RuntimeError(f"Could not construct LEMONJuicerWindow: {last_err}")

    def _try_open_star_details(self):
        win = self._win
        if not win:
            return False
        for name in ("open_star_details_for_selection", "open_selected_star", "open_star_window"):
            fn = getattr(win, name, None)
            if callable(fn):
                try:
                    fn()
                    return False
                except Exception:
                    pass
        try:
            view = getattr(win, "treeview", None) or getattr(win, "listview", None)
            if view and hasattr(view, "activate"):
                view.activate()
                return False
        except Exception:
            pass
        return False

    # ---- Actions ----
    def _on_quit(self, *args) -> None:
        self.quit()

    def _open_db_menu(self, action: Gio.SimpleAction, *args):
        self._on_open(self, action)

    def _on_open(self, action: Gio.SimpleAction, *args):
        try:
            if self._opening_db_dialog is not None:
                logger.warning("Dialog already open")
                return

            dialog = Gtk.FileDialog(title="Select LEMON Database")
            filters = Gtk.FileFilter()
            filters.set_name("LEMON Database (*.LEMONdB)")
            filters.add_pattern("*.LEMONdB")
            dialog.set_default_filter(filters)
            self._opening_db_dialog = dialog

            def on_open_dialog_complete(source: Gtk.FileDialog, result: Gio.AsyncResult):
                try:
                    files: list[Gtk.File] = source.open_multiple_finish(result)
                    if not files:
                        return

                    if self._win is None:
                        self.do_activate()
                    assert isinstance(self._win, LEMONJuicerWindow), "Window extended type"

                    for f in files:
                        p = f.get_path()
                        if p:
                            self._win.open_db(p)
                    try:
                        GLib.idle_add(lambda: (self._try_open_star_details() or False))
                    except Exception:
                        pass

                except GLib.Error as ex:
                    logger.warning("Dialog error: %s", ex.message)
                except Exception:
                    logger.exception("Open DB error")
                finally:
                    self._opening_db_dialog = None

            dialog.open_multiple(self._win, None, on_open_dialog_complete)
        except Exception:
            logger.exception("Open DB dialog init failed")
            self._opening_db_dialog = None

    def _mark_as_reference(self, *args) -> None:
        assert self._win, "there must be a window already"
        for idx in self._win.get_selected_rows():
            self._win.mark_as_reference(idx)

    def _accept_lightcurve(self, *args) -> None:
        assert self._win, "there must be a window already"
        for idx in self._win.get_selected_rows():
            self._win.accept_lightcurve(idx)

    def _reject_lightcurve(self, *args) -> None:
        assert self._win, "there must be a window already"
        for idx in self._win.get_selected_rows():
            self._win.reject_lightcurve(idx)

    def _goto_id(self, *args) -> None:
        assert self._win, "there must be a window already"
        to = self._win.ask_user_for_id("goto_id")
        if to is None:
            logger.warning("No id present? Aborting")
            return
        self._win.goto_starid(to)

    def _goto_selection(self, *args) -> None:
        assert self._win, "there must be a window already"
        self._win.goto_current_selection()

    def _toggle_window_height(self, action: Gio.SimpleAction, param: GLib.Variant) -> None:
        value = param.get_string()
        assert self._win
        if value == "max":
            self._win.set_decorated(False)
            self._win.maximize()
            return
        self._win.set_decorated(True)
        self._win.unmaximize()


# ---- Public launcher (used by juicer.main) ----
def run_app(
    db_path: Optional[str] = None,
    argv: Optional[Iterable[str]] = None,
    start_radec: Optional[Tuple[float, float]] = None,
) -> int:
    """Create and run the application with optional DB and autoselection."""
    try:
        chosen = _pick_lemondb_from_args(db_path, argv or sys.argv[1:])
    except Exception:
        chosen = db_path

    app = LEMONJuicerApp(db_path=chosen, autoselect_radec=start_radec)
    try:
        return int(app.run(list(argv) if argv is not None else None) or 0)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    db_arg = None
    if len(sys.argv) > 1 and sys.argv[1] != "--g-fatal-warnings":
        db_arg = sys.argv[1]
    sys.exit(run_app(db_path=db_arg, argv=sys.argv[1:]))
