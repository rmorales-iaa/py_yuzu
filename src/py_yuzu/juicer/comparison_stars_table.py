# juicer/comparison_stars_table.py
from __future__ import annotations
from typing import Iterable, Optional, Sequence, Tuple, Union, Dict, Any, Callable
import math

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
# Selection (same dark green used elsewhere in your app; overridable via config)
_SEL_BG    = _cfg_get("theme", "sel_row_bg", _cfg_get("theme", "accent_ok", "#0b3d2e"))
_SEL_FG    = _cfg_get("theme", "sel_row_fg", "#ffffff")
_BLUE_HDR  = _cfg_get("theme", "blue",       "#1e3a8a")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

# Comparison table theme (header uses curve_points_table to match the rest of UI)
_HDR_BG     = _cfg_get("curve_points_table", "header_bg",  "#b85c00")
_HDR_FG     = _cfg_get("curve_points_table", "header_fg",  "#ffffff")
_GRID_COLOR = _cfg_get("cmp_table", "grid_color", "#c9a000")  # dark yellow, like window.py
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

        /* ===== ColumnView header (themed) ===== */
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

        /* ===== ROWS: theme + grid ===== */
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

        /* ===== Selection styling: dark green row + white text ===== */
        .lemon-cv row:selected,
        .lemon-cv row:selected:focus,
        .lemon-cv listview row:selected,
        .lemon-cv listview row:selected:focus {{
            background: {_SEL_BG};
        }}
        .lemon-cv row:selected .cstars-cell,
        .lemon-cv row:selected:focus .cstars-cell,
        .lemon-cv listview row:selected .cstars-cell,
        .lemon-cv listview row:selected:focus .cstars-cell {{
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


# -----------------------------------------------------------------------------
# Row model
# -----------------------------------------------------------------------------

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

class CompRow(GObject.GObject):
    """Visible strings for labels + hidden numeric values for sorting."""
    # Visible
    star   = GObject.Property(type=int, default=0)     # "Star"
    weight = GObject.Property(type=str, default="")
    sigma  = GObject.Property(type=str, default="")
    mag    = GObject.Property(type=str, default="")
    stdev  = GObject.Property(type=str, default="")

    # Hidden numeric values (never inf/nan)
    weight_v = GObject.Property(type=float, default=0.0)
    sigma_v  = GObject.Property(type=float, default=0.0)
    mag_v    = GObject.Property(type=float, default=0.0)
    stdev_v  = GObject.Property(type=float, default=0.0)

    def __init__(
        self,
        star: int,
        weight_val: Optional[float],
        sigma_val: Optional[float],
        mag_val: Optional[float],
        stdev_val: Optional[float],
        w_fmt: str = "{:.6f}",
        s_fmt: str = "{:.6f}",
        m_fmt: str = "{:.3f}",
        sd_fmt: str = "{:.6f}",
    ) -> None:
        super().__init__()
        self.props.star = int(star)

        def _txt(v, fmt):
            f = _to_float(v)
            return fmt.format(f) if f is not None else ""

        def _num(v):
            f = _to_float(v)
            return f if f is not None else 0.0

        self.props.weight = _txt(weight_val, w_fmt)
        self.props.sigma  = _txt(sigma_val,  s_fmt)
        self.props.mag    = _txt(mag_val,    m_fmt)
        self.props.stdev  = _txt(stdev_val,  sd_fmt)

        self.props.weight_v = _num(weight_val)
        self.props.sigma_v  = _num(sigma_val)
        self.props.mag_v    = _num(mag_val)
        self.props.stdev_v  = _num(stdev_val)

# -----------------------------------------------------------------------------
# Table widget
# -----------------------------------------------------------------------------

class ReferenceStarsTable(Gtk.Box):
    _COLS: Sequence[str] = ("Star", "weight", "sigma", "mag", "stdev")

    def __init__(self, title: str = "Comparison stars") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        _install_css()
        self.add_css_class("lemon-surface")

        self._mag_lookup: Optional[Callable[[int], Optional[float]]] = None

        # --- Header (blue section title)
        header_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_bar.add_css_class("section-header")
        title_lbl = Gtk.Label(label=title, xalign=0.0)
        title_lbl.add_css_class("monospace")
        title_lbl.set_hexpand(True)
        header_bar.append(title_lbl)
        self.append(header_bar)

        # --- Frame
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        frame.add_css_class("section-frame")
        frame.add_css_class("lemon-surface")
        self.append(frame)

        # --- Model pipeline: store -> sortmodel -> selection -> ColumnView
        self._store: Gio.ListStore = Gio.ListStore.new(CompRow)
        self._sort_model: Gtk.SortListModel = Gtk.SortListModel(model=self._store, sorter=None)  # type: ignore
        self._selection: Gtk.SingleSelection = Gtk.SingleSelection.new(self._sort_model)  # type: ignore
        self._selection.set_can_unselect(True)
        self._selection.set_autoselect(False)
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

        self._view: Gtk.ColumnView = Gtk.ColumnView(model=self._selection)  # type: ignore
        self._view.add_css_class("columnview")
        self._view.add_css_class("lemon-cv")  # <- hook CSS to theme header & rows
        self._view.set_vexpand(True)

        # Build columns (resizable + sortable)
        self._col_map: Dict[str, Gtk.ColumnViewColumn] = {}

        def make_column(title: str, key: str, attr: str) -> Gtk.ColumnViewColumn:
            factory = Gtk.SignalListItemFactory()

            def setup(_f, li: Gtk.ListItem):
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                box.set_homogeneous(True)
                box.add_css_class("cstars-row")
                lbl = Gtk.Label(xalign=1.0)
                lbl.add_css_class("cstars-cell")
                lbl.add_css_class("monospace")
                lbl.set_hexpand(True)
                lbl.set_selectable(True)
                box.append(lbl)
                li.set_child(box)

            def bind(_f, li: Gtk.ListItem):
                obj: Optional[CompRow] = li.get_item()  # type: ignore
                box: Gtk.Box = li.get_child()  # type: ignore
                lbl: Gtk.Label = box.get_first_child()  # type: ignore
                if obj is None:
                    lbl.set_label("")
                    return
                if key == "star":
                    lbl.set_label(str(int(obj.props.star)))
                else:
                    lbl.set_label(str(getattr(obj.props, attr)))

            factory.connect("setup", setup)
            factory.connect("bind", bind)

            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(True)

            # Per-column sorter (click header toggles asc/desc)
            def cmp(a: Optional[CompRow], b: Optional[CompRow], _ud: object) -> Gtk.Ordering:
                va = self._value_for_sort(a, key)
                vb = self._value_for_sort(b, key)
                if va < vb:
                    return Gtk.Ordering.SMALLER
                if va > vb:
                    return Gtk.Ordering.LARGER
                return Gtk.Ordering.EQUAL

            sorter = Gtk.CustomSorter.new(cmp, None)
            col.set_sorter(sorter)
            return col

        cols = [
            ("Star",   "star",   "star"),
            ("weight", "weight", "weight"),
            ("sigma",  "sigma",  "sigma"),
            ("mag",    "mag",    "mag"),
            ("stdev",  "stdev",  "stdev"),
        ]
        for title, key, attr in cols:
            c = make_column(title, key, (attr if key == "star" else f"{attr}"))
            self._col_map[key] = c
            self._view.append_column(c)

        # Attach ColumnView's sorter so clicks work; default sort by weight DESC
        self._sort_model.set_sorter(self._view.get_sorter())  # type: ignore
        try:
            self._view.sort_by_column(self._col_map["weight"], Gtk.SortType.DESCENDING)  # type: ignore
        except Exception:
            pass

        sc = Gtk.ScrolledWindow.new()
        sc.set_child(self._view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        frame.append(sc)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _value_for_sort(self, obj: Optional[CompRow], key: str) -> float:
        if obj is None:
            return 0.0
        if key == "star":
            return float(int(obj.props.star))
        if key == "weight":
            return float(obj.props.weight_v)
        if key == "sigma":
            return float(obj.props.sigma_v)
        if key == "mag":
            return float(obj.props.mag_v)
        if key == "stdev":
            return float(obj.props.stdev_v)
        return 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_mag_lookup(self, fn: Optional[Callable[[int], Optional[float]]]) -> None:
        """Optional hook: if mag is missing in rows, call fn(star_id)->mag."""
        self._mag_lookup = fn

    def clear(self) -> None:
        self._store.splice(0, self._store.get_n_items(), [])
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def set_rows(
        self,
        rows: Iterable[
            Union[
                Tuple[int, Optional[float], Optional[float], Optional[float], Optional[float]],
                Dict[str, Any],
            ]
        ]
    ) -> None:
        """
        Accepts:
          - tuples: (cstar_id, weight, sigma[, mag[, stdev]])
          - dicts:  flexible keys:
                    star  := cstar_id | star_id | id
                    weight:= weight
                    sigma := sigma | stdev
                    mag   := imag | mag | i_mag | inst_mag | mag_i | mag_inst
                    stdev := stdev | sigma
        """
        out: list[CompRow] = []

        MAG_KEYS = ("imag", "mag", "i_mag", "inst_mag", "mag_i", "mag_inst")
        SIG_KEYS = ("sigma", "stdev")

        def pick_num(d: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
            for k in keys:
                if k in d:
                    f = _to_float(d.get(k))
                    if f is not None:
                        return f
            return None

        def _to_row(item) -> Optional[CompRow]:
            if isinstance(item, dict):
                star   = int(item.get("cstar_id") or item.get("star_id") or item.get("id") or 0)
                weight = _to_float(item.get("weight"))
                sigma  = pick_num(item, SIG_KEYS)
                mag    = pick_num(item, MAG_KEYS)
                stdev  = _to_float(item.get("stdev"))
                if stdev is None:
                    stdev = sigma
            else:
                try:
                    star  = int(item[0])
                    weight= _to_float(item[1])
                    sigma = _to_float(item[2])
                    mag   = _to_float(item[3]) if len(item) >= 4 else None
                    stdev = _to_float(item[4]) if len(item) >= 5 else sigma
                except Exception:
                    return None

            if (mag is None) and self._mag_lookup:
                try:
                    m = self._mag_lookup(int(star))
                    if m is not None:
                        fm = _to_float(m)
                        if fm is not None:
                            mag = fm
                except Exception:
                    pass

            return CompRow(
                star=star,
                weight_val=weight,
                sigma_val=sigma,
                mag_val=mag,
                stdev_val=stdev,
            )

        for it in rows:
            r = _to_row(it)
            if r is not None:
                out.append(r)

        self._store.splice(0, self._store.get_n_items(), out)
        self._sort_model.set_sorter(self._view.get_sorter())  # type: ignore
        self._selection.set_selected(Gtk.INVALID_LIST_POSITION)

    def sort_by(self, key: str, desc: bool = False) -> None:
        col = self._col_map.get(key)
        if not col:
            return
        try:
            self._view.sort_by_column(
                col,
                Gtk.SortType.DESCENDING if desc else Gtk.SortType.ASCENDING  # type: ignore
            )
        except Exception:
            pass
