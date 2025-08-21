# py_yuzu/conf_manager/conf_manager.py
from __future__ import annotations

import configparser
from pathlib import Path
from typing import Optional

try:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gio", "2.0")
    gi.require_version("GdkX11", "4.0")
    from gi.repository import Gtk, Gdk, Gio, GLib, GdkX11  # type: ignore
except Exception:  # pragma: no cover
    Gtk = Gdk = Gio = GLib = GdkX11 = None  # type: ignore


class ConfigManager:
    """
    INI-based configuration with:
      - theme color injection (@define-color variables)
      - window geometry persist/restore (size always; X/Y on X11 only)
      - ColumnView column width persist/restore (debounced writes)
    """

    def __init__(self, cfg_path: Optional[Path] = None) -> None:
        base_dir = Path(__file__).resolve().parent
        self.cfg_path = Path(cfg_path) if cfg_path else base_dir / "configuration.txt"
        self.cfg = configparser.ConfigParser()
        if self.cfg_path.exists():
            self.cfg.read(self.cfg_path, encoding="utf-8")
        else:
            self._seed_defaults()
            self.save()

        self._save_source_id: Optional[int] = None

    # -------- new: rebind to a different config file --------
    def rebind(self, new_path: str | Path) -> None:
        """Switch to a different configuration file immediately."""
        self.cfg_path = Path(new_path).expanduser().resolve()
        self.cfg = configparser.ConfigParser()
        if self.cfg_path.exists():
            self.cfg.read(self.cfg_path, encoding="utf-8")
        else:
            self._seed_defaults()
            self.save()

    # ---------- simple API ----------
    def get(self, section: str, key: str, default: str = "") -> str:
        try:
            return self.cfg.get(section, key)
        except Exception:
            return default

    def getint(self, section: str, key: str, default: int) -> int:
        try:
            return self.cfg.getint(section, key)
        except Exception:
            return default

    def getfloat(self, section: str, key: str, default: float) -> float:
        try:
            return self.cfg.getfloat(section, key)
        except Exception:
            return default

    def getbool(self, section: str, key: str, default: bool) -> bool:
        try:
            return self.cfg.getboolean(section, key)
        except Exception:
            return default

    def set(self, section: str, key: str, value: str) -> None:
        if not self.cfg.has_section(section):
            self.cfg.add_section(section)
        self.cfg.set(section, key, value)

    def save(self) -> None:
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with self.cfg_path.open("w", encoding="utf-8") as fh:
            self.cfg.write(fh)

    def save_debounced(self, delay_ms: int = 400) -> None:
        if GLib is None:
            self.save()
            return
        if self._save_source_id is not None:
            GLib.source_remove(self._save_source_id)
            self._save_source_id = None

        def _do() -> bool:
            try:
                self.save()
            finally:
                self._save_source_id = None
            return False

        self._save_source_id = GLib.timeout_add(delay_ms, _do)

    # ---------- theme / css ----------
    def install_css_for_display(self, display: Optional["Gdk.Display"] = None) -> None:
        if Gtk is None or Gdk is None:
            return
        display = display or Gdk.Display.get_default()
        if display is None:
            return

        bg = self.get("theme", "background", "#2a2f38")
        fg = self.get("theme", "foreground", "#e7ebef")
        accent = self.get("theme", "accent", "#ffd84d")
        blue = self.get("theme", "blue", "#0d6efd")
        divider = self.get("theme", "divider", "#ffd84d")
        row_sel = self.get("theme", "row_selected", "#375a7f")

        css_vars = f"""
        @define-color lemon-bg {bg};
        @define-color lemon-fg {fg};
        @define-color lemon-accent {accent};
        @define-color lemon-blue {blue};
        @define-color lemon-divider {divider};
        @define-color lemon-row-sel {row_sel};
        """

        provider = Gtk.CssProvider()
        provider.load_from_data(css_vars.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ---------- window geometry ----------
    def attach_window(self, win: "Gtk.Window", section: str) -> None:
        """Restore size, and save size/position on close; X11-only for position."""
        if Gtk is None:
            return

        # Restore size
        w = self.getint(section, "width", 0)
        h = self.getint(section, "height", 0)
        if w > 0 and h > 0:
            win.set_default_size(w, h)

        # Restore position (X11 only) after surface is realized
        def _try_move() -> bool:
            last_x = self.getint(section, "last_x", -1)
            last_y = self.getint(section, "last_y", -1)
            if last_x < 0 or last_y < 0:
                return False
            surf = win.get_surface()
            if not surf or surf.get_width() <= 0 or surf.get_height() <= 0:
                return True  # try again
            self._x11_move_window(win, last_x, last_y)
            return False

        if GLib is not None:
            GLib.idle_add(_try_move)

        def _on_close_req(_w: "Gtk.Window") -> bool:
            self.save_window_geometry(win, section, flush=False)
            self.save_debounced()
            return False

        win.connect("close-request", _on_close_req)

    def save_window_geometry(self, win: "Gtk.Window", section: str, *, flush: bool = False) -> None:
        """Capture current width/height and X11 position (if available)."""
        try:
            surf = win.get_surface()
            if surf:
                self.set(section, "width", str(max(0, surf.get_width())))
                self.set(section, "height", str(max(0, surf.get_height())))
                pos = self._x11_get_window_position(win)
                if pos:
                    x, y = pos
                    self.set(section, "last_x", str(max(0, x)))
                    self.set(section, "last_y", str(max(0, y)))
            if flush:
                self.save()
        except Exception:
            if flush:
                self.save()

    # ---------- column widths ----------
    def get_column_width(self, key: str, default_px: int) -> int:
        return self.getint("main_columns", key, default_px)

    def set_column_width(self, key: str, px: int, *, save: bool = True) -> None:
        if px <= 0:
            return
        self.set("main_columns", key, str(int(px)))
        if save:
            self.save_debounced()

    def attach_column(self,
                      column: "Gtk.ColumnViewColumn",
                      key: str,
                      default_px: Optional[int] = None,
                      *,
                      resizable: bool = True) -> None:
        if Gtk is None:
            return
        if default_px is None:
            default_px = -1
        px = self.get_column_width(key, default_px if default_px > 0 else -1)
        if px and px > 0:
            column.set_fixed_width(px)
        column.set_resizable(bool(resizable))

        def _on_width_changed(col: "Gtk.ColumnViewColumn", _pspec) -> None:
            try:
                fw = int(col.get_fixed_width())
                if fw > 0:
                    self.set_column_width(key, fw, save=True)
            except Exception:
                pass

        column.connect("notify::fixed-width", _on_width_changed)

    # ---------- internals: X11 helpers ----------
    def _x11_get_window_position(self, win: "Gtk.Window") -> Optional[tuple[int, int]]:
        """Return absolute (x,y) on X11; None on Wayland or failure."""
        if Gdk is None or GdkX11 is None:
            return None
        display = Gdk.Display.get_default()
        surf = win.get_surface()
        if not (display and surf):
            return None
        if not isinstance(display, GdkX11.X11Display) or not isinstance(surf, GdkX11.X11Surface):
            return None
        try:
            import ctypes
            libX11 = ctypes.CDLL("libX11.so.6")
            xdisplay = GdkX11.X11Display.get_xdisplay(display)
            xid = GdkX11.X11Surface.get_xid(surf)
            root = libX11.XDefaultRootWindow(xdisplay)

            root_return = ctypes.c_ulong()
            child_return = ctypes.c_ulong()
            root_x = ctypes.c_int()
            root_y = ctypes.c_int()
            mask = ctypes.c_uint()

            libX11.XTranslateCoordinates.argtypes = [
                ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                ctypes.c_int, ctypes.c_int,
                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                ctypes.POINTER(ctypes.c_ulong),
            ]
            ok = libX11.XTranslateCoordinates(
                xdisplay, xid, root, 0, 0,
                ctypes.byref(root_x), ctypes.byref(root_y),
                ctypes.byref(child_return)
            )
            if ok:
                return (int(root_x.value), int(root_y.value))
        except Exception:
            return None
        return None

    def _x11_move_window(self, win: "Gtk.Window", x: int, y: int) -> None:
        """Best-effort move on X11; ignored on Wayland."""
        if Gdk is None or GdkX11 is None:
            return
        display = Gdk.Display.get_default()
        surf = win.get_surface()
        if not (display and surf):
            return
        if not isinstance(display, GdkX11.X11Display) or not isinstance(surf, GdkX11.X11Surface):
            return
        try:
            import ctypes
            libX11 = ctypes.CDLL("libX11.so.6")
            xdisplay = GdkX11.X11Display.get_xdisplay(display)
            xid = GdkX11.X11Surface.get_xid(surf)
            libX11.XMoveWindow(xdisplay, xid, int(x), int(y))
            libX11.XFlush(xdisplay)
        except Exception:
            pass

    # ---------- defaults ----------
    def _seed_defaults(self) -> None:
        self.cfg["theme"] = {
            "font_name": "Cantarell",
            "font_size": "11",
            "background": "#2a2f38",
            "foreground": "#e7ebef",
            "accent": "#ffd84d",
            "blue": "#0d6efd",
            "divider": "#ffd84d",
            "row_selected": "#375a7f",
        }
        self.cfg["main_window"] = {
            "width": "1200",
            "height": "800",
            "center_on_start": "true",
            "last_x": "-1",
            "last_y": "-1",
        }
        self.cfg["star_window"] = {
            "width": "980",
            "height": "640",
            "ratio_top": "0.60",
            "ratio_plot": "0.62",
            "ratio_bottom_left": "0.50",
        }
        self.cfg["finding_chart"] = {
            "width": "760",
            "height": "760",
            "radius_arcmin": "6.0",
        }
        self.cfg["main_columns"] = {
            "id": "105",
            "mag": "96",
            "ra": "240",
            "dec": "240",
        }
        self.cfg["parallel"] = {
            "ncores": "16",
        }


cfg = ConfigManager()