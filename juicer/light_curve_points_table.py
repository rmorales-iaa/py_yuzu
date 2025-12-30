# juicer/light_curve_points_table.py
from __future__ import annotations
from typing import Iterable, Optional, Sequence, Tuple, Union, Dict, Any
import math
import datetime as _dt

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GObject", "2.0")
from gi.repository import Gtk, Gdk, Gio, GObject  # type: ignore

# ---- configuration (theme-friendly fallbacks)
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

_BG        = _cfg_get("theme", "background", "#2a2f38")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_BLUE_HDR  = _cfg_get("theme", "section_bg", "#1e3a8a")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")
_SEL_BG    = _cfg_get("theme", "sel_row_bg", _cfg_get("theme", "accent_ok", "#0b3d2e"))
_SEL_FG    = _cfg_get("theme", "sel_row_fg", "#ffffff")

# Use the same palette as the comparison table
_HDR_BG     = _cfg_get("curve_points_table", "header_bg",  "#b85c00")
_HDR_FG     = _cfg_get("curve_points_table", "header_fg",  "#ffffff")
_GRID_COLOR = _cfg_get("cmp_table", "grid_color", "#c9a000")
_ROW_HEIGHT = _cfg_getfloat("cmp_table", "row_height", 26.0)

def _install_css() -> None:
    try:
        css = f"""
        .lemon-surface {{ background: {_BG}; color: {_FG}; }}
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
            padding: 0;
        }}

        /* ===== ColumnView header (shared with comparison table) ===== */
        .lemon-cv > header,
        .lemon-cv > header > * {{
            background: {_HDR_BG};
            color: {_HDR_FG};
        }}
        .lemon-cv columnviewcolumnheader > button,
        .lemon-cv columnviewcolumnheader > button > * {{
            background: {_HDR_BG};
            color: {_HDR_FG};
        }}
        .lemon-cv > header {{
            border-bottom: 1px solid {_GRID_COLOR};
        }}
        .lemon-cv columnviewcolumnheader > button {{
            border-right: 1px solid {_GRID_COLOR};
            padding: 6px 8px;
        }}
        .lemon-cv columnviewcolumnheader:last-child > button {{
            border-right: 1px solid {_GRID_COLOR};
        }}

        /* ===== ROWS: theme + grid + selection ===== */
        .lemon-cv row {{
            background: {_BG};
            color: {_FG};
        }}
        .cstars-row {{
            border-bottom: 1px solid {_GRID_COLOR};
            min-height: {_ROW_HEIGHT}px;
        }}
        .cstars-cell {{
            background: transparent;
            color: {_FG};
            padding: 6px 8px;
            border-left: 1px solid {_GRID_COLOR};
        }}
        .cstars-row .cstars-cell:last-child {{
            border-right: 1px solid {_GRID_COLOR};
        }}

        /* Selected row: dark green like window.py */
        .lemon-cv row:selected, .lemon-cv row:focus:selected {{
            background: {_SEL_BG};
        }}
        .lemon-cv row:selected * {{
            color: {_SEL_FG};
        }}
        """
        prov = Gtk.CssProvider()
        prov.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

# ---------- helpers ----------
def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
        if math.isfinite(f):
            return f
    except Exception:
        pass
    return None

