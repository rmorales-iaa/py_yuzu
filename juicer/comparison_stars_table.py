# juicer/comparison_stars_table.py
from __future__ import annotations
from typing import Iterable, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gtk, Gdk, Gio, GObject  # type: ignore

# -------- configuration --------
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

def _cfg_getfloat(section: str, key: str, default: float) -> float:
    try:
        return float(cfg.get(section, key))  # type: ignore[attr-defined]
    except Exception:
        try:
            return cfg.getfloat(section, key, default)  # type: ignore[attr-defined]
        except Exception:
            return default

# Theme
_BG         = _cfg_get("theme", "background", "#2a2f38")
_FG         = _cfg_get("theme", "foreground", "#e7ebef")
_BLUE_HDR   = _cfg_get("theme", "blue",       "#0d6efd")
_MONO_FONT  = _cfg_get("theme", "mono_font",
                       "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

# Panel-specific
_HDR_BG     = _cfg_get("reference_table", "header_bg",  "#b85c00")  # dark orange
_HDR_FG     = _cfg_get("reference_table", "header_fg",  "#ffffff")
_GRID_COLOR = _cfg_get("reference_table", "grid_color", "#c9a000")  # dark yellow
_ROW_HEIGHT = _cfg_getfloat("reference_table", "row_height", 26.0)

def _install_css() -> None:
    try:
        css = f"""
        .lemon-surface {{ background: {_BG}; color: {_FG}; }}
        .monospace {{ font-family: {_MONO_FONT}; }}

        /* Blue section header (same look as Star info) */
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
            padding: 0;
        }}

        /* Table header strip (dark orange) */
        .refs-th {{
            background: {_HDR_BG};
            color: {_HDR_FG};
            padding: 6px 8px;
            font-weight: 600;
            border: 1px solid {_GRID_COLOR};
            border-bottom: none;
        }}

        /* Row cells with dark-yellow dividers */
        .refs-cell {{
            padding: 4px 8px;
            border-left: 1px solid {_GRID_COLOR};
            border-bottom: 1px solid {_GRID_COLOR};
        }}
        .refs-row {{ min-height: {_ROW_HEIGHT}px; }}
        """
        prov = Gtk.CssProvider()
        prov.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

# -------- model row --------
class RefRow(GObject.GObject):
    id = GObject.Property(type=int, default=0)
    weight = GObject.Property(type=float, default=0.0)
    sigma = GObject.Property(type=float, default=0.0)
    has_stats = GObject.Property(type=bool, default=True)

    def __init__(self, sid: int, weight: Optional[float], sigma: Optional[float]) -> None:
        super().__init__()
        self.props.id = int(sid)
        if weight is None or sigma is None:
            self.props.has_stats = False
            self.props.weight = 0.0
            self.props.sigma = 0.0
        else:
            self.props.has_stats = True
            self.props.weight = float(weight)
            self.props.sigma = float(sigma)

# -------- table widget --------
class ReferenceStarsTable(Gtk.Box):
    """
    3-column table (id | weight | sigma) wrapped in a framed section
    with a blue title bar.
    API:
        clear()
        set_rows(rows)  # rows: Iterable[(id, weight|None, sigma|None)]
    """

    def __init__(self, title: str = "Comparison stars") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _install_css()
        self.add_css_class("lemon-surface")

        # Section header (blue)
        header_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_bar.add_css_class("section-header")
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.add_css_class("monospace")
        title_lbl.set_hexpand(True)
        header_bar.append(title_lbl)
        self.append(header_bar)

        # Framed box
        frame_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame_box.add_css_class("section-frame")
        frame_box.add_css_class("lemon-surface")
        self.append(frame_box)

        # Dark-orange table header
        t_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        t_header.set_homogeneous(True)

        def _hdr(text: str) -> Gtk.Label:
            lbl = Gtk.Label(label=text, xalign=0.5)
            lbl.add_css_class("refs-th")
            lbl.add_css_class("monospace")
            lbl.set_hexpand(True)
            return lbl

        t_header.append(_hdr("id"))
        t_header.append(_hdr("weight"))
        t_header.append(_hdr("sigma"))
        frame_box.append(t_header)

        # Model & list view
        self._store: Gio.ListStore = Gio.ListStore.new(RefRow)

        factory = Gtk.SignalListItemFactory()

        def setup(_f, li: Gtk.ListItem):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row_box.set_homogeneous(True)
            row_box.add_css_class("refs-row")
            l_id = Gtk.Label(xalign=1.0)
            l_w  = Gtk.Label(xalign=1.0)
            l_s  = Gtk.Label(xalign=1.0)
            for lbl in (l_id, l_w, l_s):
                lbl.add_css_class("refs-cell")
                lbl.add_css_class("monospace")
                lbl.set_hexpand(True)
                lbl.set_selectable(True)
            row_box.append(l_id); row_box.append(l_w); row_box.append(l_s)
            li.set_child(row_box)

        def bind(_f, li: Gtk.ListItem):
            obj: RefRow = li.get_item()  # type: ignore
            row_box: Gtk.Box = li.get_child()  # type: ignore
            l_id = row_box.get_first_child()
            l_w  = l_id.get_next_sibling() if l_id else None
            l_s  = l_w.get_next_sibling()  if l_w else None

            if l_id: l_id.set_label(f"{obj.props.id:d}")
            if obj.props.has_stats:
                if l_w: l_w.set_label(f"{obj.props.weight:.4f}")
                if l_s: l_s.set_label(f"{obj.props.sigma:.4f}")
            else:
                if l_w: l_w.set_label("—")
                if l_s: l_s.set_label("—")

        factory.connect("setup", setup)
        factory.connect("bind", bind)

        selection = Gtk.NoSelection.new(self._store)
        list_view = Gtk.ListView.new(selection, factory)
        list_view.add_css_class("lemon-surface")
        list_view.set_vexpand(True)

        sc = Gtk.ScrolledWindow.new()
        sc.set_child(list_view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True); sc.set_vexpand(True)

        frame_box.append(sc)

    # ---- API
    def clear(self) -> None:
        self._store.splice(0, self._store.get_n_items(), [])

    def set_rows(self, rows: Iterable[Tuple[int, Optional[float], Optional[float]]]) -> None:
        self.clear()
        for sid, w, s in rows:
            self._store.append(RefRow(int(sid), w, s))
