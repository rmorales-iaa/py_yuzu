# juicer/star_window.py
from __future__ import annotations

import inspect
import logging
import math
import sys
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GObject", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gio, GLib, Gdk, GdkPixbuf  # type: ignore

from .models import StarRow

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Optional configuration manager
# -----------------------------------------------------------------------------
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
        def attach_window(self, win: Gtk.Window, section: str) -> None: return
        def install_css_for_display(self, _dsp=None): return

    cfg = _CfgFallback()  # type: ignore

# Theme colors (match window.py defaults; config overrides if provided)
_DARK_BG = cfg.get("theme", "background", "#2a2f38")
_TEXT    = cfg.get("theme", "foreground", "#e7ebef")

# -------------------------
# GTK4 helpers
# -------------------------
def _remove_all_children(container: Gtk.Widget) -> None:
    child = container.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        try:
            container.remove(child)
        except Exception:
            try:
                child.destroy()
            except Exception:
                pass
        child = nxt


# =========================
# Utilities / formatting
# =========================
def _format_mag(val: Any) -> str:
    try:
        f = float(val)
        return "" if math.isnan(f) else f"{f:.3f}"
    except Exception:
        return ""


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
    return f"{sign}{d:02d}:{m:02d}:{s:06.2f}"


# =========================
# Light-curve plot widget
# =========================
class LightCurveView(Gtk.Box):
    """Matplotlib-in-GTK4 plot (dark-styled) with robust fallback to Agg."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._fig = None
        self._canvas = None           # GTK4 canvas (if available)
        self._toolbar = None          # GTK4 toolbar (if available)
        self._canvas_agg = None       # Agg canvas (fallback)
        self._picture: Optional[Gtk.Picture] = None  # shows Agg output

        self._build()

    def _build(self) -> None:
        """Build plot area, preferring GTK4 backend; fall back to Agg + Gtk.Picture."""
        try:
            import importlib.util
            from matplotlib.figure import Figure

            # Always create the figure (dark facecolor)
            self._fig = Figure(figsize=(6, 4), tight_layout=True, facecolor=_DARK_BG)

            # Detect availability of GTK4 backends without importing blindly
            have_gtk4_canvas = importlib.util.find_spec("matplotlib.backends.backend_gtk4agg") is not None
            have_gtk4_toolbar = importlib.util.find_spec("matplotlib.backends.backend_gtk4") is not None
            have_gtk4 = have_gtk4_canvas and have_gtk4_toolbar

            if have_gtk4:
                from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
                from matplotlib.backends.backend_gtk4 import NavigationToolbar2GTK4 as NavigationToolbar

                self._canvas = FigureCanvas(self._fig)  # type: ignore
                self._canvas.set_hexpand(True)
                self._canvas.set_vexpand(True)
                self.append(self._canvas)

                try:
                    self._toolbar = NavigationToolbar(self._canvas)  # type: ignore
                    self._toolbar.set_hexpand(True)
                    self._toolbar.set_vexpand(False)
                    self.append(self._toolbar)
                except Exception:
                    self._toolbar = None
            else:
                # Fallback: render offscreen with Agg and show via Gtk.Picture
                from matplotlib.backends.backend_agg import FigureCanvasAgg
                self._canvas_agg = FigureCanvasAgg(self._fig)  # type: ignore

                self._picture = Gtk.Picture.new()
                self._picture.set_hexpand(True)
                self._picture.set_vexpand(True)
                self.append(self._picture)

                note = Gtk.Label(label="Static plot (GTK4 Matplotlib backend not found).")
                note.set_xalign(0.0)
                note.add_css_class("dim-label")
                self.append(note)

        except Exception as e:
            lbl = Gtk.Label(
                label=f"Matplotlib not available: {e}\nInstall your distro's python3-matplotlib."
            )
            lbl.set_wrap(True)
            lbl.set_hexpand(True)
            lbl.set_vexpand(True)
            lbl.set_justify(Gtk.Justification.CENTER)
            lbl.set_xalign(0.5)
            lbl.set_yalign(0.5)
            self._fig = None
            self._canvas = lbl
            self._toolbar = None
            self.append(lbl)

    @property
    def figure(self):
        return self._fig

    def _style_axes_dark(self, ax, *, title=None, xlab=None, ylab=None) -> None:
        ax.set_facecolor(_DARK_BG)
        ax.figure.set_facecolor(_DARK_BG)
        if title:
            ax.set_title(title, color=_TEXT)
        if xlab:
            ax.set_xlabel(xlab, color=_TEXT)
        if ylab:
            ax.set_ylabel(ylab, color=_TEXT)
        for sp in ax.spines.values():
            sp.set_color(_TEXT)
        ax.tick_params(colors=_TEXT)
        ax.grid(True, color=_TEXT, alpha=0.20, linewidth=0.7)

    def _update_picture_from_agg(self) -> None:
        """Render the Agg canvas to a Gtk.Picture using a Pixbuf/Texture bridge."""
        if not self._canvas_agg or not self._picture:
            return
        try:
            self._canvas_agg.draw()
            buf = self._canvas_agg.buffer_rgba()
            w, h = self._canvas_agg.get_width_height()
            data = bytes(buf)  # memoryview ? bytes
            pixbuf = GdkPixbuf.Pixbuf.new_from_data(
                data,
                GdkPixbuf.Colorspace.RGB,
                True, 8, w, h, w * 4
            )
            texture = Gdk.Texture.new_for_pixbuf(pixbuf)
            self._picture.set_paintable(texture)
        except Exception as e:
            logger.exception("Agg fallback render failed: %s", e)

    def plot_curve(self, points: Optional[Sequence[Tuple[Any, float, Optional[float]]]], *,
                   star_id: int) -> None:
        """Plot points as (t, dm, err?)."""
        if self._fig is None:
            return
        try:
            import matplotlib.dates as mdates
            ax = self._fig.add_subplot(111)
            ax.clear()
            self._style_axes_dark(ax, title=f"Light curve ? Star {star_id}",
                                  xlab="Date (UTC)", ylab="?mag")

            if points:
                ts, ys, es = [], [], []
                for p in points:
                    if p is None or len(p) < 2:
                        continue
                    t = p[0]
                    dm = p[1]
                    er = p[2] if len(p) > 2 else None
                    ts.append(t)
                    ys.append(dm)
                    es.append(0.0 if er is None else er)

                # Convert numeric timestamps to datetime if needed
                if ts and isinstance(ts[0], (int, float)):
                    import datetime
                    ts = [datetime.datetime.utcfromtimestamp(float(x)) for x in ts]  # type: ignore

                ax.errorbar(ts, ys, yerr=(es if any(es) else None),
                            fmt="o", ms=4, color=_TEXT, ecolor=_TEXT, elinewidth=0.8)
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
                self._fig.autofmt_xdate()
            else:
                ax.text(0.5, 0.5, "No curve data available",
                        transform=ax.transAxes, ha="center", va="center", color=_TEXT)

            # Draw depending on backend
            if getattr(self, "_canvas", None) is not None and hasattr(self._fig, "canvas") and self._fig.canvas:
                self._fig.canvas.draw_idle()
            else:
                self._update_picture_from_agg()
        except Exception as e:
            logger.exception("LightCurveView plot error: %s", e)


# =========================
# Star info panel (with actions)
# =========================
class StarInfoPanel(Gtk.Box):
    """Key/value info grid + action buttons (Finding Chart, SIMBAD)."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.set_margin_start(6)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        self._grid.set_hexpand(True)
        self._grid.set_vexpand(True)
        self.add_css_class("lemon-surface")
        self.append(self._grid)

        # Button row
        self._btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._btn_row.set_halign(Gtk.Align.END)
        self._btn_row.set_hexpand(True)
        self._btn_row.add_css_class("lemon-surface")
        self.append(self._btn_row)

        self._chart_btn = Gtk.Button(label="View in Finding Chart")
        self._chart_btn.add_css_class("lemon-btn")
        self._simbad_btn = Gtk.Button(label="Look up in SIMBAD")
        self._simbad_btn.add_css_class("lemon-btn")
        self._btn_row.append(self._chart_btn)
        self._btn_row.append(self._simbad_btn)

        # internal state
        self.ra_deg: float = 0.0
        self.dec_deg: float = 0.0
        self.ra_hms: str = ""
        self.dec_dms: str = ""
        self.star_id: int = 0
        self.field_name: str = ""

    def on_chart_clicked(self, handler):
        self._chart_btn.connect("clicked", handler)

    def on_simbad_clicked(self, handler, ra_hms: str, dec_dms: str):
        self._simbad_btn.connect("clicked", handler, ra_hms, dec_dms)

    def _put_row(self, r: int, key: str, val: str) -> None:
        k = Gtk.Label(xalign=1.0, label=key)
        k.add_css_class("dim-label")
        k.set_hexpand(False)
        v = Gtk.Label(xalign=0.0, label=val)
        v.set_selectable(True)
        v.set_hexpand(True)
        self._grid.attach(k, 0, r, 1, 1)
        self._grid.attach(v, 1, r, 1, 1)

    def populate_from(self, row: StarRow, miner) -> None:
        _remove_all_children(self._grid)

        # Base values
        ra_deg = float(getattr(row.props, "ra", 0.0))
        dec_deg = float(getattr(row.props, "dec", 0.0))
        imag = getattr(row.props, "imag", float("nan"))
        x = y = None

        # Try to fetch x,y, precise ra/dec, mag from miner
        try:
            vals = miner.get_star(int(row.props.id))
            if isinstance(vals, (tuple, list)) and len(vals) >= 5:
                x, y = float(vals[0]), float(vals[1])
                ra_deg, dec_deg = float(vals[2]), float(vals[3])
                imag = float(vals[-1])
        except Exception:
            pass

        ra_hms = _hms_from_deg(ra_deg)
        dec_dms = _dms_from_deg(dec_deg)

        # Save for actions
        self.ra_deg = ra_deg
        self.dec_deg = dec_deg
        self.ra_hms = ra_hms
        self.dec_dms = dec_dms
        self.star_id = int(row.props.id)
        self.field_name = getattr(miner, "field_name", "") or Path(getattr(miner, "path", "")).name

        # Fill grid
        self._put_row(0, "ID:", str(self.star_id))
        self._put_row(1, "RA (hms):", ra_hms)
        self._put_row(2, "Dec (dms):", dec_dms)
        self._put_row(3, "RA (deg):", f"{ra_deg:.8f}")
        self._put_row(4, "Dec (deg):", f"{dec_deg:.8f}")
        self._put_row(5, "Mag:", _format_mag(imag))
        if x is not None and y is not None:
            self._put_row(6, "x:", f"{x:.2f}")
            self._put_row(7, "y:", f"{y:.2f}")


