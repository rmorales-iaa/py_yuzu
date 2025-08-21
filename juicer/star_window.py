# juicer/star_window.py
from __future__ import annotations

import datetime as _dt
import io
import logging
import math
import sys
from typing import Any, Iterable, Optional, Sequence, Tuple, List

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GObject", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, Gdk, GObject, GLib  # type: ignore

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ---------------- Optional configuration manager ----------------
try:
    from conf_manager.conf_manager import cfg  # type: ignore
    _HAVE_CFG = True
except Exception:
    _HAVE_CFG = False

    class _CfgFallback:
        def get(self, section: str, key: str, default: str = "") -> str: return default
        def getint(self, section: str, key: str, default: int) -> int: return default
        def getfloat(self, section: str, key: str, default: float) -> float: return default
        def getbool(self, section: str, key: str, default: bool) -> bool: return default
        def attach_window(self, *a, **k): pass

    cfg = _CfgFallback()  # type: ignore

# StarRow model (id, ra, dec, imag) is provided by your app
try:
    from .models import StarRow  # type: ignore
except Exception:  # safe stub for typing / fallback
    class StarRow(GObject.GObject):  # type: ignore
        id = GObject.Property(type=int, default=-1)
        ra = GObject.Property(type=float, default=0.0)
        dec = GObject.Property(type=float, default=0.0)
        imag = GObject.Property(type=float, default=float("nan"))

# ---------------- Theme helpers ----------------
_BG   = cfg.get("theme", "background", "#2a2f38")
_FG   = cfg.get("theme", "foreground", "#e7ebef")
_ACC  = cfg.get("theme", "accent",     "#ffd84d")
_DIV  = cfg.get("theme", "divider",    "#ffd84d")

