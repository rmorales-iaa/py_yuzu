# py_yuzu/conf_manager/conf_manager.py
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

# Optional GTK imports are done lazily so this module can be imported before GTK.
try:
    import gi  # type: ignore
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    from gi.repository import Gtk, Gdk  # type: ignore
except Exception:  # pragma: no cover - still fine for non-GUI parts
    Gtk = None  # type: ignore
    Gdk = None  # type: ignore


# -----------------------------
# Defaults used to seed the INI
# -----------------------------
DEFAULTS: Dict[str, Dict[str, Any]] = {
    "theme": {
        "font_name": "DejaVu Sans",
        "font_size": "11",
        "background": "#2a2f38",
        "foreground": "#e7ebef",
        "accent": "#ffd84d",
        "blue": "#0d6efd",
        "divider": "#ffd84d",
        "row_selected": "#375a7f",
    },

    # Main window: size/pos are best-effort (pos only on X11)
    "main_window": {
        "width": "1200",
        "height": "800",
        "x": "",              # blank means "not stored / center"
        "y": "",
        "maximized": "false",
        "remember_position": "true",
        "remember_size": "true",
        "font_name": "",
        "font_size": "",
        "background": "",
        "foreground": "",
    },

    # Star details window
    "star_window": {
        "width": "980",
        "height": "640",
        "x": "",
        "y": "",
        "maximized": "false",
        "ratio_top": "0.60",
        "ratio_plot": "0.62",
        "ratio_bottom_left": "0.50",
        "remember_position": "true",
        "remember_size": "true",
        "font_name": "",
        "font_size": "",
        "background": "",
        "foreground": "",
    },

    # Finding chart dialog
    "find_chart": {
        "width": "780",
        "height": "780",
        "x": "",
        "y": "",
        "maximized": "false",
        "remember_position": "true",
        "remember_size": "true",
        "font_name": "",
        "font_size": "",
        "background": "",
        "foreground": "",
        "radius_arcmin": "6.0",
        "recenter_one_sided_cloud": "true",
        "font_family": "DejaVu Sans",
    },
}


def _bool(s: str, default: bool = False) -> bool:
    if s is None:
        return default
    s = str(s).strip().lower()
    return s in ("1", "true", "yes", "on")


