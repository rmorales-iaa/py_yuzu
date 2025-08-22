# juicer/star_details_window.py
from __future__ import annotations
import math
import logging
import os
import csv
import sys
import datetime as _dt
from typing import Any, Iterable, List, Optional, Tuple, Union

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk  # type: ignore

from .light_curve_view import LightCurveView
from .star_info_panel import StarInfoPanel
from .reference_stars_table import ReferenceStarsTable
from .curve_points_table import CurvePointsTable

# Optional legacy frame fallback (we keep a compat list panel)
try:
    from .string_list_frame import StringListFrame  # type: ignore
except Exception:
    StringListFrame = None  # type: ignore

logger = logging.getLogger(__name__)

# ---------------- configuration plumbing ----------------
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

def _cfg_set_persist(section: str, key: str, value: str) -> None:
    """Best-effort update + persist to disk."""
    if not cfg:
        return
    try:
        cfg.set(section, key, value)  # type: ignore[attr-defined]
    except Exception:
        return
    for m in ("save", "flush", "write", "persist", "sync"):
        fn = getattr(cfg, m, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass

_BG        = _cfg_get("theme", "background", "#0b1220")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_ACC       = _cfg_get("theme", "accent",     "#ffd84d")
_BLUE_HDR  = _cfg_get("theme", "section_bg", "#1e3a8a")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

# Export settings (with last-dir memory + portal toggle)
_EXP_SUBDIR   = _cfg_get("export", "subdir_template", "star_{id}")
_EXP_CREATE   = _cfg_get("export", "create_subdir", "true").lower() in {"1", "true", "yes", "on"}
_EXP_REFS_CSV = _cfg_get("export", "refs_filename",  "reference_stars.csv")
_EXP_PTS_CSV  = _cfg_get("export", "points_filename","data_points.csv")
_EXP_PNG      = _cfg_get("export", "plot_filename",  "light_curve.png")
_EXP_DPI      = int(_cfg_getfloat("export", "plot_dpi", 150.0))
_LAST_DIR     = _cfg_get("export", "last_dir", "")

def _install_theme_css() -> None:
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(f"""
            .lemon-surface {{ background: {_BG}; }}
            .lemon-surface * {{ color: {_FG}; }}
            .monospace {{ font-family: {_MONO_FONT}; }}
            .lemon-button {{
                background: {_ACC};
                color: #000;
                border-radius: 10px;
                padding: 6px 10px;
            }}
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

# ---------------- formatting helpers ----------------
def _hms_from_deg(ra_deg: float) -> str:
    ra = (ra_deg / 15.0) % 24.0
    h = int(ra); m = int((ra - h) * 60.0); s = (ra - h - m/60.0) * 3600.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def _dms_from_deg(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else "+"
    dabs = abs(dec_deg)
    d = int(dabs); m = int((dabs - d) * 60.0); s = (dabs - d - m/60.0) * 3600.0
    return f"{sign}{d:02d}:{m:02d}:{s:05.2f}"

def _fmt_time(unix: Union[str, float, int]) -> str:
    if isinstance(unix, str):
        return unix
    try:
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

# ---------------- curve helpers ----------------
def _curve_to_points(curve: Any) -> List[Tuple[float, float, Optional[float]]]:
    if curve is None:
        return []
    try:
        pts: List[Tuple[float, float, Optional[float]]] = []
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
        cs = list(getattr(curve, "cstars"))
        w  = list(getattr(curve, "cweights"))
        st = list(getattr(curve, "cstdevs"))
        if cs and len(cs) == len(w) == len(st):
            return [(int(s), float(ww), float(ss)) for s, ww, ss in zip(cs, w, st)]
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

# ---------------- miner/DB helpers ----------------
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

# ---- SQLAlchemy helpers ----
def _build_sa_session_from_path(db_path: str):
    try:
        from sqlalchemy import create_engine  # type: ignore
        from sqlalchemy.orm import sessionmaker  # type: ignore
        db_path = os.path.abspath(db_path)
        eng = create_engine(f"sqlite:///{db_path}", future=True)
        Session = sessionmaker(bind=eng, future=True)
        return Session()
    except Exception:
        return None

def _get_sa_session(miner: Any, parent: Optional[Gtk.Window] = None):
    if miner is None:
        return None
    def pick_from(obj: Any):
        if obj is None:
            return None
        for name in ("sa_session", "session", "db_session", "orm_session"):
            sess = getattr(obj, name, None)
            if sess is None:
                continue
            if callable(sess):
                try:
                    tmp = sess()
                    if hasattr(tmp, "execute"):
                        return tmp
                except Exception:
                    pass
            if hasattr(sess, "execute"):
                return sess
        for name in ("session_factory",):
            fac = getattr(obj, name, None)
            if callable(fac):
                try:
                    tmp = fac()
                    if hasattr(tmp, "execute"):
                        return tmp
                except Exception:
                    pass
        for name in ("engine", "sa_engine"):
            eng = getattr(obj, name, None)
            if eng is not None:
                try:
                    from sqlalchemy.orm import sessionmaker  # type: ignore
                    Session = sessionmaker(bind=eng, future=True)
                    tmp = Session()
                    if hasattr(tmp, "execute"):
                        return tmp
                except Exception:
                    pass
        return None
    sess = pick_from(miner) or pick_from(getattr(miner, "db", None))
    if sess is not None:
        return sess
    db_path = _find_db_path(miner, parent) or (getattr(parent, "_db_path", None) if parent else None)
    if db_path:
        return _build_sa_session_from_path(str(db_path))
    return None

def _collect_via_sqlalchemy(miner: Any, star_id: int, parent: Optional[Gtk.Window] = None) -> Tuple[Any | None, Any | None]:
    sess = getattr(miner, "sa_session", None)
    owns_session = False
    if sess is None:
        sess = _get_sa_session(miner, parent)
        if sess is not None:
            owns_session = True
            try:
                if getattr(miner, "sa_session", None) is None:
                    setattr(miner, "sa_session", sess)
            except Exception:
                pass
    if sess is None:
        return None, None
    try:
        from sqlalchemy import text  # type: ignore
    except Exception:
        if owns_session:
            try: sess.close()
            except Exception: pass
        return None, None

    def first_map(sql: str, params: dict):
        try:
            res = sess.execute(text(sql), params).mappings().first()
            if res is not None:
                return res
        except Exception:
            pass
        try:
            row = sess.execute(text(sql), params).fetchone()
            if row is None:
                return None
            class W:
                def __init__(self, r): self.r = r
                def get(self, k, default=None):
                    try:
                        return getattr(self.r, k)
                    except Exception:
                        return default
            return W(row)
        except Exception:
            return None

    pick_lc = """
        SELECT img.filter_id AS fid, COALESCE(pf.name, '(none)') AS fname, COUNT(*) AS n
        FROM light_curves lc
        JOIN images img ON lc.image_id = img.id
        LEFT JOIN photometric_filters pf ON img.filter_id = pf.id
        WHERE lc.star_id = :sid
        GROUP BY img.filter_id, pf.name
        ORDER BY n DESC
        LIMIT 1
    """
    row = first_map(pick_lc, {"sid": int(star_id)})
    used_table = "light_curves"

    if row is None:
        pick_ph = """
            SELECT img.filter_id AS fid, COALESCE(pf.name, '(none)') AS fname, COUNT(*) AS n
            FROM photometry ph
            JOIN images img ON ph.image_id = img.id
            LEFT JOIN photometric_filters pf ON img.filter_id = pf.id
            WHERE ph.star_id = :sid
            GROUP BY img.filter_id, pf.name
            ORDER BY n DESC
            LIMIT 1
        """
        row = first_map(pick_ph, {"sid": int(star_id)})
        used_table = "photometry"

    if row is None:
        if owns_session:
            try: sess.close()
            except Exception: pass
        return None, None

    fid = row.get("fid", None)
    used_filter_name = row.get("fname")

    if used_table == "light_curves":
        if fid is None:
            pts_sql = """
                SELECT img.unix_time, lc.magnitude, lc.snr
                FROM light_curves lc
                JOIN images img ON lc.image_id = img.id
                WHERE lc.star_id = :sid AND img.filter_id IS NULL
                ORDER BY img.unix_time ASC
            """
            pts = sess.execute(text(pts_sql), {"sid": int(star_id)}).fetchall()
        else:
            pts_sql = """
                SELECT img.unix_time, lc.magnitude, lc.snr
                FROM light_curves lc
                JOIN images img ON lc.image_id = img.id
                WHERE lc.star_id = :sid AND img.filter_id = :fid
                ORDER BY img.unix_time ASC
            """
            pts = sess.execute(text(pts_sql), {"sid": int(star_id), "fid": int(fid)}).fetchall()
    else:
        if fid is None:
            pts_sql = """
                SELECT img.unix_time, ph.magnitude, ph.snr
                FROM photometry ph
                JOIN images img ON ph.image_id = img.id
                WHERE ph.star_id = :sid AND img.filter_id IS NULL
                ORDER BY img.unix_time ASC
            """
            pts = sess.execute(text(pts_sql), {"sid": int(star_id)}).fetchall()
        else:
            pts_sql = """
                SELECT img.unix_time, ph.magnitude, ph.snr
                FROM photometry ph
                JOIN images img ON ph.image_id = img.id
                WHERE ph.star_id = :sid AND img.filter_id = :fid
                ORDER BY img.unix_time ASC
            """
            pts = sess.execute(text(pts_sql), {"sid": int(star_id), "fid": int(fid)}).fetchall()

    if fid is None:
        refs = []
    else:
        refs_sql = """
            SELECT cstar_id, weight, stdev
            FROM cmp_stars
            WHERE star_id = :sid AND filter_id = :fid
            ORDER BY cstar_id
        """
        refs = sess.execute(text(refs_sql), {"sid": int(star_id), "fid": int(fid)}).fetchall()

    if owns_session:
        try: sess.close()
        except Exception: pass

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
        def weights(self):
            return zip(self.cstars, self.cweights, self.cstdevs)

    return _LC(pts, refs, used_filter_name), used_filter_name

def _collect_via_lemondb_path(db_path: str, star_id: int) -> Tuple[Any | None, Any | None]:
    try:
        from . import database  # type: ignore
    except Exception:
        try:
            import database  # type: ignore
        except Exception:
            return None, None
    try:
        db = database.LEMONdB(db_path)  # type: ignore
    except Exception:
        return None, None
    try:
        for pf in db.filters():
            cur = db.get_light_curve(star_id, pf)
            if _curve_to_points(cur):
                return cur, pf
    except Exception:
        pass
    return None, None

# ---------------- internal compat list panel ----------------
class _CompatStringListPanel(Gtk.Box):
    def __init__(self, title: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        _install_theme_css()
        self.add_css_class("lemon-surface")

        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.add_css_class("section-header")
        lbl = Gtk.Label(label=title, xalign=0.0)
        lbl.add_css_class("monospace")
        lbl.set_hexpand(True)
        hdr.append(lbl)
        self.append(hdr)

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
        self.append(frame_box)

    def set_lines(self, lines: List[str]) -> None:
        self._store.splice(0, self._store.get_n_items(), [])
        for ln in lines:
            self._store.append(Gtk.StringObject.new(ln))

# ---------------- StarDetailsWindow ----------------
class StarDetailsWindow(Gtk.Window):
    """
    StarDetailsWindow(parent: Gtk.Window, row_as_dict: dict, miner: LEMONdBMiner)
    """
    def __init__(self, parent: Gtk.Window, row_as_dict: dict, miner: Any, **_kwargs) -> None:
        _install_theme_css()

        sid = None
        try: sid = int(row_as_dict.get("id"))
        except Exception: pass

        super().__init__(transient_for=parent, modal=True,
                         title=(f"Star {sid}" if isinstance(sid, int) and sid >= 0 else "Star details"))
        self.set_hide_on_close(True)
        try:
            w = int(cfg.get("star_window","width")); h = int(cfg.get("star_window","height"))  # type: ignore[arg-type]
        except Exception:
            w, h = 1080, 720
        self.set_default_size(w, h)
        try:
            cfg.attach_window(self, "star_window")  # type: ignore[attr-defined]
        except Exception:
            pass

        self._parent = parent
        self._miner  = miner
        self._data   = dict(row_as_dict or {})
        self._export_rows_refs: List[Tuple[int, Optional[float], Optional[float]]] = []
        self._export_rows_pts:  List[Tuple[int, float, float, Optional[float]]] = []
        self._used_filter: Optional[str] = None

        # Layout
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        root.set_margin_top(8); root.set_margin_bottom(8)
        root.set_margin_start(8); root.set_margin_end(8)
        root.add_css_class("lemon-surface")
        self.set_child(root)

        self.vpaned = Gtk.Paned.new(Gtk.Orientation.VERTICAL)
        self.vpaned.set_resize_start_child(True); self.vpaned.set_resize_end_child(True); self.vpaned.set_wide_handle(True)
        root.append(self.vpaned)

        self.top_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.top_paned.set_resize_start_child(True); self.top_paned.set_resize_end_child(True); self.top_paned.set_wide_handle(True)
        self.vpaned.set_start_child(self.top_paned)

        self.bottom_paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        self.bottom_paned.set_resize_start_child(True); self.bottom_paned.set_resize_end_child(True); self.bottom_paned.set_wide_handle(True)
        self.vpaned.set_end_child(self.bottom_paned)

        # Top: plot + info
        self.plot = LightCurveView()
        sc_plot = Gtk.ScrolledWindow.new(); sc_plot.set_child(self.plot); sc_plot.set_hexpand(True); sc_plot.set_vexpand(True); sc_plot.add_css_class("lemon-surface")
        self.top_paned.set_start_child(sc_plot)

        self.info = StarInfoPanel()
        self.info.apply_theme_to_buttons()
        if hasattr(self.info, "btn_simbad"):
            self.info.btn_simbad.connect("clicked", self._on_open_simbad)
        if hasattr(self.info, "btn_save"):
            self.info.btn_save.connect("clicked", self._on_export_to_disk)

        sc_info = Gtk.ScrolledWindow.new(); sc_info.set_child(self.info); sc_info.set_hexpand(True); sc_info.set_vexpand(True); sc_info.add_css_class("lemon-surface")
        self.top_paned.set_end_child(sc_info)

        # Bottom panels: reference table + data points table
        try:
            refs_panel = ReferenceStarsTable("Reference stars")
        except Exception:
            refs_panel = None
        try:
            points_panel = CurvePointsTable("Data points")
        except Exception:
            points_panel = None

        def _ensure_panel(panel: Any, title: str) -> Any:
            if panel is None:
                return _CompatStringListPanel(title)
            if any(hasattr(panel, name) for name in ("set_rows", "set_lines")):
                return panel
            return _CompatStringListPanel(title)

        self.refs   = _ensure_panel(refs_panel,   "Reference stars")
        self.points = _ensure_panel(points_panel, "Data points")

        self.bottom_paned.set_start_child(self.refs)
        self.bottom_paned.set_end_child(self.points)

        # Static info (from selection)
        self._populate_static_header()

        # Ratios
        self._ratio_top = _cfg_getfloat("star_window","ratio_top",0.60)
        self._ratio_plot = _cfg_getfloat("star_window","ratio_plot",0.60)
        self._ratio_bottom_left = _cfg_getfloat("star_window","ratio_bottom_left",0.50)
        self.connect("show", lambda *_: GLib.idle_add(self._apply_ratios))

        # Dynamic load
        GLib.idle_add(lambda: (self._load_dynamic() or False))

    # -------- geometry ratios --------
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

    # -------- header from static dict --------
    def _populate_static_header(self) -> None:
        sid = self._data.get("id")
        ra_deg = self._data.get("ra_deg")
        dec_deg = self._data.get("dec_deg")
        imag = self._data.get("mag")
        pfilter = self._data.get("filt")

        ra_str  = self._data.get("ra_str")  or (_hms_from_deg(float(ra_deg)) if isinstance(ra_deg,(int,float)) else "")
        dec_str = self._data.get("dec_str") or (_dms_from_deg(float(dec_deg)) if isinstance(dec_deg,(int,float)) else "")

        try: self.info.star_id = int(sid)
        except Exception: self.info.star_id = -1
        self.info.ra_hms  = ra_str
        self.info.dec_dms = dec_str
        self.info.field_name = self._data.get("field_name", "") or str(_find_db_path(self._miner, self._parent) or "")

        self.info.set("id", str(self.info.star_id if self.info.star_id >= 0 else "—"))
        self.info.set("ra", ra_str or "—")
        self.info.set("dec", dec_str or "—")
        self.info.set("imag", _format_mag(imag) if imag is not None else "—")
        self.info.set("filter", str(pfilter) if pfilter is not None else "—")
        self.info.set("npts", str(self._data.get("n_points")) if self._data.get("n_points") is not None else "—")

    # -------- populate panels (helpers) --------
    def _panel_set_lines(self, panel: Any, title: str, lines: List[str], side: str) -> None:
        if hasattr(panel, "set_lines") and callable(getattr(panel, "set_lines")):
            try:
                panel.set_lines(lines)
                return
            except Exception:
                pass
        compat = _CompatStringListPanel(title)
        if side == "start":
            self.bottom_paned.set_start_child(compat)
            self.refs = compat
        else:
            self.bottom_paned.set_end_child(compat)
            self.points = compat
        compat.set_lines(lines)

    def _panel_set_refs_rows(self, rows: List[Tuple[int, Optional[float], Optional[float]]]) -> None:
        self._export_rows_refs = rows[:]  # keep for export
        if not rows:
            if hasattr(self.refs, "clear"):
                try: self.refs.clear(); return
                except Exception: pass
            self._panel_set_lines(self.refs, "Reference stars", [], side="start")
            return
        if hasattr(self.refs, "set_rows"):
            try: self.refs.set_rows(rows); return
            except Exception: pass
        # fallback as lines
        lines = []
        for sidr, wt, st in rows:
            wtxt = "—" if wt is None else f"{wt:.4f}"
            stxt = "—" if st is None else f"{st:.4f}"
            lines.append(f"{sidr:5d}    w={wtxt:>8}    σ={stxt:>8}")
        self._panel_set_lines(self.refs, "Reference stars", lines, side="start")

    def _panel_set_cpoints_rows(self, rows: List[Tuple[int, float, float, Optional[float]]]) -> None:
        self._export_rows_pts = rows[:]  # keep for export
        if not rows:
            if hasattr(self.points, "clear"):
                try: self.points.clear(); return
                except Exception: pass
            self._panel_set_lines(self.points, "Data points", [], side="end")
            return
        if hasattr(self.points, "set_rows"):
            try: self.points.set_rows(rows); return
            except Exception: pass
        # fallback as lines
        lines = []
        for idx, t, mag, snr in rows:
            dt = _fmt_time(t)
            snrt = "—" if snr is None else f"{float(snr):.1f}"
            lines.append(f"{idx:03d}  {dt}    mag={_format_mag(mag)}    SNR={snrt}")
        self._panel_set_lines(self.points, "Data points", lines, side="end")

    # -------- dynamic load --------
    def _load_dynamic(self) -> bool:
        sid = self.info.star_id
        if not isinstance(sid, int) or sid < 0:
            return False
        miner = self._miner

        curve = None
        used_pf = None
        pf_hint = self._data.get("filt")

        # Miner (hint)
        if miner is not None and pf_hint is not None:
            for name in ("get_light_curve", "get_curve", "light_curve_for_star",
                         "lightcurve_for_star", "light_curve"):
                f = getattr(miner, name, None)
                if callable(f):
                    try:
                        tmp = f(int(sid), pf_hint)
                        if _curve_to_points(tmp):
                            curve, used_pf = tmp, pf_hint
                            logger.info("Star %s: loader=miner(hint), pts=%d, filt=%s",
                                        sid, len(_curve_to_points(curve)), used_pf)
                            break
                    except Exception:
                        pass

        # Miner (no hint)
        if not _curve_to_points(curve) and miner is not None:
            try:
                tmp_curve, tmp_pf = _collect_best_light_curve(miner, sid)
                if _curve_to_points(tmp_curve):
                    curve, used_pf = tmp_curve, tmp_pf
                    logger.info("Star %s: loader=miner, pts=%d, filt=%s",
                                sid, len(_curve_to_points(curve)), used_pf)
            except Exception:
                pass

        # SQLAlchemy
        if not _curve_to_points(curve):
            try:
                tmp_curve, tmp_pf = _collect_via_sqlalchemy(miner, sid, parent=self._parent)
                if _curve_to_points(tmp_curve):
                    curve, used_pf = tmp_curve, tmp_pf
                    logger.info("Star %s: loader=sqlalchemy, pts=%d, filt=%s",
                                sid, len(_curve_to_points(curve)), used_pf)
            except Exception as e:
                logger.warning("Star %s: sqlalchemy failed: %s", sid, e)

        # LEMONdB fallback
        if not _curve_to_points(curve):
            db_path = _find_db_path(miner, self._parent) if miner is not None else getattr(self._parent, "_db_path", None)
            if db_path:
                try:
                    tmp_curve, tmp_pf = _collect_via_lemondb_path(str(db_path), sid)
                    if _curve_to_points(tmp_curve):
                        curve, used_pf = tmp_curve, tmp_pf
                        logger.info("Star %s: loader=lemondb, pts=%d, filt=%s",
                                    sid, len(_curve_to_points(curve)), used_pf)
                except Exception as e:
                    logger.warning("Star %s: lemondb failed: %s", sid, e)

        self._used_filter = None if used_pf is None else str(used_pf)

        # Build rows & populate tables
        pts = _curve_to_points(curve)
        rows_pts = [(i + 1, t, m, s) for i, (t, m, s) in enumerate(pts)]
        self._panel_set_cpoints_rows(rows_pts)

        rows_refs: List[Tuple[int, Optional[float], Optional[float]]] = []
        w = _curve_cstars_with_weights(curve)
        if w:
            for sidr, wt, st in w:
                rows_refs.append((int(sidr), float(wt), float(st)))
        else:
            ids = _curve_cstars_ids_only(curve)
            if not ids and miner is not None:
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
            if ids:
                for sidr in ids:
                    rows_refs.append((int(sidr), None, None))
        self._panel_set_refs_rows(rows_refs)

        # Plot + labels
        try:
            self.plot.plot_curve(curve, star_id=sid, pfilter=self._used_filter)
        except Exception as e:
            logger.warning("Plotting failed: %s", e)

        self.info.set("npts", str(len(pts)))
        if self._used_filter is not None:
            self.info.set("filter", str(self._used_filter))

        # stdev in panel
        try:
            import statistics as _stats
            mags = [m for (_t, m, _s) in pts]
            stdev = _stats.stdev(mags) if len(mags) > 1 else 0.0
        except Exception:
            stdev = None
        self.info.set("stdev", stdev)

        return False

    # -------- actions --------
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

    # -------- save/export --------
    def _prompt_path_fallback(self, initial_dir: str) -> Optional[str]:
        """
        Simple in-app folder entry dialog (no portal / no GVfs),
        returns a directory path or None if cancelled.
        """
        dlg = Gtk.Dialog(title="Export — choose destination folder",
                         transient_for=self, modal=True)
        dlg.add_css_class("lemon-surface")
        dlg.add_button("_Cancel", Gtk.ResponseType.CANCEL)
        dlg.add_button("_Save", Gtk.ResponseType.OK)

        box = dlg.get_content_area()
        box.set_margin_top(12); box.set_margin_bottom(12)
        box.set_margin_start(12); box.set_margin_end(12)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.add_css_class("lemon-surface")

        entry = Gtk.Entry()
        entry.set_hexpand(True)
        entry.set_text(initial_dir or os.path.expanduser("~"))
        entry.add_css_class("monospace")

        cb_create = Gtk.CheckButton(label="Create directory if it does not exist")
        cb_create.set_active(True if _EXP_CREATE else False)

        v.append(Gtk.Label(label="Destination folder (server path):", xalign=0.0))
        v.append(entry)
        v.append(cb_create)
        box.append(v)

        # run dialog synchronously using a nested loop
        chosen: dict[str, Optional[str]] = {"path": None}
        loop = GLib.MainLoop()

        def on_resp(_d, resp_id):
            if resp_id == Gtk.ResponseType.OK:
                p = entry.get_text().strip()
                if p:
                    # Expand and normalize
                    p = os.path.abspath(os.path.expanduser(p))
                    if not os.path.isdir(p):
                        if cb_create.get_active():
                            try:
                                os.makedirs(p, exist_ok=True)
                            except Exception as e:
                                logger.error("Cannot create directory %s: %s", p, e)
                                p = None
                        else:
                            p = None
                    chosen["path"] = p
            try:
                _d.destroy()
            except Exception:
                pass
            loop.quit()

        dlg.connect("response", on_resp)
        dlg.present()
        loop.run()
        return chosen["path"]

    def _on_export_to_disk(self, _btn: Gtk.Button) -> None:
        """Open a folder picker (portal or fallback) and export refs.csv, points.csv, and plot.png."""

        # 1) Portal/GTK FileDialog path (may warn on some remotes)
        dialog = Gtk.FileDialog(title="Choose output folder")
        try:
            base = _LAST_DIR or os.path.expanduser("~")
            if base and os.path.isdir(base):
                dialog.set_initial_folder(Gio.File.new_for_path(base))
        except Exception:
            pass

        def on_done(dlg: Gtk.FileDialog, result: Gio.AsyncResult):
            try:
                folder: Gio.File = dlg.select_folder_finish(result)
            except GLib.Error:
                return  # cancelled
            except Exception as e:
                logger.error("Folder dialog error: %s", e); return

            base_dir = folder.get_path()
            if not base_dir:
                return
            self._do_export_to(base_dir)

        try:
            dialog.select_folder(self, None, on_done)
        except Exception as e:
            logger.error("Folder dialog init failed: %s", e)
            # Fallback immediately
            initial = _LAST_DIR or os.path.expanduser("~")
            base_dir = self._prompt_path_fallback(initial)
            if base_dir:
                self._do_export_to(base_dir)

    def _do_export_to(self, base_dir: str) -> None:
        """Perform the actual export to base_dir (remember last_dir, build subdir, write files)."""
        # Remember last dir
        _cfg_set_persist("export", "last_dir", base_dir)

        # Build final output dir (respect create_subdir)
        out_dir = base_dir
        try:
            if _EXP_CREATE:
                subdir = _EXP_SUBDIR.format(id=self.info.star_id, filter=(self._used_filter or ""))
                out_dir = os.path.join(base_dir, subdir)
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            logger.error("Cannot create output dir: %s", e); return

        try:
            # refs CSV
            refs_csv = os.path.join(out_dir, _EXP_REFS_CSV)
            with open(refs_csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "weight", "sigma"])
                for sidr, wt, st in self._export_rows_refs:
                    w.writerow([sidr,
                                "" if wt is None else f"{wt:.6f}",
                                "" if st is None else f"{st:.6f}"])

            # points CSV
            pts_csv = os.path.join(out_dir, _EXP_PTS_CSV)
            with open(pts_csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "epoch_utc", "date_utc", "magnitude", "snr"])
                for idx, t, mag, snr in self._export_rows_pts:
                    dt = _fmt_time(t)
                    w.writerow([idx, f"{float(t):.6f}", dt,
                                f"{float(mag):.6f}",
                                "" if snr is None else f"{float(snr):.3f}"])

            # plot PNG — prefer MPL figure if available
            png_path = os.path.join(out_dir, _EXP_PNG)
            saved = False
            try:
                fig = getattr(self.plot, "_fig", None)
                if fig is not None:
                    fig.savefig(png_path, dpi=_EXP_DPI, facecolor=_BG, bbox_inches="tight")
                    saved = True
            except Exception:
                pass
            if not saved:
                try:
                    pic = getattr(self.plot, "_picture", None)
                    if pic is not None:
                        paint = pic.get_paintable()
                        if isinstance(paint, Gdk.Texture):
                            paint.save_to_png(png_path)
                            saved = True
                except Exception:
                    pass

            logger.info("Exported CSVs and plot to: %s", out_dir)
        except Exception as e:
            logger.error("Export failed: %s", e)