def _install_theme_css() -> None:
    """Install dark theme for widgets using `.lemon-surface` (idempotent)."""
    try:
        if getattr(_install_theme_css, "_done", False):
            return
        css = f"""
        .lemon-surface {{
            background-color: {_BG};
            color: {_FG};
        }}
        .lemon-surface label,
        .lemon-surface button,
        .lemon-surface spinbutton,
        .lemon-surface entry,
        .lemon-surface listview,
        .lemon-surface treeview,
        .lemon-surface textview,
        .lemon-surface frame > label {{
            color: {_FG};
        }}
        .lemon-surface scrolledwindow viewport,
        .lemon-surface scrolledwindow viewport > * {{
            background-color: {_BG};
        }}
        .lemon-surface paned > separator {{
            background-color: {_DIV};
            min-width: 2px;
            min-height: 2px;
        }}
        .lemon-surface frame {{
            border: 1px solid {_DIV};
            border-radius: 6px;
        }}
        .lemon-surface frame > label {{
            padding: 4px 6px;
        }}
        /* Make toolbar buttons inherit readable colors */
        .lemon-surface button, .lemon-surface button * {{
            color: {_FG};
            background-color: transparent;
        }}
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode("utf-8"))
        disp = Gdk.Display.get_default()
        if disp is not None:
            Gtk.StyleContext.add_provider_for_display(
                disp, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        _install_theme_css._done = True  # type: ignore[attr-defined]
    except Exception:
        pass

# ---------------- RA/Dec + time helpers ----------------
def _hms_from_deg(ra_deg: float) -> str:
    ra = (ra_deg / 15.0) % 24.0
    h = int(ra)
    m = int((ra - h) * 60.0)
    s = (ra - h - m / 60.0) * 3600.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def _dms_from_deg(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else "+"
    dabs = abs(dec_deg)
    d = int(dabs)
    m = int((dabs - d) * 60.0)
    s = (dabs - d - m / 60.0) * 3600.0
    return f"{sign}{d:02d}:{m:02d}:{s:05.2f}"

def _fmt_time(unix_time: float) -> str:
    try:
        return _dt.datetime.utcfromtimestamp(float(unix_time)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(unix_time)

def _format_mag(x: Optional[float]) -> str:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "—"
        return f"{float(x):.4f}"
    except Exception:
        return str(x)

# ---------------- Light curve helpers ----------------
def _curve_to_points(curve: Any) -> List[Tuple[float, float, Optional[float]]]:
    """Normalize LightCurve or iterable -> list of (t, mag, snr)."""
    if curve is None:
        return []
    # Common case: iterable yielding triples
    pts: List[Tuple[float, float, Optional[float]]] = []
    try:
        for item in curve:
            try:
                t, mag, snr = item  # type: ignore[misc]
                pts.append((float(t), float(mag), (None if snr is None else float(snr))))
            except Exception:
                # Unknown shape -> skip
                continue
        if pts:
            return pts
    except Exception:
        pass
    # Fallback: attribute holding data
    for attr in ("_data", "data"):
        try:
            seq = getattr(curve, attr)
            return _curve_to_points(seq)
        except Exception:
            continue
    return []

def _curve_cstars_with_weights(curve: Any) -> Optional[List[Tuple[int, float, float]]]:
    """
    Return list of (star_id, weight, stdev) if available on the curve.
    """
    # A) curve.weights() -> iterable of (id, weight, stdev)
    try:
        ws = list(getattr(curve, "weights")())
        if ws:
            out = []
            for sid, w, st in ws:
                try:
                    out.append((int(sid), float(w), float(st)))
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        pass
    # B) curve.cstars + curve.cweights + curve.cstdevs
    try:
        cstars = list(getattr(curve, "cstars"))
        wts    = list(getattr(curve, "cweights"))
        stds   = list(getattr(curve, "cstdevs"))
        if cstars and len(cstars) == len(wts) == len(stds):
            out = []
            for sid, w, st in zip(cstars, wts, stds):
                try:
                    out.append((int(sid), float(w), float(st)))
                except Exception:
                    continue
            if out:
                return out
    except Exception:
        pass
    return None

def _curve_cstars_ids_only(curve: Any) -> Optional[Iterable[int]]:
    try:
        ids = getattr(curve, "cstars", None)
        if ids:
            return list(int(x) for x in ids)
    except Exception:
        pass
    return None

def _iter_filters(miner: Any):
    """Yield candidate filters in a sensible order, avoiding duplicates."""
    seen = set()
    for name in ("selected_filter", "current_filter", "default_filter"):
        pf = getattr(miner, name, None)
        if pf is not None:
            key = pf if isinstance(pf, (int, str)) else id(pf)
            if key not in seen:
                seen.add(key)
                yield pf
    for pf in (getattr(miner, "pfilters", None) or []):
        key = pf if isinstance(pf, (int, str)) else id(pf)
        if key not in seen:
            seen.add(key)
            yield pf

def _collect_best_light_curve(miner: Any, star_id: int) -> Tuple[Any | None, Any | None]:
    """
    Try common miner APIs across likely filters; return the first non-empty curve.
    Returns (curve_or_None, filter_used_or_None).
    """
    method_names = ("get_light_curve", "get_curve", "light_curve_for_star",
                    "lightcurve_for_star", "light_curve")
    # Try candidate filters first
    for pf in _iter_filters(miner):
        for name in method_names:
            f = getattr(miner, name, None)
            if not callable(f):
                continue
            try:
                try:
                    curve = f(int(star_id), pf)
                except TypeError:
                    curve = f(int(star_id))
                if _curve_to_points(curve):
                    return curve, pf
            except Exception:
                continue
    # As a last resort, try without a filter
    for name in method_names:
        f = getattr(miner, name, None)
        if not callable(f):
            continue
        try:
            curve = f(int(star_id))
            if _curve_to_points(curve):
                return curve, None
        except Exception:
            continue
    return None, None

# ===================== Plot widget (Matplotlib) =====================
class LightCurveView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._fig = None
        self._canvas = None    # GTK4 canvas (if available)
        self._toolbar = None   # GTK4 toolbar (if available)
        self._picture = None   # Gtk.Picture for Agg fallback

        self._build()

    def _build(self) -> None:
        """Build area; prefer GTK4 backend, fallback to Agg -> Gtk.Picture."""
        try:
            from matplotlib.figure import Figure
            self._fig = Figure(figsize=(6, 4), tight_layout=True, facecolor=_BG)
        except Exception:
            self._fig = None

        have_canvas = False
        if self._fig is not None:
            try:
                from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4 as FigureCanvas
                from matplotlib.backends.backend_gtk4 import NavigationToolbar2GTK4 as Toolbar
                self._canvas = FigureCanvas(self._fig)
                self._canvas.set_hexpand(True); self._canvas.set_vexpand(True)
                self._toolbar = Toolbar(self._canvas)
                self._toolbar.set_hexpand(True)

                # Ensure the toolbar adopts the theme
                self._toolbar.add_css_class("lemon-surface")
                try:
                    child = self._toolbar.get_first_child()
                    while child is not None:
                        try:
                            child.add_css_class("lemon-surface")
                            if isinstance(child, Gtk.Button):
                                inner = child.get_child()
                                if isinstance(inner, Gtk.Widget):
                                    inner.add_css_class("lemon-surface")
                        except Exception:
                            pass
                        child = child.get_next_sibling()
                except Exception:
                    pass

                self.append(self._toolbar)
                self.append(self._canvas)
                have_canvas = True
            except Exception:
                self._canvas = None
                self._toolbar = None

        if not have_canvas:
            self._picture = Gtk.Picture.new()
            self._picture.set_hexpand(True); self._picture.set_vexpand(True)
            self.append(self._picture)

    def _style_axes(self, ax, *, title=None, xlab=None, ylab=None) -> None:
        ax.set_facecolor(_BG)
        ax.figure.set_facecolor(_BG)
        fg = _FG
        if title: ax.set_title(title, color=fg)
        if xlab:  ax.set_xlabel(xlab, color=fg)
        if ylab:  ax.set_ylabel(ylab, color=fg)
        ax.tick_params(colors=fg, labelsize=9)
        for sp in ax.spines.values():
            sp.set_color(_ACC)

    def plot_curve(self, points: Any,
                   *, star_id: Optional[int] = None, pfilter: Any | None = None) -> None:
        """Accepts a LightCurve (iterable) or a sequence of (t, mag, snr)."""
        if self._fig is None:
            if self._picture:
                self._picture.set_paintable(None)
            return
        try:
            trip = _curve_to_points(points)

            self._fig.clear()
            ax = self._fig.add_subplot(111)
            title = "Light curve"
            if star_id is not None:
                title += f" — Star {star_id}"
            if pfilter is not None:
                title += f" — {pfilter}"
            self._style_axes(ax, title=title, xlab="Date (UTC)", ylab="Δmag")

            if trip:
                try:
                    import matplotlib.dates as mdates
                    xs = [mdates.epoch2num(float(t)) for t, _, _ in trip]
                    ys = [float(m) for _, m, _ in trip]
                    ax.plot_date(xs, ys, linestyle="-", marker="o", markersize=3, color=_ACC)
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d\n%H:%M:%S"))
                    self._fig.autofmt_xdate()
                except Exception:
                    xs = [float(t) for t, _, _ in trip]
                    ys = [float(m) for _, m, _ in trip]
                    ax.plot(xs, ys, marker="o", linewidth=1.0, color=_ACC)
            else:
                ax.text(0.5, 0.5, "No curve points", ha="center", va="center",
                        transform=ax.transAxes, color=_FG)

            if self._canvas is not None:
                self._canvas.queue_draw()
            else:
                import matplotlib
                matplotlib.use("Agg")
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                buf = io.BytesIO()
                FigureCanvasAgg(self._fig).print_png(buf)
                data = buf.getvalue()
                texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))  # type: ignore
                self._picture.set_paintable(texture)
        except Exception as e:
            logger.warning("Plot error: %s", e)

# ===================== Info panel =====================
class StarInfoPanel(Gtk.Frame):
    __gtype_name__ = "StarInfoPanel"

    def __init__(self) -> None:
        super().__init__(label="Star info")
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        grid.set_margin_top(6); grid.set_margin_bottom(6)
        grid.set_margin_start(6); grid.set_margin_end(6)
        self.set_child(grid)

        self._labels: dict[str, Gtk.Label] = {}
        for i, (k, txt) in enumerate((
            ("id","ID"), ("ra","RA (hms)"), ("dec","Dec (dms)"), ("imag","Mag"),
            ("filter","Filter"), ("npts","Curve points"),
        )):
            k_lbl = Gtk.Label(label=f"{txt}:"); k_lbl.set_xalign(1.0); k_lbl.add_css_class("monospace")
            v_lbl = Gtk.Label(label="—");     v_lbl.set_xalign(0.0); v_lbl.add_css_class("monospace"); v_lbl.set_selectable(True)
            grid.attach(k_lbl, 0, i, 1, 1)
            grid.attach(v_lbl, 1, i, 1, 1)
            self._labels[k] = v_lbl

        # Actions
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._btn_chart = Gtk.Button(label="Finding chart")
        self._btn_simbad = Gtk.Button(label="Open in SIMBAD")
        box.append(self._btn_chart)
        box.append(self._btn_simbad)
        grid.attach(box, 0, 6, 2, 1)

        # Data mirrors
        self.star_id: int = -1
        self.ra_hms: str = ""
        self.dec_dms: str = ""
        self.field_name: str = ""

    def set(self, key: str, value: str) -> None:
        lbl = self._labels.get(key)
        if lbl: lbl.set_label(value)

    def populate_from(self, row: StarRow, miner: Any) -> None:
        try:
            sid = int(getattr(row.props, "id", -1))
        except Exception:
            sid = -1
        ra = float(getattr(row.props, "ra", 0.0))
        dec = float(getattr(row.props, "dec", 0.0))
        imag = getattr(row.props, "imag", None)

        self.star_id = sid
        self.ra_hms = _hms_from_deg(ra)
        self.dec_dms = _dms_from_deg(dec)
        self.field_name = getattr(miner, "field_name", "") or getattr(miner, "path", "")

        self.set("id", str(sid))
        self.set("ra", self.ra_hms)
        self.set("dec", self.dec_dms)
        self.set("imag", "" if (imag is None or (isinstance(imag,float) and math.isnan(imag))) else f"{float(imag):.3f}")

        # filter displayed later once chosen; but set a hint if available
        pf = None
        for name in ("selected_filter", "current_filter", "default_filter"):
            pf = getattr(miner, name, None)
            if pf: break
        self.set("filter", str(pf) if pf is not None else "—")

    # actions
    def on_chart_clicked(self, cb) -> None:
        self._btn_chart.connect("clicked", cb)

    def on_simbad_clicked(self, cb, ra_hms: str, dec_dms: str) -> None:
        self._btn_simbad.connect("clicked", cb, ra_hms, dec_dms)

# ===================== Reference stars =====================
class ReferenceStarsList(Gtk.Frame):
    def __init__(self) -> None:
        super().__init__(label="Reference stars")
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._store: Gio.ListStore = Gio.ListStore.new(Gtk.StringObject)

        factory = Gtk.SignalListItemFactory()
        def _setup(_f, li: Gtk.ListItem):
            lbl = Gtk.Label(xalign=0.0)
            lbl.add_css_class("monospace")
            lbl.set_selectable(True)
            li.set_child(lbl)
        def _bind(_f, li: Gtk.ListItem):
            it = li.get_item()
            li.get_child().set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
        factory.connect("setup", _setup)
        factory.connect("bind", _bind)

        sel = Gtk.NoSelection.new(self._store)
        view = Gtk.ListView.new(sel, factory)
        view.add_css_class("lemon-surface")
        sc = Gtk.ScrolledWindow.new()
        sc.set_child(view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True); sc.set_vexpand(True)
        self.set_child(sc)

    def set_from_curve_or_miner(self, curve: Any, miner: Any) -> None:
        self._store.splice(0, self._store.get_n_items(), [])

        # Prefer curve-provided (id, weight, stdev)
        w = _curve_cstars_with_weights(curve)
        if w:
            for sid, wt, st in w:
                self._store.append(Gtk.StringObject.new(f"{sid:6d}    weight={wt:.4f}    σ={st:.4f}"))
            return

        # Otherwise just IDs from curve/miner
        ids = _curve_cstars_ids_only(curve)
        if not ids:
            for attr in ("reference_star_ids", "refstars", "reference_stars"):
                ids = getattr(miner, attr, None)
                if ids:
                    break
            if not ids:
                for name in ("get_reference_stars", "get_refstars"):
                    f = getattr(miner, name, None)
                    if callable(f):
                        try:
                            ids = f()
                            break
                        except Exception:
                            pass

        for sid in (ids or []):
            try:
                self._store.append(Gtk.StringObject.new(f"{int(sid)}"))
            except Exception:
                continue

# ===================== Curve points =====================
class CurvePointsList(Gtk.Frame):
    def __init__(self) -> None:
        super().__init__(label="Curve points")
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._store: Gio.ListStore = Gio.ListStore.new(Gtk.StringObject)

        factory = Gtk.SignalListItemFactory()
        def _setup(_f, li: Gtk.ListItem):
            lbl = Gtk.Label(xalign=0.0)
            lbl.add_css_class("monospace")
            lbl.set_selectable(True)
            li.set_child(lbl)
        def _bind(_f, li: Gtk.ListItem):
            it = li.get_item()
            li.get_child().set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
        factory.connect("setup", _setup)
        factory.connect("bind", _bind)

        sel = Gtk.NoSelection.new(self._store)
        view = Gtk.ListView.new(sel, factory)
        view.add_css_class("lemon-surface")

        sc = Gtk.ScrolledWindow.new()
        sc.set_child(view)
        sc.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        sc.add_css_class("lemon-surface")
        sc.set_hexpand(True); sc.set_vexpand(True)
        self.set_child(sc)

    def set_points(self, points: Any) -> None:
        self._store.splice(0, self._store.get_n_items(), [])
        pts = _curve_to_points(points)
        if not pts:
            return
        items: List[str] = []
        for i, (t, mag, snr) in enumerate(pts, 1):
            snr_txt = "" if snr is None else f"{snr:.1f}"
            items.append(f"{i:03d}  {_fmt_time(t)}    mag={_format_mag(mag)}    SNR={snr_txt}")
        for line in items:
            self._store.append(Gtk.StringObject.new(line))

# ===================== Star details window =====================
class StarDetailsWindow(Gtk.Window):
    """
    Star details window:
      - LightCurveView (left/top)
      - StarInfoPanel  (right/top)
      - ReferenceStarsList (bottom-left)
      - CurvePointsList    (bottom-right)
    """
    def __init__(self, parent: Gtk.Window, row: StarRow, miner: Any) -> None:
        _install_theme_css()

        title = f"Star {int(getattr(row.props,'id',-1))}"
        super().__init__(transient_for=parent, modal=True, title=title)
        self.set_hide_on_close(True)

        # Geometry
        try:
            w = int(cfg.get("star_window","width"))      # type: ignore[arg-type]
            h = int(cfg.get("star_window","height"))     # type: ignore[arg-type]
        except Exception:
            w, h = 1000, 640
        self.set_default_size(w, h)

        self._ratio_top = _safe_getfloat("star_window","ratio_top", 0.60)
        self._ratio_plot = _safe_getfloat("star_window","ratio_plot", 0.62)
        self._ratio_bottom_left = _safe_getfloat("star_window","ratio_bottom_left", 0.50)

        try:
            cfg.attach_window(self, "star_window")  # persist geometry if supported
        except Exception:
            pass

        self._row = row
        self._miner = miner

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(8); root.set_margin_bottom(8)
        root.set_margin_start(8); root.set_margin_end(8)
        root.add_css_class("lemon-surface")
        self.set_child(root)

        # Vertical: (top) plot|info  /  (bottom) ref|curve
        self.vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned.set_wide_handle(True)
        self.vpaned.set_resize_start_child(True)
        self.vpaned.set_resize_end_child(True)
        self.vpaned.set_shrink_start_child(False)
        self.vpaned.set_shrink_end_child(False)
        root.append(self.vpaned)

        # Top split
        self.top_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_wide_handle(True)
        self.top_paned.set_resize_start_child(True)
        self.top_paned.set_resize_end_child(True)
        self.top_paned.set_shrink_start_child(False)
        self.top_paned.set_shrink_end_child(False)
        self.top_paned.set_hexpand(True); self.top_paned.set_vexpand(True)
        self.vpaned.set_start_child(self.top_paned)

        # Plot
        self.plot = LightCurveView()
        sc_plot = Gtk.ScrolledWindow.new()
        sc_plot.set_child(self.plot)
        sc_plot.set_hexpand(True); sc_plot.set_vexpand(True)
        sc_plot.add_css_class("lemon-surface")
        self.top_paned.set_start_child(sc_plot)

        # Info
        sc_info = Gtk.ScrolledWindow.new()
        sc_info.set_hexpand(True); sc_info.set_vexpand(True)
        sc_info.add_css_class("lemon-surface")
        self.info = StarInfoPanel()
        sc_info.set_child(self.info)
        self.top_paned.set_end_child(sc_info)

        # Bottom split
        self.bottom_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.bottom_paned.set_wide_handle(True)
        self.bottom_paned.set_resize_start_child(True)
        self.bottom_paned.set_resize_end_child(True)
        self.bottom_paned.set_shrink_start_child(False)
        self.bottom_paned.set_shrink_end_child(False)
        self.bottom_paned.set_hexpand(True); self.bottom_paned.set_vexpand(True)
        self.vpaned.set_end_child(self.bottom_paned)

        self.ref_list = ReferenceStarsList()
        self.curve_list = CurvePointsList()
        self.bottom_paned.set_start_child(self.ref_list)
        self.bottom_paned.set_end_child(self.curve_list)

        # Populate now
        self._populate()

        # Wire actions
        self.info.on_chart_clicked(self._on_open_chart)
        self.info.on_simbad_clicked(self._on_open_simbad, self.info.ra_hms, self.info.dec_dms)

        # Keep split proportional on map/resize
        self.connect("show", lambda *_: GLib.idle_add(self._apply_ratios))
        try:
            self.add_tick_callback(lambda *_: (self._apply_ratios() or False))
        except Exception:
            pass
        self.vpaned.connect("notify::position", self._on_vpaned_moved)
        self.top_paned.connect("notify::position", self._on_top_paned_moved)
        self.bottom_paned.connect("notify::position", self._on_bottom_paned_moved)

    # ---- proportional logic ----
    def _apply_ratios(self) -> bool:
        try:
            vh = self.vpaned.get_allocated_height()
            if vh > 0:
                self.vpaned.set_position(int(self._ratio_top * vh))
            tw = self.top_paned.get_allocated_width()
            if tw > 0:
                self.top_paned.set_position(int(self._ratio_plot * tw))
            bw = self.bottom_paned.get_allocated_width()
            if bw > 0:
                self.bottom_paned.set_position(int(self._ratio_bottom_left * bw))
        except Exception:
            pass
        return False

    def _on_vpaned_moved(self, *_a) -> None:
        try:
            vh = self.vpaned.get_allocated_height()
            if vh > 0:
                self._ratio_top = self.vpaned.get_position() / float(vh)
        except Exception:
            pass

    def _on_top_paned_moved(self, *_a) -> None:
        try:
            tw = self.top_paned.get_allocated_width()
            if tw > 0:
                self._ratio_plot = self.top_paned.get_position() / float(tw)
        except Exception:
            pass

    def _on_bottom_paned_moved(self, *_a) -> None:
        try:
            bw = self.bottom_paned.get_allocated_width()
            if bw > 0:
                self._ratio_bottom_left = self.bottom_paned.get_position() / float(bw)
        except Exception:
            pass

    # ---- content population ----
    def _populate(self) -> None:
        row = self._row; miner = self._miner
        self.info.populate_from(row, miner)

        sid = int(getattr(row.props, "id", -1))
        curve, used_pf = _collect_best_light_curve(miner, sid)

        # Populate lists
        pts = _curve_to_points(curve)
        self.curve_list.set_points(pts)
        self.ref_list.set_from_curve_or_miner(curve, miner)

        # Plot
        try:
            self.plot.plot_curve(curve, star_id=sid, pfilter=used_pf)
        except Exception as e:
            logger.warning("Plotting failed: %s", e)

        # npts label
        self.info.set("npts", str(len(pts)))

        # Update shown filter if we ended up choosing a different one
        if used_pf is not None:
            try:
                self.info.set("filter", str(used_pf))
            except Exception:
                pass

    # ---- Actions ----
    def _on_open_chart(self, _btn: Gtk.Button) -> None:
        try:
            from . import chart
            chart.show_finding_chart(
                self,
                miner=self._miner,
                star_id=self.info.star_id,
                field_name=self.info.field_name,
            )
        except Exception as e:
            print(f"Finding Chart error: {e}", file=sys.stderr)

    def _on_open_simbad(self, _btn: Gtk.Button, ra_hms: str, dec_dms: str) -> None:
        try:
            from . import simbad
            simbad.coordinate_query(ra_hms.replace(":", "h", 1), dec_dms.replace(":", "d", 1))
        except Exception:
            print("SIMBAD lookup not available (simbad.py missing or error).", file=sys.stderr)

# -------- small util --------
def _safe_getfloat(section: str, key: str, default: float) -> float:
    try:
        return float(cfg.get(section, key))  # type: ignore[attr-defined]
    except Exception:
        try:
            return cfg.getfloat(section, key, default)  # type: ignore[attr-defined]
        except Exception:
            return default

__all__ = ["StarDetailsWindow", "LightCurveView", "ReferenceStarsList", "CurvePointsList", "StarInfoPanel"]
