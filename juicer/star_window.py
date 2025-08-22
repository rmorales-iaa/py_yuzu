# juicer/star_window.py
# GTK4 Star details — accepts StarDetailsWindow(parent, data=...) and supports set_star_data(...)

from __future__ import annotations
import io
import logging
import math
import sys
from pathlib import Path
from typing import Any, Iterable, List, Optional, Tuple, Union

import gi

from mining import LEMONdBMiner

gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk  # type: ignore

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

# ---------- optional config/theming ----------
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

_BG  = _cfg_get("theme", "background", "#1f242b")
_FG  = _cfg_get("theme", "foreground", "#e7ebef")
_ACC = _cfg_get("theme", "accent",     "#ffd84d")

def _install_theme_css() -> None:
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(f"""
            .lemon-surface * {{ color: {_FG}; }}
            .lemon-surface {{ background: {_BG}; }}
            .monospace {{ font-family: monospace; }}
        """.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

# ---------- format helpers ----------
def _hms_from_deg(ra_deg: float) -> str:
    ra = (ra_deg / 15.0) % 24.0
    h = int(ra); m = int((ra - h) * 60.0); s = (ra - h - m/60.0) * 3600.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def _dms_from_deg(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else "+"
    dabs = abs(dec_deg)
    d = int(dabs); m = int((dabs - d) * 60.0); s = (dabs - d - m/60.0) * 3600.0
    return f"{sign}{d:02d}:{m:02d}:{s:05.2f}"

def _fmt_time(unix: float) -> str:
    try:
        import datetime as _dt
        return _dt.datetime.utcfromtimestamp(float(unix)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return f"{float(unix):.6f}"

def _format_mag(x: Optional[float]) -> str:
    try:
        if x is None or (isinstance(x, float) and math.isnan(x)):
            return "—"
        return f"{float(x):.4f}"
    except Exception:
        return str(x)

# ---------- curve normalization ----------
def _curve_to_points(curve: Any) -> List[Tuple[float, float, Optional[float]]]:
    """Return list[(unix_time, mag, snr|None)] from many shapes."""
    if curve is None:
        return []
    pts: List[Tuple[float, float, Optional[float]]] = []
    try:
        for item in curve:
            try:
                t, mag, snr = item  # type: ignore[misc]
                pts.append((float(t), float(mag), (None if snr is None else float(snr))))
            except Exception:
                continue
        if pts:
            return pts
    except Exception:
        pass
    for attr in ("_data", "data"):
        try:
            seq = getattr(curve, attr)
            return _curve_to_points(seq)
        except Exception:
            continue
    return []

def _curve_cstars_with_weights(curve: Any) -> Optional[List[Tuple[int, float, float]]]:
    """Try (id, weight, stdev)."""
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
    try:
        cstars = list(getattr(curve, "cstars"))
        wts    = list(getattr(curve, "cweights"))
        stds   = list(getattr(curve, "cstdevs"))
        if cstars and len(cstars) == len(wts) == len(sts := stds):
            return [(int(s), float(w), float(st)) for s, w, st in zip(cstars, wts, sts)]
    except Exception:
        pass
    return None

def _curve_cstars_ids_only(curve: Any) -> Optional[Iterable[int]]:
    try:
        ids = getattr(curve, "cstars", None)
        if ids:
            return [int(x) for x in ids]
    except Exception:
        pass
    return None

# ---------- miner & DB helpers ----------
def _iter_filters(miner: Any):
    seen = set()
    for name in ("selected_filter", "current_filter", "default_filter"):
        pf = getattr(miner, name, None)
        if pf is not None:
            key = pf if isinstance(pf, (int, str)) else id(pf)
            if key not in seen:
                seen.add(key); yield pf
    for pf in (getattr(miner, "pfilters", None) or []):
        key = pf if isinstance(pf, (int, str)) else id(pf)
        if key not in seen:
            seen.add(key); yield pf

def _collect_best_light_curve(miner: Any, star_id: int) -> Tuple[Any | None, Any | None]:
    names = ("get_light_curve", "get_curve", "light_curve_for_star",
             "lightcurve_for_star", "light_curve")
    for pf in _iter_filters(miner):
        for name in names:
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
    for name in names:
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

def _find_db_path(miner: Any, parent: Optional[Gtk.Window] = None) -> Optional[str]:
    for attr in ("path", "db_path", "database_path", "dbfile", "filename"):
        p = getattr(miner, attr, None)
        if isinstance(p, str) and p:
            return p
        try:
            if hasattr(p, "__fspath__"):
                return str(p)  # type: ignore[arg-type]
        except Exception:
            pass
    try:
        if parent is not None:
            p = getattr(parent, "_db_path", None)
            if isinstance(p, str) and p:
                return p
    except Exception:
        pass
    return None

def _collect_via_sqlalchemy(miner: Any, star_id: int) -> Tuple[Any | None, Any | None]:
    sess = getattr(miner, "sa_session", None)
    if sess is None:
        return None, None
    try:
        from sqlalchemy import text  # type: ignore
    except Exception:
        return None, None

    row = sess.execute(text("""
        SELECT pf.id AS filter_id, pf.name AS fname, COUNT(*) AS n
        FROM light_curves lc
        JOIN images img ON lc.image_id = img.id
        JOIN photometric_filters pf ON img.filter_id = pf.id
        WHERE lc.star_id = :sid
        GROUP BY pf.id, pf.name
        ORDER BY n DESC
        LIMIT 1
    """), {"sid": int(star_id)}).fetchone()
    if not row:
        return None, None
    fid = row.filter_id
    used_filter = row.fname

    pts = sess.execute(text("""
        SELECT img.unix_time, lc.magnitude, lc.snr
        FROM light_curves lc
        JOIN images img ON lc.image_id = img.id
        WHERE lc.star_id = :sid AND img.filter_id = :fid
        ORDER BY img.unix_time ASC
    """), {"sid": int(star_id), "fid": int(fid)}).fetchall()

    refs = sess.execute(text("""
        SELECT cstar_id, weight, stdev
        FROM cmp_stars
        WHERE star_id = :sid AND filter_id = :fid
        ORDER BY cstar_id
    """), {"sid": int(star_id), "fid": int(fid)}).fetchall()

    if not pts:
        return None, None

    class _LC:
        def __init__(self, pts, refs, fname):
            self._data = [(float(t), float(m), (None if s is None else float(s))) for (t, m, s) in pts]
            self.cstars   = [int(s) for (s, *_r) in refs]
            self.cweights = [float(w) for (_s, w, _st) in refs]
            self.cstdevs  = [float(st) for (_s, _w, st) in refs]
            self.pfilter  = fname
        def __iter__(self): return iter(self._data)
        def weights(self):  # (id, weight, stdev)
            return zip(self.cstars, self.cweights, self.cstdevs)

    return _LC(pts, refs, used_filter), used_filter

def _collect_via_lemondb_path(db_path: str, star_id: int) -> Tuple[Any | None, Any | None]:
    try:
        from . import database  # type: ignore
    except Exception:
        try:
            import database  # type: ignore
        except Exception:
            return None, None

    try:
        with database.LEMONdB(db_path) as db:
            best_curve, best_pf, best_len = None, None, -1
            for pf in db.filters():
                try:
                    cur = db.get_light_curve(int(star_id), pf)
                    pts = _curve_to_points(cur)
                    if len(pts) > best_len:
                        best_curve, best_pf, best_len = cur, pf, len(pts)
                except Exception:
                    continue
            return best_curve, best_pf
    except Exception:
        return None, None

# ---------- UI widgets ----------
class LightCurveView(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")
        self._fig = None; self._canvas = None; self._toolbar = None; self._picture = None
        self._build()

    def _build(self) -> None:
        try:
            from matplotlib.figure import Figure
            self._fig = Figure(figsize=(6, 4), tight_layout=True, facecolor=_BG)
        except Exception:
            self._fig = None

        if self._fig is not None:
            try:
                from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4 as FigureCanvas
                from matplotlib.backends.backend_gtk4 import NavigationToolbar2GTK4 as Toolbar
                self._canvas = FigureCanvas(self._fig)
                self._canvas.set_hexpand(True); self._canvas.set_vexpand(True)
                self._toolbar = Toolbar(self._canvas)
                self._toolbar.set_hexpand(True); self._toolbar.add_css_class("lemon-surface")
                self.append(self._toolbar); self.append(self._canvas); return
            except Exception:
                self._canvas = None; self._toolbar = None

        self._picture = Gtk.Picture.new()
        self._picture.set_hexpand(True); self._picture.set_vexpand(True)
        self.append(self._picture)

    def _style_axes(self, ax, *, title: str, xlab: str, ylab: str) -> None:
        ax.set_title(title, color=_FG)
        ax.set_xlabel(xlab, color=_FG); ax.set_ylabel(ylab, color=_FG)
        for s in ("top", "right", "left", "bottom"):
            ax.spines[s].set_color(_FG)
        ax.tick_params(axis="x", colors=_FG)
        ax.tick_params(axis="y", colors=_FG)
        ax.grid(True, color=_FG, alpha=0.15)

    def plot_curve(self, points: Any, *, star_id: Optional[int] = None, pfilter: Any | None = None) -> None:
        if self._fig is None:
            if self._picture: self._picture.set_paintable(None)
            return
        try:
            trip = _curve_to_points(points)
            self._fig.clear()
            ax = self._fig.add_subplot(111)
            title = "Light curve"
            if star_id is not None: title += f" — Star {star_id}"
            if pfilter is not None: title += f" — {pfilter}"
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

class StringListFrame(Gtk.Frame):
    def __init__(self, title: str):
        super().__init__(label=title)
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")

        self._store: Gio.ListStore = Gio.ListStore.new(Gtk.StringObject)

        factory = Gtk.SignalListItemFactory()
        def setup(_f, li: Gtk.ListItem):
            lbl = Gtk.Label(xalign=0.0); lbl.add_css_class("monospace"); lbl.set_selectable(True)
            li.set_child(lbl)
        def bind(_f, li: Gtk.ListItem):
            it = li.get_item(); lbl: Gtk.Label = li.get_child()  # type: ignore[assignment]
            lbl.set_text(it.get_string() if it else "")  # type: ignore[attr-defined]
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
        self.set_child(sc)

    def set_lines(self, lines: List[str]) -> None:
        self._store.splice(0, self._store.get_n_items(), [])
        for line in lines:
            self._store.append(Gtk.StringObject.new(line))

class StarInfoPanel(Gtk.Frame):
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
            ("id","ID"), ("ra","RA (hms)"), ("dec","Dec (dms)"),
            ("imag","Mag"), ("filter","Filter"), ("npts","Curve points"),
        )):
            k_lbl = Gtk.Label(label=f"{txt}:", xalign=1.0); k_lbl.add_css_class("monospace")
            v_lbl = Gtk.Label(label="—", xalign=0.0);       v_lbl.add_css_class("monospace"); v_lbl.set_selectable(True)
            grid.attach(k_lbl, 0, i, 1, 1)
            grid.attach(v_lbl, 1, i, 1, 1)
            self._labels[k] = v_lbl

        # Action buttons
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.btn_chart  = Gtk.Button(label="Finding chart")
        self.btn_simbad = Gtk.Button(label="Open in SIMBAD")
        box.append(self.btn_chart); box.append(self.btn_simbad)
        grid.attach(box, 0, 6, 2, 1)

        # Mirrors for actions
        self.star_id: int = -1
        self.ra_hms: str = ""
        self.dec_dms: str = ""
        self.field_name: str = ""

    def set(self, key: str, value: str) -> None:
        lbl = self._labels.get(key)
        if lbl: lbl.set_label(value)

# ---------- main window ----------
class StarDetailsWindow(Gtk.Window):
    """
    Accepts legacy/new patterns:
      StarDetailsWindow(parent, data=<dict>)
      StarDetailsWindow(parent, row=<row>, miner=?)
      StarDetailsWindow(parent, row, miner)

    Also provides: set_star_data(data, reference_rows=None, point_rows=None)
    so the caller (your window) can push precomputed lists.  :contentReference[oaicite:1]{index=1}
    """
    def __init__(self, parent: Gtk.Window,
                 row_or_data: Optional[Union[Any, dict]] = None,
                 miner: LEMONdBMiner = None,
                 **kwargs) -> None:
        _install_theme_css()

        row_kw = kwargs.pop("row", None)
        data_kw = kwargs.pop("data", None)

        if row_kw is not None:
            row = row_kw
        elif row_or_data is not None and not isinstance(row_or_data, dict):
            row = row_or_data
        else:
            row = None

        if data_kw is not None:
            data = data_kw
        elif isinstance(row_or_data, dict):
            data = row_or_data
        else:
            data = None


        # Title from data/row
        sid = None
        if row is not None:
            try: sid = int(getattr(row.props, "id", -1))
            except Exception: sid = None
        if sid is None and isinstance(data, dict):
            try: sid = int(data.get("id"))
            except Exception: sid = None

        super().__init__(transient_for=parent, modal=True,
                         title=(f"Star {sid}" if isinstance(sid, int) and sid >= 0 else "Star details"))
        self.set_hide_on_close(True)

        try:
            w = int(cfg.get("star_window","width")); h = int(cfg.get("star_window","height"))  # type: ignore[attr-defined]
        except Exception:
            w, h = 1024, 680
        self.set_default_size(w, h)
        try:
            if cfg: cfg.attach_window(self, "star_window")  # type: ignore[attr-defined]
        except Exception:
            pass

        self._parent = parent
        self._miner  = miner
        self._row    = row
        self._data   = data or {}

        # Layout
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8); root.set_margin_bottom(8)
        root.set_margin_start(8); root.set_margin_end(8)
        root.add_css_class("lemon-surface")
        self.set_child(root)

        self.vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL); self.vpaned.set_resize_start_child(True); self.vpaned.set_resize_end_child(True); self.vpaned.set_wide_handle(True)
        root.append(self.vpaned)

        self.top_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL); self.top_paned.set_resize_start_child(True); self.top_paned.set_resize_end_child(True); self.top_paned.set_wide_handle(True)
        self.vpaned.set_start_child(self.top_paned)

        self.bottom_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL); self.bottom_paned.set_resize_start_child(True); self.bottom_paned.set_resize_end_child(True); self.bottom_paned.set_wide_handle(True)
        self.vpaned.set_end_child(self.bottom_paned)

        # Top: plot + info
        self.plot = LightCurveView()
        sc_plot = Gtk.ScrolledWindow.new(); sc_plot.set_child(self.plot); sc_plot.set_hexpand(True); sc_plot.set_vexpand(True); sc_plot.add_css_class("lemon-surface")
        self.top_paned.set_start_child(sc_plot)

        self.info = StarInfoPanel()
        sc_info = Gtk.ScrolledWindow.new(); sc_info.set_child(self.info); sc_info.set_hexpand(True); sc_info.set_vexpand(True); sc_info.add_css_class("lemon-surface")
        self.top_paned.set_end_child(sc_info)

        # Bottom: refs + points
        self.refs   = StringListFrame("Reference stars")
        self.points = StringListFrame("Curve points")
        self.bottom_paned.set_start_child(self.refs)
        self.bottom_paned.set_end_child(self.points)

        # Static header from provided dict/row (fast feedback)
        self._populate_static_header()

        # Wire actions
        self.info.btn_chart.connect("clicked", self._on_open_chart)
        self.info.btn_simbad.connect("clicked", self._on_open_simbad)

        # Ratios
        self._ratio_top = _cfg_getfloat("star_window","ratio_top",0.60)
        self._ratio_plot = _cfg_getfloat("star_window","ratio_plot",0.60)
        self._ratio_bottom_left = _cfg_getfloat("star_window","ratio_bottom_left",0.50)
        self.connect("show", lambda *_: GLib.idle_add(self._apply_ratios))

        # Load dynamic data (miner → SQLAlchemy → .LEMONdB)
        GLib.idle_add(lambda: (self._load_dynamic() or False))

    # ----- external API expected by window.py -----
    def set_star_data(self, data: dict,
                      reference_rows: Optional[List[str]] = None,
                      point_rows: Optional[List[Tuple[str, str]]] = None) -> None:
        """
        Called by the main window right after construction to push a prebuilt payload.
        We use it to immediately update labels/lists; dynamic loading will still refine.
        """
        try:
            self._data.update(data or {})
        except Exception:
            self._data = dict(data or {})
        self._populate_static_header()

        # Refs
        if reference_rows:
            self.refs.set_lines([str(x) for x in reference_rows])

        # Points (string pairs -> show as-is; plotting waits for numeric dynamic load)
        if point_rows:
            lines: List[str] = []
            for i, (x, y) in enumerate(point_rows, 1):
                lines.append(f"{i:03d}  {str(x)}    mag={str(y)}")
            self.points.set_lines(lines)

    # ----- UI proportions -----
    def _apply_ratios(self) -> bool:
        try:
            vh = self.vpaned.get_allocated_height()
            if vh > 0: self.vpaned.set_position(int(self._ratio_top * vh))
            tw = self.top_paned.get_allocated_width()
            if tw > 0: self.top_paned.set_position(int(self._ratio_plot * tw))
            bw = self.bottom_paned.get_allocated_width()
            if bw > 0: self.bottom_paned.set_position(int(self._ratio_bottom_left * bw))
        except Exception:
            pass
        return False

    # ----- labels from static info -----
    def _populate_static_header(self) -> None:
        sid = self._data.get("id")
        ra_deg = self._data.get("ra_deg")
        dec_deg = self._data.get("dec_deg")
        imag = self._data.get("mag")
        pfilter = self._data.get("filt")

        if sid is None and self._row is not None:
            try: sid = int(getattr(self._row.props, "id", -1))
            except Exception: sid = None
        if (ra_deg is None or dec_deg is None) and self._row is not None:
            try:
                ra_deg = float(getattr(self._row.props, "ra", 0.0))
                dec_deg = float(getattr(self._row.props, "dec", 0.0))
            except Exception:
                pass
            imag = getattr(self._row.props, "imag", imag)

        self.info.star_id = int(sid) if isinstance(sid, (int, float)) else -1

        ra_str  = self._data.get("ra_str")  or (_hms_from_deg(float(ra_deg)) if isinstance(ra_deg,(int,float)) else "")
        dec_str = self._data.get("dec_str") or (_dms_from_deg(float(dec_deg)) if isinstance(dec_deg,(int,float)) else "")
        self.info.ra_hms  = ra_str
        self.info.dec_dms = dec_str
        self.info.field_name = self._data.get("field_name", "") or str(_find_db_path(self._miner, self._parent) or "")

        self.info.set("id", str(self.info.star_id if self.info.star_id >= 0 else "—"))
        self.info.set("ra", ra_str or "—")
        self.info.set("dec", dec_str or "—")
        self.info.set("imag", "" if (imag is None or (isinstance(imag,float) and math.isnan(imag))) else f"{float(imag):.3f}")
        self.info.set("filter", str(pfilter) if pfilter is not None else "—")
        self.info.set("npts", str(self._data.get("n_points")) if self._data.get("n_points") is not None else "—")

    # ----- dynamic: curve, refs, filter -----
    def _load_dynamic(self) -> bool:
        sid = self.info.star_id
        if not isinstance(sid, int) or sid < 0:
            return False
        miner = self._miner

        curve = None; used_pf = None

        # 1) Miner APIs
        if miner is not None:
            try:
                curve, used_pf = _collect_best_light_curve(miner, sid)
            except Exception:
                pass

        # 2) SQLAlchemy (miner.sa_session)
        if not _curve_to_points(curve) and miner is not None:
            try:
                alt_curve, alt_pf = _collect_via_sqlalchemy(miner, sid)
                if _curve_to_points(alt_curve):
                    curve, used_pf = alt_curve, alt_pf
            except Exception:
                pass

        # 3) Raw .LEMONdB
        if not _curve_to_points(curve):
            db_path = _find_db_path(miner, self._parent) if miner is not None else getattr(self._parent, "_db_path", None)
            if db_path:
                try:
                    alt_curve, alt_pf = _collect_via_lemondb_path(str(db_path), sid)
                    if _curve_to_points(alt_curve):
                        curve, used_pf = alt_curve, alt_pf
                except Exception:
                    pass

        # Lists
        pts = _curve_to_points(curve)
        lines_pts: List[str] = []
        for i, (t, mag, snr) in enumerate(pts, 1):
            snr_txt = "" if snr is None else f"{snr:.1f}"
            lines_pts.append(f"{i:03d}  {_fmt_time(t)}    mag={_format_mag(mag)}    SNR={snr_txt}")
        if lines_pts:
            self.points.set_lines(lines_pts)

        lines_refs: List[str] = []
        w = _curve_cstars_with_weights(curve)
        if w:
            for sidr, wt, st in w:
                lines_refs.append(f"{sidr:6d}    weight={wt:.4f}    σ={st:.4f}")
        else:
            ids = _curve_cstars_ids_only(curve)
            if not ids and miner is not None:
                for attr in ("reference_star_ids", "refstars", "reference_stars"):
                    ids = getattr(miner, attr, None)
                    if ids: break
                if not ids:
                    for name in ("get_reference_stars", "get_refstars"):
                        f = getattr(miner, name, None)
                        if callable(f):
                            try:
                                ids = f(); break
                            except Exception:
                                pass
            if ids:
                for sidr in ids:
                    lines_refs.append(f"{int(sidr):6d}")
        if lines_refs:
            self.refs.set_lines(lines_refs)

        # Plot + labels
        try:
            self.plot.plot_curve(curve, star_id=sid, pfilter=used_pf)
        except Exception as e:
            logger.warning("Plotting failed: %s", e)

        self.info.set("npts", str(len(pts)))
        if used_pf is not None:
            self.info.set("filter", str(used_pf))
        return False

    # ----- actions -----
    def _on_open_chart(self, _btn: Gtk.Button) -> None:
        try:
            from . import chart  # type: ignore
            chart.show_finding_chart(self, miner=self._miner,
                                     star_id=self.info.star_id,
                                     field_name=self.info.field_name)
        except Exception as e:
            print(f"Finding Chart error: {e}", file=sys.stderr)

    def _on_open_simbad(self, _btn: Gtk.Button) -> None:
        try:
            from . import simbad  # type: ignore
            ra = self.info.ra_hms.replace(":", "h", 1)
            ra = ra.replace(":", "m", 1) + "s" if "m" in ra else self.info.ra_hms
            dec = self.info.dec_dms.replace(":", "d", 1)
            dec = dec.replace(":", "m", 1) + "s" if "m" in dec else self.info.dec_dms
            simbad.coordinate_query(ra, dec)
        except Exception:
            print("SIMBAD lookup not available.", file=sys.stderr)

__all__ = ["StarDetailsWindow"]
