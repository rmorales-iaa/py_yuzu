# juicer/curve_points_table.py
from __future__ import annotations
from typing import Iterable, Optional, Tuple, Union
import datetime as _dt

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gtk, Gdk, Gio, GObject  # type: ignore

# ---- configuration
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

# Base theme
_BG        = _cfg_get("theme", "background", "#2a2f38")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_BLUE_HDR  = _cfg_get("theme", "blue",       "#0d6efd")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

# Table section (NEW)
_HDR_BG     = _cfg_get("curve_points_table", "header_bg",  "#b85c00")
_HDR_FG     = _cfg_get("curve_points_table", "header_fg",  "#ffffff")
_GRID_COLOR = _cfg_get("curve_points_table", "grid_color", "#c9a000")
_ROW_HEIGHT = _cfg_getfloat("curve_points_table", "row_height", 26.0)
_DATE_FMT   = _cfg_get("curve_points_table", "date_format", "%Y-%m-%d %H:%M:%S")
_MAG_DEC    = int(_cfg_getfloat("curve_points_table", "mag_decimals", 4.0))
_SNR_DEC    = int(_cfg_getfloat("curve_points_table", "snr_decimals", 1.0))

def _install_css() -> None:
    try:
        css = f"""
        .lemon-surface {{ background: {_BG}; color: {_FG}; }}
        .monospace {{ font-family: {_MONO_FONT}; }}

        /* Blue section header */
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

        /* Table header (dark orange) */
        .cpts-th {{
            background: {_HDR_BG};
            color: {_HDR_FG};
            padding: 6px 8px;
            font-weight: 600;
            border: 1px solid {_GRID_COLOR};
            border-bottom: none;
        }}

        /* Row cells with visible grid */
        .cpts-cell {{
            padding: 4px 8px;
            border-left: 1px solid {_GRID_COLOR};
            border-bottom: 1px solid {_GRID_COLOR};
        }}
        .cpts-row {{ min-height: {_ROW_HEIGHT}px; }}
        """
        prov = Gtk.CssProvider()
        prov.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

class CurveRow(GObject.GObject):
    idx = GObject.Property(type=int, default=0)
    date = GObject.Property(type=str, default="")
    magnitude = GObject.Property(type=str, default="")
    snr = GObject.Property(type=str, default="")

    def __init__(self, idx: int, date_str: str, mag_str: str, snr_str: str) -> None:
        super().__init__()
        self.props.idx = int(idx)
        self.props.date = date_str
        self.props.magnitude = mag_str
        self.props.snr = snr_str

class CurvePointsTable(Gtk.Box):
    """
    4-column table in a framed section with blue title:
      ID | date | magnitude | SNR
    API:
        clear()
        set_rows(rows)  # rows: Iterable[(idx, t, mag, snr)]
                        #   t: epoch seconds (float/int) or str
    """
    def __init__(self, title: str = "Data points") -> None:
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
            lbl.add_css_class("cpts-th")
            lbl.add_css_class("monospace")
            lbl.set_hexpand(True)
            return lbl

        t_header.append(_hdr("ID"))
        t_header.append(_hdr("date"))
        t_header.append(_hdr("magnitude"))
        t_header.append(_hdr("SNR"))
        frame_box.append(t_header)

        # Model & list
        self._store: Gio.ListStore = Gio.ListStore.new(CurveRow)

        factory = Gtk.SignalListItemFactory()

        def setup(_f, li: Gtk.ListItem):
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
            row_box.set_homogeneous(True)
            row_box.add_css_class("cpts-row")
            l_id = Gtk.Label(xalign=1.0)
            l_dt = Gtk.Label(xalign=0.0)
            l_mg = Gtk.Label(xalign=1.0)
            l_sn = Gtk.Label(xalign=1.0)
            for lbl in (l_id, l_dt, l_mg, l_sn):
                lbl.add_css_class("cpts-cell")
                lbl.add_css_class("monospace")
                lbl.set_hexpand(True)
                lbl.set_selectable(True)
            row_box.append(l_id); row_box.append(l_dt); row_box.append(l_mg); row_box.append(l_sn)
            li.set_child(row_box)

        def bind(_f, li: Gtk.ListItem):
            obj: CurveRow = li.get_item()  # type: ignore
            row_box: Gtk.Box = li.get_child()  # type: ignore
            l_id = row_box.get_first_child()
            l_dt = l_id.get_next_sibling() if l_id else None
            l_mg = l_dt.get_next_sibling() if l_dt else None
            l_sn = l_mg.get_next_sibling() if l_mg else None

            if l_id: l_id.set_label(f"{obj.props.idx:d}")
            if l_dt: l_dt.set_label(obj.props.date)
            if l_mg: l_mg.set_label(obj.props.magnitude)
            if l_sn: l_sn.set_label(obj.props.snr)

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

    def _fmt_time(self, t: Union[str, float, int]) -> str:
        if isinstance(t, str):
            return t
        try:
            return _dt.datetime.utcfromtimestamp(float(t)).strftime(_DATE_FMT)
        except Exception:
            return f"{float(t):.6f}"

    def set_rows(self, rows: Iterable[Tuple[int, Union[str, float, int], float, Optional[float]]]) -> None:
        self.clear()
        for idx, t, mag, snr in rows:
            dt = self._fmt_time(t)
            try:
                mag_str = f"{float(mag):.{int(_MAG_DEC)}f}"
            except Exception:
                mag_str = str(mag)
            if snr is None:
                snr_str = "—"
            else:
                try:
                    snr_str = f"{float(snr):.{int(_SNR_DEC)}f}"
                except Exception:
                    snr_str = str(snr)
            self._store.append(CurveRow(int(idx), dt, mag_str, snr_str))
