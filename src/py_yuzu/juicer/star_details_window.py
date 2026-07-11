# juicer/star_details_window.py
from __future__ import annotations
import math
import logging
import os
import csv
import sys
import datetime as _dt
from typing import Any, List, Optional, Tuple, Union, Dict, Sequence

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk  # type: ignore

from .light_curve_view import LightCurveView
from .star_info_panel import StarInfoPanel
from .comparison_stars_table import ReferenceStarsTable
from .light_curve_points_table import CurvePointsTable

logger = logging.getLogger(__name__)

# ---------------- configuration plumbing ----------------
try:
    from .conf_manager import cfg  # type: ignore
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

# Export settings
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
            return "?"
        return f"{float(x):.4f}"
    except Exception:
        return str(x)

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

def _pick_num(d: Dict[str, Any], keys: Sequence[str]) -> Optional[float]:
    for k in keys:
        if k in d and d[k] is not None:
            f = _to_float(d[k])
            if f is not None:
                return f
    return None

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

# ---------------- StarDetailsWindow ----------------
class StarDetailsWindow(Gtk.Window):
    """
    StarDetailsWindow(parent: Gtk.Window, row_as_dict: dict, miner: LEMONdBMiner)
    """
    def __init__(self, parent: Gtk.Window, row_as_dict: dict, miner: Any, **_kwargs) -> None:
        _install_theme_css()

        sid = None
        try:
            sid = int(row_as_dict.get("id"))
        except Exception:
            pass

        super().__init__(
            transient_for=parent,
            modal=True,
            title=(f"Star {sid}" if isinstance(sid, int) and sid >= 0 else "Star details"),
        )
        self.set_hide_on_close(True)
        try:
            w = int(cfg.get("star_window", "width")); h = int(cfg.get("star_window", "height"))  # type: ignore[arg-type]
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
        self._export_rows_refs: List[Tuple[int, Optional[float], Optional[float], Optional[float]]] = []
        self._export_rows_pts:  List[Tuple[int, float, float, Optional[float]]] = []
        self._used_filter: Optional[str] = None

        # cache of cstar_id -> mag for table & export
        self._mag_cache: Dict[int, float] = {}

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
        sc_plot = Gtk.ScrolledWindow.new()
        sc_plot.set_child(self.plot)
        sc_plot.set_hexpand(True); sc_plot.set_vexpand(True)
        sc_plot.add_css_class("lemon-surface")
        self.top_paned.set_start_child(sc_plot)

        self.info = StarInfoPanel()
        self.info.apply_theme_to_buttons()
        if hasattr(self.info, "btn_simbad"):
            self.info.btn_simbad.connect("clicked", self._on_open_simbad)
        if hasattr(self.info, "btn_save"):
            self.info.btn_save.connect("clicked", self._on_export_to_disk)

        sc_info = Gtk.ScrolledWindow.new()
        sc_info.set_child(self.info)
        sc_info.set_hexpand(True); sc_info.set_vexpand(True)
        sc_info.add_css_class("lemon-surface")
        self.top_paned.set_end_child(sc_info)

        # Bottom panels: reference table + data points table
        self.refs   = ReferenceStarsTable("Comparison stars")
        # attach a lookup so mag is filled even if rows lack it
        self.refs.set_mag_lookup(self._lookup_mag)

        self.points = CurvePointsTable("Light curve data points")
        self.bottom_paned.set_start_child(self.refs)
        self.bottom_paned.set_end_child(self.points)

        # Static info (from selection)
        self._populate_static_header()

        # Ratios
        self._ratio_top = _cfg_getfloat("star_window", "ratio_top", 0.60)
        self._ratio_plot = _cfg_getfloat("star_window", "ratio_plot", 0.60)
        self._ratio_bottom_left = _cfg_getfloat("star_window", "ratio_bottom_left", 0.50)
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
        px = self._data.get("x")
        py = self._data.get("y")

        ra_hms  = self._data.get("ra_str")  or (_hms_from_deg(float(ra_deg)) if isinstance(ra_deg,(int,float)) else "")
        dec_dms = self._data.get("dec_str") or (_dms_from_deg(float(dec_deg)) if isinstance(dec_deg,(int,float)) else "")

        if isinstance(ra_deg, (int, float)):
            ra_val = f"{ra_hms} ({float(ra_deg):.5f}°)"
        else:
            ra_val = ra_hms or "?"

        if isinstance(dec_deg, (int, float)):
            dec_val = f"{dec_dms} ({float(dec_deg):.5f}°)"
        else:
            dec_val = dec_dms or "?"

        try:
            self.info.star_id = int(sid)
        except Exception:
            self.info.star_id = -1
        self.info.ra_hms  = ra_hms
        self.info.dec_dms = dec_dms

        field_name = self._data.get("field_name")
        if not field_name:
            field_name = getattr(self._miner, "field_name", "") or getattr(self._miner, "path", "") or ""
        self.info.field_name = str(field_name)

        self.info.set("id", str(self.info.star_id if self.info.star_id >= 0 else "?"))
        self.info.set("ra", ra_val)
        self.info.set("dec", dec_val)
        self.info.set("pixel_x", f"{float(px):.3f}" if px is not None else "?")
        self.info.set("pixel_y", f"{float(py):.3f}" if py is not None else "?")
        self.info.set("imag", _format_mag(imag) if imag is not None else "?")
        self.info.set("filter", str(pfilter) if pfilter is not None else "?")
        self.info.set("npts", str(self._data.get("n_points")) if self._data.get("n_points") is not None else "?")

    # -------- populate panels (helpers) --------
    def _panel_set_refs_rows(self, rows: List[Tuple[int, Optional[float], Optional[float], Optional[float]]]) -> None:
        # rows: (cstar_id, weight, sigma, mag)
        self._export_rows_refs = rows[:]
        self.refs.set_rows(rows)

    def _panel_set_cpoints_rows(self, rows: List[Tuple[int, float, float, Optional[float]]]) -> None:
        self._export_rows_pts = rows[:]
        self.points.set_rows(rows)

    # -------- mag lookup for table (fallback path) --------
    def _lookup_mag(self, star_id: int) -> Optional[float]:
        # cached?
        if star_id in self._mag_cache:
            return self._mag_cache.get(star_id)

        m = None
        miner = self._miner
        pf = self._used_filter

        # Try a few likely miner APIs defensively
        for fn_name in (
            "get_inst_mag", "get_mag", "get_star_mag",
        ):
            fn = getattr(miner, fn_name, None)
            if callable(fn):
                try:
                    m = fn(int(star_id), pf) if fn.__code__.co_argcount >= 3 else fn(int(star_id))  # type: ignore[attr-defined]
                    m = _to_float(m)
                    if m is not None:
                        break
                except Exception:
                    pass

        if m is None:
            # Try a dict info getter and pick a mag-like field
            for fn_name in ("get_star_info", "get_cmp_star_info", "get_star"):
                fn = getattr(miner, fn_name, None)
                if callable(fn):
                    try:
                        info = fn(int(star_id), pf) if fn.__code__.co_argcount >= 3 else fn(int(star_id))  # type: ignore[attr-defined]
                    except Exception:
                        info = None
                    if isinstance(info, dict):
                        m = _pick_num(info, ("imag", "mag", "i_mag", "inst_mag", "mag_i", "mag_inst"))
                        if m is not None:
                            break

        if m is not None:
            self._mag_cache[star_id] = m
        return m

    # -------- dynamic load --------
    def _load_dynamic(self) -> bool:
        sid = self.info.star_id
        if not isinstance(sid, int) or sid < 0:
            return False

        miner = self._miner
        pf_hint = self._data.get("filt")

        # Load points from miner (hint first; else let miner choose dominant filter)
        points: List[Tuple[float, float, Optional[float]]] = []
        used_pf = pf_hint
        try:
            points = list(miner.get_light_curve(int(sid), pf_hint) or [])
        except Exception:
            points = []
        if not points:
            try:
                points = list(miner.get_light_curve(int(sid), None) or [])
                used_pf = None
            except Exception:
                points = []

        self._used_filter = None if used_pf is None else str(used_pf)

        # Build and set curve points table rows
        rows_pts = [(i + 1, t, m, s) for i, (t, m, s) in enumerate(points)]
        self._panel_set_cpoints_rows(rows_pts)

        # Comparison stars: prefer rich info; fallback to raw tuples
        rows_refs: List[Tuple[int, Optional[float], Optional[float], Optional[float]]] = []
        info_rows: Optional[List[dict]] = None
        self._mag_cache.clear()

        try:
            info_rows = miner.get_cmp_stars_info(int(sid), used_pf)
            if not info_rows and used_pf is None:
                info_rows = miner.get_cmp_stars_info(int(sid), None)
        except Exception:
            info_rows = None

        if info_rows:
            for d in info_rows:
                try:
                    cid = int(d.get("cstar_id") or d.get("id") or d.get("star_id"))
                except Exception:
                    continue
                wt   = _to_float(d.get("weight"))
                # treat sigma as stdev alias if that's what we have
                sig  = _pick_num(d, ("sigma", "stdev"))
                mag  = _pick_num(d, ("imag", "mag", "i_mag", "inst_mag", "mag_i", "mag_inst"))
                if mag is not None:
                    self._mag_cache[cid] = mag
                rows_refs.append((cid, wt, sig, mag))
        else:
            # Fallback: weight/sigma only; mag looked up via hook
            try:
                raw = miner.get_cmp_stars(int(sid), used_pf) or []
                if not raw and used_pf is None:
                    raw = miner.get_cmp_stars(int(sid), None) or []
                for tup in raw:
                    try:
                        cid = int(tup[0]); wt = _to_float(tup[1]); sig = _to_float(tup[2])
                    except Exception:
                        continue
                    rows_refs.append((cid, wt, sig, None))  # mag will be resolved by lookup
            except Exception:
                rows_refs = []

        self._panel_set_refs_rows(rows_refs)

        # Plot + labels
        try:
            self.plot.plot_curve(points, star_id=sid, pfilter=self._used_filter)
        except Exception as e:
            logger.warning("Plotting failed: %s", e)

        # Update info panel
        self.info.set("npts", str(len(points)))
        if self._used_filter is not None:
            self.info.set("filter", str(self._used_filter))

        # Standard deviation of magnitudes
        try:
            import statistics as _stats
            mags = [m for (_t, m, _s) in points]
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
        dlg = Gtk.Dialog(title="Export ? choose destination folder",
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

        chosen: dict[str, Optional[str]] = {"path": None}
        loop = GLib.MainLoop()

        def on_resp(_d, resp_id):
            if resp_id == Gtk.ResponseType.OK:
                p = entry.get_text().strip()
                if p:
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
            initial = _LAST_DIR or os.path.expanduser("~")
            base_dir = self._prompt_path_fallback(initial)
            if base_dir:
                self._do_export_to(base_dir)

    def _do_export_to(self, base_dir: str) -> None:
        _cfg_set_persist("export", "last_dir", base_dir)

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
                w.writerow(["id", "weight", "sigma", "mag"])
                for sidr, wt, st, mag in self._export_rows_refs:
                    w.writerow([
                        sidr,
                        "" if wt  is None else f"{wt:.6f}",
                        "" if st  is None else f"{st:.6f}",
                        "" if mag is None else f"{mag:.6f}",
                    ])

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

            # plot PNG ? prefer MPL figure if available
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