# =========================
# Reference stars list
# =========================
class ReferenceStarsList(Gtk.Frame):
    def __init__(self) -> None:
        super().__init__(label="Reference stars")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._model: Gtk.StringList = Gtk.StringList.new([])
        self._sel = Gtk.SingleSelection.new(self._model)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0.0)))
        def _bind(_f, li: Gtk.ListItem):
            it = li.get_item()
            li.get_child().set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
        factory.connect("bind", _bind)

        self._view = Gtk.ListView.new(self._sel, factory)
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)
        self._view.add_css_class("lemon-surface")

        sc = Gtk.ScrolledWindow.new()
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        sc.add_css_class("lemon-surface")
        sc.set_child(self._view)
        self.set_child(sc)

    def set_ids(self, ids: Iterable[int] | None) -> None:
        self._model = Gtk.StringList.new([])
        self._sel.set_model(self._model)
        self._view.set_model(self._sel)
        if not ids:
            return
        for rid in ids:
            self._model.append(f"ID {rid}")


# =========================
# Curve points list
# =========================
class CurvePointsList(Gtk.Frame):
    def __init__(self) -> None:
        super().__init__(label="Curve points")
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._store: Gio.ListStore = Gio.ListStore.new(Gtk.StringObject)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", lambda _f, li: li.set_child(Gtk.Label(xalign=0.0)))
        def _bind(_f, li: Gtk.ListItem):
            it = li.get_item()
            li.get_child().set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
        factory.connect("bind", _bind)

        sel = Gtk.SingleSelection.new(self._store)
        self._view = Gtk.ListView.new(sel, factory)
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)
        self._view.add_css_class("lemon-surface")

        sc = Gtk.ScrolledWindow.new()
        sc.set_hexpand(True)
        sc.set_vexpand(True)
        sc.add_css_class("lemon-surface")
        sc.set_child(self._view)
        self.set_child(sc)

    def set_points(self, points: Optional[Sequence[Tuple[Any, float, Optional[float]]]]) -> None:
        # Clear list store in a robust way
        try:
            self._store.remove_all()
        except AttributeError:
            for i in range(self._store.get_n_items() - 1, -1, -1):
                self._store.remove(i)

        if not points:
            return

        for i, pt in enumerate(points, 1):
            try:
                if len(pt) >= 3:
                    t, dm, er = pt[0], pt[1], pt[2]
                    line = f"{i:03d}  t={t}  ?mag={_format_mag(dm)}  err={_format_mag(er)}"
                else:
                    line = f"{i:03d}  {pt!r}"
            except Exception:
                line = f"{i:03d}  {pt!r}"
            self._store.append(Gtk.StringObject.new(line))


