# juicer/string_list_frame.py
from __future__ import annotations
from typing import Any, List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, Gdk  # type: ignore

# ----- theme helpers (optional conf_manager) -----
try:
    from .conf_manager import cfg  # type: ignore
except Exception:
    try:
        from conf_manager import cfg  # type: ignore
    except Exception:
        cfg = None  # type: ignore

def _cfg_get(section: str, key: str, default: str) -> str:
    try:
        return cfg.get(section, key) if cfg else default  # type: ignore[attr-defined]
    except Exception:
        return default

_BG        = _cfg_get("theme", "background", "#0b1220")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_BLUE_HDR  = _cfg_get("theme", "section_bg", "#1e3a8a")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

def _install_theme_css() -> None:
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(f"""
            .lemon-surface {{ background: {_BG}; }}
            .lemon-surface * {{ color: {_FG}; }}
            .monospace {{ font-family: {_MONO_FONT}; }}
            .section-header {{
                background: {_BLUE_HDR};
                color: #fff;
                padding: 6px 10px;
                font-weight: 600;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }}
            .section-frame {{
                border: 1px solid rgba(255,255,255,0.08);
                border-radius: 12px;
            }}
        """.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

def _curve_to_points(curve: Any):
    if curve is None:
        return []
    try:
        out = []
        for item in curve:
            try:
                t, mag, snr = item  # type: ignore[misc]
                out.append((float(t), float(mag), None if snr is None else float(snr)))
            except Exception:
                continue
        if out:
            return out
    except Exception:
        pass
    for attr in ("_data", "data"):
        try:
            seq = getattr(curve, attr)
            return _curve_to_points(seq)
        except Exception:
            continue
    return []

def _fmt_time(unix: float) -> str:
    import datetime as _dt
    try:
        return _dt.datetime.utcfromtimestamp(float(unix)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{float(unix):.6f}"

def _format_mag(x):
    try:
        return "—" if x is None else f"{float(x):.4f}"
    except Exception:
        return str(x)

class StringListFrame(Gtk.Box):
    """
    A vertical panel with a blue header (section title) and a monospace list.
    Methods:
      - set_lines(list[str])
      - set_points(curve_like or list[(t, mag, snr)])
    """
    def __init__(self, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _install_theme_css()
        self.add_css_class("lemon-surface")

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.add_css_class("section-header")
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.add_css_class("monospace")
        lbl.set_hexpand(True)
        hdr.append(lbl)
        self.append(hdr)

        # Framed list
        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame_box.add_css_class("section-frame")
        frame_box.add_css_class("lemon-surface")

        self._store: Gio.ListStore = Gio.ListStore.new(Gtk.StringObject)

        factory = Gtk.SignalListItemFactory()
        def setup(_f, li: Gtk.ListItem):
            l = Gtk.Label(xalign=0.0)
            l.add_css_class("monospace")
            l.add_css_class("lemon-surface")
            l.set_selectable(True)
            li.set_child(l)
        def bind(_f, li: Gtk.ListItem):
            it = li.get_item()
            l: Gtk.Label = li.get_child()  # type: ignore[assignment]
            l.set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
        factory.connect("setup", setup)
        factory.connect("bind", bind)

        sel = Gtk.NoSelection.new(self._store)
        view = Gtk.ListView.new(sel, factory)
        view.add_css_class("lemon-surface")

        sc = Gtk.ScrolledWindow.new()
        sc.set_child(view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True); sc.set_vexpand(True)

        frame_box.append(sc)