class ConfigManager:
    """
    Small INI-backed configuration manager with a few GTK helpers.

    - Loads/saves configuration.txt
    - Exposes get()/set() helpers
    - Builds CSS for the app theme from the [theme] section
    - Can restore & remember window size/position (best-effort)
    """

    def __init__(self, config_dir: Optional[Path] = None, filename: str = "configuration.txt") -> None:
        # default location: alongside this file
        if config_dir is None:
            config_dir = Path(__file__).resolve().parent
        self.config_dir = config_dir
        self.path = self.config_dir / filename

        self.cfg = configparser.ConfigParser()
        self.loaded = False

    # ---------------------
    # Core load / save API
    # ---------------------
    def load(self) -> None:
        """Load config; create the file with defaults if missing."""
        self.config_dir.mkdir(parents=True, exist_ok=True)

        if not self.path.exists():
            self._seed_defaults()
            self.save()
        else:
            self.cfg.read(self.path, encoding="utf-8")
            # make sure all default keys exist (non-destructive)
            for sect, kv in DEFAULTS.items():
                if sect not in self.cfg:
                    self.cfg[sect] = {}
                for k, v in kv.items():
                    if k not in self.cfg[sect]:
                        self.cfg[sect][k] = str(v)

        self.loaded = True

    def save(self) -> None:
        """Persist current configuration to disk."""
        with self.path.open("w", encoding="utf-8") as f:
            self.cfg.write(f)

    def _seed_defaults(self) -> None:
        """Create config with DEFAULTS."""
        for sect, kv in DEFAULTS.items():
            self.cfg[sect] = {}
            for k, v in kv.items():
                self.cfg[sect][k] = str(v)

    # -------------
    # Get / set API
    # -------------
    def get(self, section: str, key: str, fallback: Optional[str] = None) -> Optional[str]:
        return self.cfg.get(section, key, fallback=fallback)

    def getint(self, section: str, key: str, fallback: int) -> int:
        try:
            return self.cfg.getint(section, key, fallback=fallback)
        except Exception:
            return fallback

    def getfloat(self, section: str, key: str, fallback: float) -> float:
        try:
            return self.cfg.getfloat(section, key, fallback=fallback)
        except Exception:
            return fallback

    def getbool(self, section: str, key: str, fallback: bool) -> bool:
        try:
            return self.cfg.getboolean(section, key, fallback=fallback)
        except Exception:
            return fallback

    def set(self, section: str, key: str, value: Any) -> None:
        if section not in self.cfg:
            self.cfg[section] = {}
        self.cfg[section][key] = str(value)

    # -------------------
    # Theme / CSS helpers
    # -------------------
    def build_css(self) -> str:
        """Return the CSS string matching the theme section (colors + fonts)."""
        t = self.cfg["theme"]
        bg = t.get("background", DEFAULTS["theme"]["background"])
        fg = t.get("foreground", DEFAULTS["theme"]["foreground"])
        accent = t.get("accent", DEFAULTS["theme"]["accent"])
        blue = t.get("blue", DEFAULTS["theme"]["blue"])
        divider = t.get("divider", DEFAULTS["theme"]["divider"])
        row_sel = t.get("row_selected", DEFAULTS["theme"]["row_selected"])

        font_name = t.get("font_name", DEFAULTS["theme"]["font_name"])
        font_size = t.get("font_size", DEFAULTS["theme"]["font_size"])

        return f"""
@define-color lemon-bg        {bg};
@define-color lemon-fg        {fg};
@define-color lemon-accent    {accent};
@define-color lemon-blue      {blue};
@define-color lemon-divider   {divider};
@define-color lemon-row-sel   {row_sel};

.lemon-surface {{
  background-color: @lemon-bg;
}}
.lemon-surface, .lemon-surface * {{
  color: @lemon-fg;
  font-family: "{font_name}";
  font-size: {font_size}pt;
}}

scrolledwindow.lemon-scroller,
scrolledwindow.lemon-scroller viewport,
scrolledwindow.lemon-scroller viewport > *,
columnview.lemon-dark,
columnview.lemon-dark > *,
columnview.lemon-dark listview,
columnview.lemon-dark listview > *,
columnview.lemon-dark row,
columnview.lemon-dark row > * {{
  background-color: @lemon-bg;
}}

columnview.lemon-dark label {{
  color: @lemon-fg;
}}

columnview.lemon-dark row:selected {{
  background-color: @lemon-row-sel;
}}
columnview.lemon-dark row:selected label {{
  color: @lemon-accent;
}}

columnview.lemon-dark > header {{
  background-color: @lemon-blue;
}}
columnview.lemon-dark > header button {{
  background-image: none;
  background-color: transparent;
  color: #ffffff;
  border-color: transparent;
}}
columnview.lemon-dark > header button label {{
  color: #ffffff;
}}

columnview.lemon-dark row > * {{
  border-left: 1px solid @lemon-divider;
}}
columnview.lemon-dark row > *:first-child {{
  border-left: none;
}}
columnview.lemon-dark > header > * {{
  border-left: 1px solid @lemon-divider;
}}
columnview.lemon-dark > header > *:first-child {{
  border-left: none;
}}

.lemon-statusbar {{
  background-color: @lemon-bg;
  padding: 6px 10px;
}}
.lemon-statusbar * {{
  color: @lemon-accent;
}}

.lemon-btn {{
  background: none;
  background-color: @lemon-bg;
  color: @lemon-accent;
  border: 1px solid @lemon-accent;
  border-radius: 6px;
  padding: 6px 10px;
}}
.lemon-btn:hover {{
  filter: brightness(1.08);
}}
.lemon-btn:disabled {{
  opacity: 0.6;
}}
"""

    def install_css_for_display(self, display: "Gdk.Display") -> None:
        """Install the theme CSS for the given display."""
        if Gtk is None or Gdk is None or display is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(self.build_css().encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    # ------------------------------------
    # Window geometry (restore & remember)
    # ------------------------------------
    def restore_window(self, window: "Gtk.Window", section: str) -> None:
        """Apply stored size/position to a Gtk.Window (best-effort)."""
        if Gtk is None:
            return
        s = self.cfg.get(section, "remember_size", fallback="true")
        if _bool(s, True):
            w = self.getint(section, "width", 0)
            h = self.getint(section, "height", 0)
            if w > 0 and h > 0:
                window.set_default_size(w, h)

        m = self.get(section, "maximized", "")
        if _bool(m, False):
            try:
                window.maximize()
            except Exception:
                pass

        # Positions are only meaningful on X11; Wayland generally ignores them.
        p = self.cfg.get(section, "remember_position", fallback="true")
        if _bool(p, True):
            try:
                gi.require_version("GdkX11", "4.0")
                from gi.repository import GdkX11  # type: ignore
                display = Gdk.Display.get_default()
                surface = window.get_surface()
                if (
                    display is not None and surface is not None
                    and isinstance(display, GdkX11.X11Display)
                    and isinstance(surface, GdkX11.X11Surface)
                ):
                    x = self.cfg.get(section, "x", "")
                    y = self.cfg.get(section, "y", "")
                    if x and y:
                        import ctypes
                        libX11 = ctypes.CDLL("libX11.so.6")
                        xdisplay = GdkX11.X11Display.get_xdisplay(display)
                        xid = GdkX11.X11Surface.get_xid(surface)
                        libX11.XMoveWindow(xdisplay, xid, int(x), int(y))
                        libX11.XFlush(xdisplay)
            except Exception:
                # Wayland / missing X11 ? ignore
                pass

    def _read_current_geometry(self, window: "Gtk.Window") -> Tuple[int, int, Optional[int], Optional[int]]:
        """Return (w, h, x, y) best-effort from a Gtk.Window surface."""
        if Gtk is None or Gdk is None:
            return (0, 0, None, None)
        surface = window.get_surface()
        if surface is None:
            return (0, 0, None, None)
        w = surface.get_width()
        h = surface.get_height()

        x = y = None
        try:
            gi.require_version("GdkX11", "4.0")
            from gi.repository import GdkX11  # type: ignore
            display = Gdk.Display.get_default()
            if isinstance(display, GdkX11.X11Display) and isinstance(surface, GdkX11.X11Surface):
                import ctypes
                libX11 = ctypes.CDLL("libX11.so.6")
                xdisplay = GdkX11.X11Display.get_xdisplay(display)
                xid = GdkX11.X11Surface.get_xid(surface)
                # Query geometry; fall back if not available
                # We?ll try to read current location from the WM
                # (using XTranslateCoordinates into root)
                root = ctypes.c_ulong()
                child = ctypes.c_ulong()
                rx = ctypes.c_int()
                ry = ctypes.c_int()
                wx = ctypes.c_int()
                wy = ctypes.c_int()
                mask = ctypes.c_uint()
                libX11.XQueryPointer(xdisplay, xid, ctypes.byref(root), ctypes.byref(child),
                                     ctypes.byref(rx), ctypes.byref(ry),
                                     ctypes.byref(wx), ctypes.byref(wy),
                                     ctypes.byref(mask))
                x = int(rx.value) if rx.value else None
                y = int(ry.value) if ry.value else None
        except Exception:
            pass
        return (w, h, x, y)

    def remember_window_on_close(self, window: "Gtk.Window", section: str) -> None:
        """Connect to close-request to persist current size/pos/maximized."""
        if Gtk is None:
            return

        def _on_close(_w: "Gtk.Window") -> bool:
            try:
                w, h, x, y = self._read_current_geometry(_w)
                maximized = False
                try:
                    maximized = _w.is_maximized()
                except Exception:
                    pass
                if _bool(self.get(section, "remember_size", "true"), True):
                    if w > 0 and h > 0:
                        self.set(section, "width", w)
                        self.set(section, "height", h)
                if _bool(self.get(section, "remember_position", "true"), True):
                    if x is not None and y is not None:
                        self.set(section, "x", x)
                        self.set(section, "y", y)
                self.set(section, "maximized", str(maximized).lower())
                self.save()
            except Exception:
                # Never block closing due to config errors
                pass
            return False  # propagate (allow window to close)

        try:
            window.connect("close-request", _on_close)
        except Exception:
            pass

    # Convenience: attach (restore now & remember later)
    def attach_window(self, window: "Gtk.Window", section: str) -> None:
        self.restore_window(window, section)
        self.remember_window_on_close(window, section)


# Singleton instance, importable as `from conf_manager.conf_manager import cfg`
cfg = ConfigManager()