# =========================
# Helpers to fetch data
# =========================
def _collect_light_curve(miner, star_id: int) -> Optional[Sequence[Tuple[Any, float, Optional[float]]]]:
    """Returns list of (t, dm, err?) for the given star_id, trying different miner APIs."""
    curve = None
    for name in ("get_light_curve", "light_curve_for_star", "get_curve"):
        f = getattr(miner, name, None)
        if not callable(f):
            continue
        try:
            sig = None
            try:
                sig = inspect.signature(f)
            except Exception:
                pass

            if sig and "pfilter" in sig.parameters:
                # Choose a filter
                pf = None
                for attr in ("selected_filter", "current_filter", "default_filter"):
                    pf = getattr(miner, attr, None)
                    if pf:
                        break
                if not pf:
                    pfilters = getattr(miner, "pfilters", None)
                    if pfilters:
                        try:
                            pf = next(iter(pfilters))
                        except Exception:
                            pf = None
                if pf is not None:
                    curve = f(int(star_id), pf)
                else:
                    combined: list[Tuple[Any, float, Optional[float]]] = []
                    pfilters = getattr(miner, "pfilters", []) or []
                    for _pf in pfilters:
                        try:
                            pts = f(int(star_id), _pf) or []
                            combined.extend(list(pts))
                        except Exception:
                            pass
                    curve = combined
            else:
                curve = f(int(star_id))
            break
        except TypeError:
            continue
        except Exception:
            continue
    return curve