def _fmt_time(unix: Union[str, float, int]) -> str:
    if isinstance(unix, str):
        return unix
    try:
        return _dt.datetime.utcfromtimestamp(float(unix)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            return f"{float(unix):.6f}"
        except Exception:
            return str(unix)

# ---------- row model ----------
class PointsRow(GObject.GObject):
    """Visible strings + hidden numeric values for sorting."""
    date  = GObject.Property(type=str,   default="")
    mag   = GObject.Property(type=str,   default="")
    snr   = GObject.Property(type=str,   default="")

    date_v = GObject.Property(type=float, default=0.0)   # epoch seconds
    mag_v  = GObject.Property(type=float, default=0.0)
    snr_v  = GObject.Property(type=float, default=0.0)

    def __init__(self,
                 epoch: Optional[float],
                 mag:   Optional[float],
                 snr:   Optional[float],
                 m_fmt: str = "{:.6f}",
                 s_fmt: str = "{:.3f}") -> None:
        super().__init__()
        # visible
        self.props.date = _fmt_time(epoch if epoch is not None else "")
        self.props.mag  = (m_fmt.format(mag) if _to_float(mag) is not None else "")
        self.props.snr  = (s_fmt.format(snr) if _to_float(snr) is not None else "")

        # numeric (safe 0.0 fallbacks)
        self.props.date_v = (_to_float(epoch) or 0.0) if epoch is not None else 0.0
        self.props.mag_v  = _to_float(mag) or 0.0
        self.props.snr_v  = _to_float(snr) or 0.0

# ---------- table ----------
class CurvePointsTable(Gtk.Box):
    """
    A themed, sortable, resizable ColumnView showing:
        Date (UTC string) | mag | snr

    set_rows(rows):
        Accepts any of:
          - (t, mag, snr)
          - (index, t, mag, snr)  # index ignored
          - dicts with keys: t/epoch/utc/ts, mag, snr
    """
    _COLS: Sequence[str] = ("Date (UTC)", "mag", "snr")

    def __init__(self, title: str = "Light curve data points") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _install_css()
        self.add_css_class("lemon-surface")

        # Header
        header_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_bar.add_css_class("section-header")
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.add_css_class("monospace")
        title_lbl.set_hexpand(True)
        header_bar.append(title_lbl)
        self.append(header_bar)

        # Frame
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("section-frame")
        frame.add_css_class("lemon-surface")
        self.append(frame)

        # Model pipeline
        self._store: Gio.ListStore = Gio.ListStore.new(PointsRow)
        self._sort_model: Gtk.SortListModel = Gtk.SortListModel(model=self._store, sorter=None)  # type: ignore
        self._selection: Gtk.SingleSelection = Gtk.SingleSelection.new(self._sort_model)  # type: ignore
        self._selection.set_can_unselect(True)
        self._selection.set_autoselect(False)
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

        # View
        self._view: Gtk.ColumnView = Gtk.ColumnView(model=self._selection)  # type: ignore
        self._view.add_css_class("columnview")
        self._view.add_css_class("lemon-cv")
        self._view.set_vexpand(True)

        self._col_map: Dict[str, Gtk.ColumnViewColumn] = {}

        def make_column(title: str, key: str, align_right: bool = True) -> Gtk.ColumnViewColumn:
            factory = Gtk.SignalListItemFactory()

            def setup(_f, li: Gtk.ListItem):
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                box.set_homogeneous(True)
                box.add_css_class("cstars-row")
                lbl = Gtk.Label(xalign=(1.0 if align_right else 0.0))
                lbl.add_css_class("cstars-cell")
                lbl.add_css_class("monospace")
                lbl.set_hexpand(True)
                lbl.set_selectable(True)
                box.append(lbl)
                li.set_child(box)

            def bind(_f, li: Gtk.ListItem):
                obj: Optional[PointsRow] = li.get_item()  # type: ignore
                box: Gtk.Box = li.get_child()  # type: ignore
                lbl: Gtk.Label = box.get_first_child()  # type: ignore
                if obj is None:
                    lbl.set_label("")
                    return
                if key == "date":
                    lbl.set_label(obj.props.date)
                elif key == "mag":
                    lbl.set_label(obj.props.mag)
                elif key == "snr":
                    lbl.set_label(obj.props.snr)

            factory.connect("setup", setup)
            factory.connect("bind", bind)

            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(True)

            def cmp(a: Optional[PointsRow], b: Optional[PointsRow], _ud: object) -> Gtk.Ordering:
                av = 0.0; bv = 0.0
                if key == "date":
                    av = 0.0 if a is None else float(a.props.date_v)
                    bv = 0.0 if b is None else float(b.props.date_v)
                elif key == "mag":
                    av = 0.0 if a is None else float(a.props.mag_v)
                    bv = 0.0 if b is None else float(b.props.mag_v)
                elif key == "snr":
                    av = 0.0 if a is None else float(a.props.snr_v)
                    bv = 0.0 if b is None else float(b.props.snr_v)
                if av < bv:
                    return Gtk.Ordering.SMALLER
                if av > bv:
                    return Gtk.Ordering.LARGER
                return Gtk.Ordering.EQUAL

            sorter = Gtk.CustomSorter.new(cmp, None)
            col.set_sorter(sorter)
            return col

        # Columns: Date | mag | snr
        c_date = make_column("Date (UTC)", "date", align_right=False)
        c_mag  = make_column("mag", "mag", align_right=True)
        c_snr  = make_column("snr", "snr", align_right=True)

        self._col_map = {"date": c_date, "mag": c_mag, "snr": c_snr}
        self._view.append_column(c_date)
        self._view.append_column(c_mag)
        self._view.append_column(c_snr)

        # Hook sorter & default sort by date ASC
        self._sort_model.set_sorter(self._view.get_sorter())  # type: ignore
        try:
            self._view.sort_by_column(c_date, Gtk.SortType.ASCENDING)  # type: ignore
        except Exception:
            pass

        sc = Gtk.ScrolledWindow.new()
        sc.set_child(self._view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        frame.append(sc)

    # -------- public API --------
    def clear(self) -> None:
        self._store.splice(0, self._store.get_n_items(), [])
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def set_rows(
        self,
        rows: Iterable[
            Union[
                Tuple[float, float, Optional[float]],                # (t, mag, snr)
                Tuple[int, float, float, Optional[float]],           # (idx, t, mag, snr) - idx ignored
                Dict[str, Any],                                      # {'t'|'epoch'|'utc'|'ts', 'mag', 'snr'}
            ]
        ]
    ) -> None:
        out: list[PointsRow] = []

        def row_from_dict(d: Dict[str, Any]) -> Optional[PointsRow]:
            t = d.get("t", d.get("epoch", d.get("utc", d.get("ts"))))
            m = d.get("mag")
            s = d.get("snr")
            tf = _to_float(t)
            mf = _to_float(m)
            sf = _to_float(s)
            return PointsRow(tf, mf, sf)

        for it in rows:
            try:
                if isinstance(it, dict):
                    out.append(row_from_dict(it))  # type: ignore[arg-type]
                else:
                    # tuples
                    if len(it) == 3:
                        t, m, s = it  # type: ignore[misc]
                    else:
                        _idx, t, m, s = it  # type: ignore[misc]
                    out.append(PointsRow(_to_float(t), _to_float(m), _to_float(s)))
            except Exception:
                continue

        self._store.splice(0, self._store.get_n_items(), out)
        self._sort_model.set_sorter(self._view.get_sorter())  # type: ignore
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def sort_by(self, key: str, desc: bool = False) -> None:
        col = self._col_map.get(key)
        if not col:
            return
        try:
            self._view.sort_by_column(col, Gtk.SortType.DESCENDING if desc else Gtk.SortType.ASCENDING)  # type: ignore
        except Exception:
            pass
