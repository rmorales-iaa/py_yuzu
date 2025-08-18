# juicer/window.py
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Optional, Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gtk, Gio, Gdk, Pango, GLib  # type: ignore

from .models import StarRow
from .utils import _import_miner_class

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Optional configuration manager (preferred). If missing, we fall back to
# a local CSS installer and default sizes.
# -----------------------------------------------------------------------------
try:
    from conf_manager.conf_manager import cfg  # type: ignore
    _HAVE_CFG = True
except Exception:  # fallback stubs
    _HAVE_CFG = False

    class _CfgFallback:
        _css = b"""
        @define-color lemon-bg        #2a2f38;
        @define-color lemon-fg        #e7ebef;
        @define-color lemon-accent    #ffd84d;
        @define-color lemon-row-sel   #375a7f;
        @define-color lemon-blue      #0d6efd;
        @define-color lemon-divider   #ffd84d;

        .lemon-surface { background-color: @lemon-bg; }
        .lemon-surface, .lemon-surface * { color: @lemon-fg; }

        scrolledwindow.lemon-scroller,
        scrolledwindow.lemon-scroller viewport,
        scrolledwindow.lemon-scroller viewport > *,
        columnview.lemon-dark,
        columnview.lemon-dark > *,
        columnview.lemon-dark listview,
        columnview.lemon-dark listview > *,
        columnview.lemon-dark row,
        columnview.lemon-dark row > * { background-color: @lemon-bg; }

        columnview.lemon-dark label { color: @lemon-fg; }

        columnview.lemon-dark row:selected { background-color: @lemon-row-sel; }
        columnview.lemon-dark row:selected label { color: @lemon-accent; }

        columnview.lemon-dark > header { background-color: @lemon-blue; }
        columnview.lemon-dark > header button {
          background-image: none; background-color: transparent;
          color: #ffffff; border-color: transparent;
        }
        columnview.lemon-dark > header button label { color: #ffffff; }

        columnview.lemon-dark row > * { border-left: 1px solid @lemon-divider; }
        columnview.lemon-dark row > *:first-child { border-left: none; }
        columnview.lemon-dark > header > * { border-left: 1px solid @lemon-divider; }
        columnview.lemon-dark > header > *:first-child { border-left: none; }

        .lemon-statusbar { background-color: @lemon-bg; padding: 6px 10px; }
        .lemon-statusbar * { color: @lemon-accent; }

        .lemon-btn {
          background: none; background-color: @lemon-bg; color: @lemon-accent;
          border: 1px solid @lemon-accent; border-radius: 6px; padding: 6px 10px;
        }
        .lemon-btn:hover { filter: brightness(1.08); }
        .lemon-btn:disabled { opacity: 0.6; }
        """

        def install_css_for_display(self, display: Gdk.Display | None) -> None:
            if not display:
                display = Gdk.Display.get_default()
            if not display:
                return
            prov = Gtk.CssProvider()
            prov.load_from_data(self._css)
            Gtk.StyleContext.add_provider_for_display(
                display, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        # simple getters
        def get(self, section: str, key: str, default: str = "") -> str:
            return default

        def getint(self, section: str, key: str, default: int) -> int:
            return default

        def getfloat(self, section: str, key: str, default: float) -> float:
            return default

        def getbool(self, section: str, key: str, default: bool) -> bool:
            return default

        # geometry persistence no-ops
        def attach_window(self, win: Gtk.Window, section: str) -> None:
            return

    cfg = _CfgFallback()  # type: ignore


class LEMONJuicerWindow(Gtk.ApplicationWindow):
    """GTK4 main window with a ColumnView listing stars from the DB."""

    def __init__(self, app: Gtk.Application, title_suffix: str = ""):
        super().__init__(application=app, title=f"LEMON Juicer{title_suffix}")

        # Install theme CSS (from config or fallback)
        try:
            cfg.install_css_for_display(Gdk.Display.get_default())
        except Exception:
            pass

        # Size from configuration (fallback to 1200x800)
        w = cfg.getint("main_window", "width", 1200)
        h = cfg.getint("main_window", "height", 800)
        self.set_default_size(w, h)

        # ---------- HeaderBar ----------
        hb = Gtk.HeaderBar.new()
        self.set_titlebar(hb)

        open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        open_btn.set_tooltip_text("Open LEMON database?")
        open_btn.add_css_class("lemon-btn")
        open_btn.connect("clicked", self._on_open_clicked)
        hb.pack_start(open_btn)

        quit_btn = Gtk.Button(label="Quit")
        quit_btn.add_css_class("lemon-btn")
        quit_btn.connect("clicked", lambda *_: self.get_application().quit())
        hb.pack_end(quit_btn)

        # ---------- Root layout ----------
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.add_css_class("lemon-surface")
        self.set_child(root)

        # ---------- Model ----------
        self._rows = Gio.ListStore.new(StarRow)
        self._sel = Gtk.SingleSelection.new(self._rows)
        self._sel.connect("selection-changed", self._on_selection_changed)

        # ---------- Column factory ----------
        def make_text_column(
            title: str,
            prop: str,
            *,
            width_chars: int | None,
            xalign: float,
            monospace: bool = True,
            formatter: Optional[Callable[[object], str]] = None,
            expand: bool = False,
            label_width_chars: int | None = None,
        ) -> Gtk.ColumnViewColumn:
            factory = Gtk.SignalListItemFactory()

            def setup(_f, li: Gtk.ListItem):
                lbl = Gtk.Label(xalign=xalign)
                lbl.set_single_line_mode(True)
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                if monospace:
                    lbl.add_css_class("monospace")
                if label_width_chars is not None:
                    lbl.set_width_chars(label_width_chars)
                li.set_child(lbl)

            def bind(_f, li: Gtk.ListItem):
                row: StarRow | None = li.get_item()
                val = row.get_property(prop) if row is not None else None
                text = formatter(val) if formatter is not None else ("" if val is None else str(val))
                child = li.get_child()
                if isinstance(child, Gtk.Label):
                    child.set_text(text)

            factory.connect("setup", setup)
            factory.connect("bind", bind)

            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(expand)
            if width_chars is not None and not expand:
                col.set_fixed_width(int(width_chars * 9 + 24))  # rough monospace char?px
            return col

        # ---------- ColumnView (table) ----------
        self._view = Gtk.ColumnView.new(self._sel)
        self._view.add_css_class("lemon-dark")
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)

        # Double-click / Enter activation -> open details
        self._view.connect("activate", self._on_view_activate)

        # Columns: ID (fixed), Mag (fixed), RA (expand), Dec (expand)
        self._view.append_column(
            make_text_column(
                "ID",
                "id",
                width_chars=9,
                xalign=1.0,
                monospace=True,
                expand=False,
                label_width_chars=9,
                formatter=lambda v: "" if v is None else f"{int(v)}",
            )
        )
        self._view.append_column(
            make_text_column(
                "Mag",
                "imag",
                width_chars=8,
                xalign=1.0,
                monospace=True,
                expand=False,
                label_width_chars=8,
                formatter=lambda v: ""
                if (v is None or (isinstance(v, float) and math.isnan(v)))
                else f"{float(v):.3f}",
            )
        )
        self._view.append_column(
            make_text_column(
                "RA (hms)",
                "ra_str",
                width_chars=None,
                xalign=0.0,
                monospace=True,
                expand=True,
                label_width_chars=20,
            )
        )
        self._view.append_column(
            make_text_column(
                "Dec (dms)",
                "dec_str",
                width_chars=None,
                xalign=0.0,
                monospace=True,
                expand=True,
                label_width_chars=20,
            )
        )

        # ---------- Scroller ----------
        scroller = Gtk.ScrolledWindow.new()
        scroller.add_css_class("lemon-scroller")
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._view)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        root.append(scroller)

        # ---------- Status bar ----------
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.add_css_class("lemon-statusbar")
        status_bar.set_hexpand(True)
        self._status = Gtk.Label(label="Open a .LEMONdB to begin.")
        self._status.set_halign(Gtk.Align.START)
        status_bar.append(self._status)
        root.append(status_bar)

        # ---------- Data holders ----------
        self._miner = None
        self._db_path: Optional[str] = None
        self._file_dlg: Optional[Gtk.FileChooserNative] = None
        self._details_win = None  # created on demand

        # Persist geometry/position via cfg (no-op in fallback)
        try:
            cfg.attach_window(self, "main_window")
        except Exception:
            pass

        # Center main window at startup if no stored position (X11 best-effort)
        GLib.idle_add(self._center_main_window_once)

    # ---- UI helpers ----
    def _set_status(self, text: str) -> None:
        self._status.set_label(text)

    def _show_error(self, message: str, title: str = "Error") -> None:
        logger.error("%s", message)
        # Try a notification
        try:
            n = Gio.Notification.new(title)
            n.set_body(message)
            n.set_priority(Gio.NotificationPriority.HIGH)
            app = self.get_application()
            if app is not None:
                app.send_notification(None, n)
        except Exception:
            pass
        # Always print to stderr
        print(f"{title}: {message}", file=sys.stderr)

    # ---- Centering (best-effort on X11/VNC) ----
    def _center_main_window_once(self) -> bool:
        try:
            # If a position was remembered by cfg, don't re-center
            if _HAVE_CFG and (cfg.get("main_window", "x", "") or cfg.get("main_window", "y", "")):
                return False

            surface = self.get_surface()
            if surface is None or surface.get_width() == 0 or surface.get_height() == 0:
                # Try again until we have a real surface size
                return True

            display = Gdk.Display.get_default()
            if not display:
                return False

            monitor = display.get_primary_monitor()
            if not monitor:
                return False

            rect = monitor.get_geometry()
            win_w = surface.get_width()
            win_h = surface.get_height()
            target_x = int(rect.x + (rect.width - win_w) / 2)
            target_y = int(rect.y + (rect.height - win_h) / 2)

            # X11-specific move via GdkX11 + libX11 (best effort)
            try:
                gi.require_version("GdkX11", "4.0")
                from gi.repository import GdkX11  # type: ignore
                if isinstance(display, GdkX11.X11Display) and isinstance(surface, GdkX11.X11Surface):
                    import ctypes
                    libX11 = ctypes.CDLL("libX11.so.6")
                    xdisplay = GdkX11.X11Display.get_xdisplay(display)
                    xid = GdkX11.X11Surface.get_xid(surface)
                    libX11.XMoveWindow(xdisplay, xid, target_x, target_y)
                    libX11.XFlush(xdisplay)
            except Exception:
                pass
        except Exception:
            pass
        return False  # run once

    # ---- Selection / activation ----
    def _on_selection_changed(self, _model, _pos, _n_items):
        item = self._sel.get_selected_item()
        if not item:
            self._status.set_label("No star selected.")
            return
        sid = getattr(item.props, "id", None)
        ra = getattr(item.props, "ra_str", "")
        dec = getattr(item.props, "dec_str", "")
        mag = getattr(item.props, "imag", float("nan"))
        mag_txt = "" if (mag is None or (isinstance(mag, float) and math.isnan(mag))) else f"{mag:.3f}"
        self._status.set_label(f"ID: {sid}  |  RA: {ra}  |  Dec: {dec}  |  Mag: {mag_txt}")

    def _on_view_activate(self, _view: Gtk.ColumnView, position: int):
        try:
            self._open_row_at_position(position)
        except Exception as e:
            self._show_error(f"Failed to open row at {position}: {e}")

    def _open_row_at_position(self, position: int) -> None:
        item = self._sel.get_item(position)
        if not item:
            return
        self._open_details_for(item)

    def _open_selected_details(self) -> None:
        item = self._sel.get_selected_item()
        if item:
            self._open_details_for(item)

    def _open_details_for(self, item: StarRow) -> None:
        try:
            from .star_window import StarDetailsWindow
            self._details_win = StarDetailsWindow(self, item, self._miner)
            self._details_win.present()
        except Exception as e:
            self._show_error(f"Failed to build Star Details window:\n{e}")

    # ---- Open handlers ----
    def _on_open_clicked(self, _btn: Gtk.Button) -> None:
        self._show_open_dialog()

    def _show_open_dialog(self) -> None:
        # Robust across distros: FileChooserNative survives GC; keep as a field
        dlg = Gtk.FileChooserNative.new(
            "Open LEMON database",
            self,
            Gtk.FileChooserAction.OPEN,
            "_Open",
            "_Cancel",
        )
        self._file_dlg = dlg

        # Filter *.LEMONdB
        f = Gtk.FileFilter()
        f.set_name("LEMON Database (*.LEMONdB)")
        f.add_pattern("*.LEMONdB")
        dlg.add_filter(f)

        def on_response(dialog: Gtk.FileChooserNative, response: int):
            try:
                if response == Gtk.ResponseType.ACCEPT:
                    file = dialog.get_file()
                    if file:
                        path = file.get_path()
                        if path:
                            self.open_db(path)
            finally:
                # Explicitly destroy and drop ref so it doesn't linger
                dialog.destroy()
                if getattr(self, "_file_dlg", None) is dialog:
                    self._file_dlg = None

        dlg.connect("response", on_response)
        dlg.show()

    # ---- DB ops ----
    def open_db(self, path: str) -> None:
        p = Path(path) if path else None
        try:
            if not p or not p.exists():
                raise FileNotFoundError(f"Path does not exist:\n{path}")
            if not p.is_file():
                raise IsADirectoryError(f"Expected a .LEMONdB file, got a directory:\n{path}")
            if p.suffix.lower() != ".lemondb":
                raise ValueError(f"Not a LEMON database (*.LEMONdB):\n{path}")
            self._load_db(str(p))
        except Exception as e:
            self._show_error(f"Failed to open database:\n{p}\n\n{e}")

    def _load_db(self, db_path: str) -> None:
        Miner = _import_miner_class()
        miner = Miner(db_path)

        self._miner = miner
        self._db_path = db_path
        self.set_title(f"LEMON Juicer ? {Path(db_path).name}")
        logger.info("Opened database: %s", db_path)

        self._populate_overview_from_miner(miner)

    def _populate_overview_from_miner(self, miner) -> None:
        """Fill the ColumnView with star rows + update status line."""
        # Safely reset store
        try:
            self._rows.remove_all()
        except Exception:
            self._rows = Gio.ListStore.new(StarRow)
            self._sel.set_model(self._rows)
            self._view.set_model(self._sel)

        n_added = 0
        for star_id in getattr(miner, "star_ids", []):
            try:
                x, y, ra, dec, *_rest, imag = miner.get_star(star_id)
                row = StarRow(id=star_id, ra=float(ra), dec=float(dec), imag=float(imag))
                self._rows.append(row)
                n_added += 1
            except Exception:
                continue

        n_filters = len(getattr(miner, "pfilters", []))
        field = getattr(miner, "field_name", "") or Path(getattr(miner, "path", "")).name
        self._set_status(f"Field: {field} ? Stars shown: {n_added} ? Filters: {n_filters}")