def _collect_reference_ids(miner) -> Iterable[int] | None:
    for attr in ("reference_star_ids", "refstars", "reference_stars"):
        ids = getattr(miner, attr, None)
        if ids:
            return ids
    for name in ("get_reference_stars", "get_refstars"):
        f = getattr(miner, name, None)
        if callable(f):
            try:
                return f()
            except Exception:
                pass
    return None


# =========================
# Composed details window
# =========================
class StarDetailsWindow(Gtk.Window):
    """
    Star details window composed of:
      - LightCurveView (left/top)
      - StarInfoPanel with actions (right/top)
      - ReferenceStarsList (bottom-left)
      - CurvePointsList (bottom-right)
    Uses .lemon-surface to inherit dark theme from app CSS.
    """

    def __init__(self, parent: Gtk.Window, row: StarRow, miner) -> None:
        title = f"Star {row.props.id}"
        super().__init__(transient_for=parent, modal=True, title=title)
        self.set_hide_on_close(True)

        # Size & ratios from config, with sensible defaults
        self.set_default_size(
            cfg.getint("star_window", "width", 980),
            cfg.getint("star_window", "height", 640),
        )
        self._ratio_top = cfg.getfloat("star_window", "ratio_top", 0.60)          # top vs bottom
        self._ratio_plot = cfg.getfloat("star_window", "ratio_plot", 0.62)        # plot vs info
        self._ratio_bottom_left = cfg.getfloat("star_window", "ratio_bottom_left", 0.50)  # ref vs curve

        self._row = row
        self._miner = miner

        # Persist geometry via config manager (no-op if fallback)
        try:
            cfg.attach_window(self, "star_window")
        except Exception:
            pass

        # Root container
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(8)
        root.set_margin_end(8)
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.add_css_class("lemon-surface")
        self.set_child(root)

        # Split vertical (top: plot+info, bottom: lists)
        self.vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned.set_wide_handle(True)
        self.vpaned.set_resize_start_child(True)
        self.vpaned.set_resize_end_child(True)
        self.vpaned.set_shrink_start_child(False)
        self.vpaned.set_shrink_end_child(False)
        self.vpaned.set_hexpand(True)
        self.vpaned.set_vexpand(True)
        root.append(self.vpaned)

        # Top split (plot | info)
        self.top_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_wide_handle(True)
        self.top_paned.set_resize_start_child(True)
        self.top_paned.set_resize_end_child(True)
        self.top_paned.set_shrink_start_child(False)
        self.top_paned.set_shrink_end_child(False)
        self.top_paned.set_hexpand(True)
        self.top_paned.set_vexpand(True)
        self.vpaned.set_start_child(self.top_paned)

        # Plot
        self.plot = LightCurveView()
        self.top_paned.set_start_child(self.plot)

        # Info (in scroller)
        sc_info = Gtk.ScrolledWindow.new()
        sc_info.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sc_info.set_hexpand(True)
        sc_info.set_vexpand(True)
        sc_info.add_css_class("lemon-surface")
        self.info = StarInfoPanel()
        sc_info.set_child(self.info)
        self.top_paned.set_end_child(sc_info)

        # Bottom split (ref | curve)
        self.bottom_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.bottom_paned.set_wide_handle(True)
        self.bottom_paned.set_resize_start_child(True)
        self.bottom_paned.set_resize_end_child(True)
        self.bottom_paned.set_shrink_start_child(False)
        self.bottom_paned.set_shrink_end_child(False)
        self.bottom_paned.set_hexpand(True)
        self.bottom_paned.set_vexpand(True)
        self.vpaned.set_end_child(self.bottom_paned)

        self.ref_list = ReferenceStarsList()
        self.curve_list = CurvePointsList()
        self.bottom_paned.set_start_child(self.ref_list)
        self.bottom_paned.set_end_child(self.curve_list)

        # Populate content
        self._populate(row, miner)

        # Wire actions
        self.info.on_chart_clicked(self._on_open_chart)
        self.info.on_simbad_clicked(self._on_open_simbad, self.info.ra_hms, self.info.dec_dms)

        # Keep split proportional on resize & after drag
        self.vpaned.connect("notify::position", self._on_vpaned_moved)
        self.top_paned.connect("notify::position", self._on_top_paned_moved)
        self.bottom_paned.connect("notify::position", self._on_bottom_paned_moved)
        GLib.idle_add(self._apply_ratios)

    # --- GTK4 vfunc override to keep panes proportional on resize ---
    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        try:
            super().do_size_allocate(width, height, baseline)
        except TypeError:
            Gtk.Window.do_size_allocate(self, width, height, baseline)
        self._apply_ratios()

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
        return False  # for GLib.idle_add

    def _on_vpaned_moved(self, paned, _pspec):
        h = paned.get_allocated_height()
        if h > 0:
            self._ratio_top = max(0.05, min(0.95, paned.get_position() / h))

    def _on_top_paned_moved(self, paned, _pspec):
        w = paned.get_allocated_width()
        if w > 0:
            self._ratio_plot = max(0.10, min(0.90, paned.get_position() / w))

    def _on_bottom_paned_moved(self, paned, _pspec):
        w = paned.get_allocated_width()
        if w > 0:
            self._ratio_bottom_left = max(0.10, min(0.90, paned.get_position() / w))

    # ---- Data population ----
    def _populate(self, row: StarRow, miner) -> None:
        self.info.populate_from(row, miner)

        curve = _collect_light_curve(miner, int(row.props.id))
        self.curve_list.set_points(curve)
        self.plot.plot_curve(curve, star_id=int(row.props.id))

        self.ref_list.set_ids(_collect_reference_ids(miner))

    # ---- Actions ----
    def _on_open_chart(self, _btn: Gtk.Button) -> None:
        try:
            from . import chart
            chart.show_finding_chart(
                self,
                ra_deg=self.info.ra_deg,
                dec_deg=self.info.dec_deg,
                miner=self._miner,
                star_id=self.info.star_id,
                field_name=self.info.field_name,
            )
        except Exception as e:
            print(f"Finding Chart error: {e}", file=sys.stderr)

    def _on_open_simbad(self, _btn: Gtk.Button, ra_hms: str, dec_dms: str) -> None:
        try:
            from . import simbad
            # SIMBAD prefers 00h00m00s / +00d00m00s or HH:MM:SS variants
            simbad.coordinate_query(ra_hms.replace(":", "h", 1), dec_dms.replace(":", "d", 1))
        except Exception:
            print("SIMBAD lookup not available (simbad.py missing or error).", file=sys.stderr)
