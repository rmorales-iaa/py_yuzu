# juicer/window.py
from __future__ import annotations
import importlib
import importlib.util
import logging
import math
import sys
from pathlib import Path
from typing import Optional, Type

# ==== GI / GTK4 ====
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GObject", "2.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gio, GLib, Gdk, GObject, Pango, Graphene  # type: ignore

# Optional config (rebound via --config)
try:
    from conf_manager.conf_manager import cfg  # type: ignore
except Exception:
    cfg = None  # type: ignore

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent
for p in (str(PKG_DIR), str(ROOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Models + details window (both programmatic, no .ui)
from .models import StarRow
from .star_details_window import StarDetailsWindow


# ---------------- GLib helper ----------------
def _remove_source_if_alive(sid: int) -> bool:
    try:
        if not isinstance(sid, int) or sid <= 0:
            return False
        ctx = GLib.MainContext.default()
        if ctx is None:
            return GLib.Source.remove(sid)
        src = ctx.find_source_by_id(sid)
        if src is None:
            return False
        src.destroy()
        return True
    except Exception:
        try:
            return GLib.Source.remove(sid)
        except Exception:
            return False


# ---------------- Config helpers ----------------
def _cfg_get(section: str, key: str, default: str) -> str:
    try:
        return cfg.get(section, key) if cfg else default
    except Exception:
        return default

def _cfg_get_int(section: str, key: str, default: int) -> int:
    try:
        return int(cfg.get(section, key)) if cfg else default
    except Exception:
        return default


# ---------------- Import miner ----------------
def _import_miner_class() -> Type:
    last_exc: Exception | None = None
    try:
        from .miner import LEMONdBMiner  # type: ignore
        return LEMONdBMiner
    except Exception as e1:
        last_exc = e1
    try:
        from juicer.miner import LEMONdBMiner  # type: ignore
        return LEMONdBMiner
    except Exception as e2:
        last_exc = e2
    candidate = PKG_DIR / "miner.py"
    if candidate.exists():
        spec = importlib.util.spec_from_file_location("juicer.mining", candidate)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["juicer.mining"] = mod
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            if hasattr(mod, "LEMONdBMiner"):
                return getattr(mod, "LEMONdBMiner")
            last_exc = RuntimeError("miner.py loaded but LEMONdBMiner not found")
    raise RuntimeError(
        "mining module not available. Ensure juicer/miner.py exists and is importable.\n"
        f"Last import error: {last_exc}"
    )


# ---------------- Sexagesimal formatters ----------------
def _hms_from_deg(ra_deg: float) -> str:
    ra = (ra_deg / 15.0) % 24.0
    h = int(ra); m = int((ra - h) * 60.0)
    s = (ra - h - m / 60.0) * 3600.0
    return f"{h:02d}:{m:02d}:{s:06.3f}"

def _dms_from_deg(dec_deg: float) -> str:
    sign = "-" if dec_deg < 0 else "+"
    dabs = abs(dec_deg)
    d = int(dabs); m = int((dabs - d) * 60.0)
    s = (dabs - d - m / 60.0) * 3600.0
    return f"{sign}{d:02d}:{m:02d}:{s:05.2f}"


# ---------------- Main Window ----------------
class LEMONJuicerWindow(Gtk.ApplicationWindow):
    """GTK4 window with a ColumnView listing stars from the DB (no .ui files)."""

    def __init__(self, app: Gtk.Application, title_suffix: str = "",
                 autoselect_radec: Optional[tuple[float, float]] = None):
        # Geometry from config
        width  = _cfg_get_int("main_window", "width", 1200)
        height = _cfg_get_int("main_window", "height", 800)
        super().__init__(application=app, title=f"yuzu juicer{title_suffix}")
        self.set_default_size(width, height)

        # One-shot autoselection (degrees)
        self._autoselect_radec: Optional[tuple[float, float]] = getattr(
            self, "_autoselect_radec", autoselect_radec
        )
        self._autoselect_done: bool = False

        self._star_win: Optional[Gtk.Window] = None

        # Selection memory + reveal scheduling
        self._selected_star_id: Optional[int] = None
        self._reselect_idle_id: Optional[int] = None
        self._reselect_guard_id: Optional[int] = None
        self._reveal_retry_ids: list[int] = []

        # Scrolling helpers
        self._row_height_px: Optional[int] = None
        self._scroller: Optional[Gtk.ScrolledWindow] = None
        self._row_widgets: dict[int, Gtk.Widget] = {}     # sorted index -> row widget
        self._inner_listview: Optional[Gtk.ListView] = None

        # ---- Theme from [theme] ----
        _FONT_NAME   = _cfg_get("theme", "font_name", "Cantarell")
        _FONT_SIZEPT = _cfg_get_int("theme", "font_size", 11)
        _BG          = _cfg_get("theme", "background", "#2a2f38")
        _FG          = _cfg_get("theme", "foreground", "#e7ebef")
        _ACCENT      = _cfg_get("theme", "accent",    "#ffd84d")
        _DIVIDER     = _cfg_get("theme", "divider",   "#ffd84d")
        _DIV_W       = _cfg_get_int("theme", "divider_width_px", 1)
        _SEL_BG      = _cfg_get("theme", "row_selected", "#375a7f")
        _SEL_FG      = _ACCENT
        _HEADER_BG        = "#1f4aa8"
        _HEADER_BG_HOVER  = "#2456bf"
        _HEADER_BG_ACTIVE = "#1a3e8f"

        # ---------- CSS ----------
        provider = Gtk.CssProvider()
        css = f"""
        * {{ font-family: '{_FONT_NAME}'; font-size: {_FONT_SIZEPT}pt; }}
        columnview.lemon-dark listview {{ background-color: {_BG}; }}
        columnview.lemon-dark label {{ color: {_FG}; }}
        columnview.lemon-dark listview row:selected,
        columnview.lemon-dark listitem:selected {{ background-color: {_SEL_BG}; }}
        columnview.lemon-dark listview row:selected label,
        columnview.lemon-dark listitem:selected label {{ color: {_SEL_FG}; }}
        .lemon-vsep {{ background-color: {_DIVIDER}; margin-left: 6px; }}
        .lemon-last .lemon-vsep {{ background-color: transparent; margin-left: 0; }}
        columnview.lemon-dark > header, columnview.lemon-dark > header > * {{
          background-color: {_HEADER_BG}; color: #fff;
        }}
        columnview.lemon-dark > header button {{
          background-color: {_HEADER_BG}; color: #fff; font-weight: 600;
          padding: 6px 8px; border-right: 1px solid {_DIVIDER}; border-top: 0; border-bottom: 0;
        }}
        columnview.lemon-dark > header button:hover {{ background-color: {_HEADER_BG_HOVER}; }}
        columnview.lemon-dark > header button:active,
        columnview.lemon-dark > header button:checked {{ background-color: {_HEADER_BG_ACTIVE}; }}
        .lemon-statusbar {{ background-color: {_BG}; padding: 6px 10px; }}
        .lemon-statusbar * {{ color: {_ACCENT}; }}
        """
        provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
        )

        # ---------- HeaderBar ----------
        hb = Gtk.HeaderBar.new()
        self.set_titlebar(hb)
        open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        open_btn.set_tooltip_text("Open LEMON database?")
        open_btn.connect("clicked", self._on_open_clicked)
        hb.pack_start(open_btn)

        # ---------- Root ----------
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_hexpand(True); root.set_vexpand(True)
        self.set_child(root)

        # ---------- Models ----------
        self._rows = Gio.ListStore.new(StarRow)
        self._sort_model = Gtk.SortListModel.new(model=self._rows, sorter=None)
        self._sel = Gtk.SingleSelection.new(self._sort_model)
        try: self._sel.set_autoselect(True)
        except Exception: pass
        self._sel.set_can_unselect(False)
        self._sel.connect("selection-changed", self._on_selection_changed)

        # ---------- ColumnView ----------
        self._view = Gtk.ColumnView.new(self._sel)
        self._view.add_css_class("lemon-dark")
        self._view.set_hexpand(True); self._view.set_vexpand(True)
        self._view.connect("activate", self._on_view_activate)  # double-click / Enter

        # Reveal retries when mapped / resized
        self._view.connect("map", lambda *_: GLib.idle_add(self._reveal_current_selection))
        try:
            self._view.connect("size-allocate", lambda *_a: GLib.idle_add(self._reveal_current_selection))
        except Exception:
            pass
        self._sort_model.connect("items-changed", lambda *_: self._request_reselect())
        try:
            sorter = self._view.get_sorter()
            if sorter is not None:
                sorter.connect("changed", lambda *_: self._request_reselect())
        except Exception:
            pass
        try:
            self._view.connect("notify::sorter", lambda *_: self._request_reselect())
        except Exception:
            pass

        # ---------- Column widths ----------
        _W_ID  = _cfg_get_int("main_columns", "id", 105)
        _W_RA  = _cfg_get_int("main_columns", "ra", 164)
        _W_DEC = _cfg_get_int("main_columns", "dec", 240)
        _W_MAG = _cfg_get_int("main_columns", "mag", 150)

        def make_text_column(title: str, get_text,
                             *, width_px: int | None,
                             xalign: float, monospace: bool = True,
                             expand: bool = False,
                             is_last: bool = False) -> Gtk.ColumnViewColumn:
            factory = Gtk.SignalListItemFactory()

            def setup(_f, li: Gtk.ListItem):
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
                if is_last: box.add_css_class("lemon-last")
                lbl = Gtk.Label(xalign=xalign)
                lbl.set_single_line_mode(True)
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                if monospace: lbl.add_css_class("monospace")
                lbl.set_hexpand(True)
                box.append(lbl)
                sep = Gtk.Box()
                sep.add_css_class("lemon-vsep")
                sep.set_hexpand(False); sep.set_vexpand(True)
                sep.set_size_request(max(1, _CFG_DIV_W()), -1)
                if is_last: sep.set_visible(False)
                box.append(sep)
                li.set_child(box)

            def bind(_f, li: Gtk.ListItem):
                row: StarRow | None = li.get_item()
                txt = get_text(row) if row is not None else ""
                box = li.get_child()
                lbl = box.get_first_child()
                if isinstance(lbl, Gtk.Label):
                    lbl.set_text(txt)

                # Track row widget
                try:
                    pos = li.get_position()
                    if isinstance(pos, int) and pos >= 0:
                        self._row_widgets[pos] = box
                except Exception:
                    pass

                # First height measurement
                try:
                    alloc = box.get_allocation()
                    h = getattr(alloc, "height", 0) or 0
                    if h > 0 and self._row_height_px is None:
                        self._row_height_px = int(h)
                except Exception:
                    pass

            def unbind(_f, li: Gtk.ListItem):
                try:
                    box = li.get_child()
                    for k, v in list(self._row_widgets.items()):
                        if v is box:
                            self._row_widgets.pop(k, None)
                except Exception:
                    pass

            factory.connect("setup", setup)
            factory.connect("bind", bind)
            factory.connect("unbind", unbind)

            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(expand)
            if width_px is not None and not expand:
                col.set_fixed_width(width_px)
            return col

        def _CFG_DIV_W() -> int:
            try:
                return _cfg_get_int("theme", "divider_width_px", 1)
            except Exception:
                return 1

        # Columns
        col_id = make_text_column(
            "ID",
            lambda r: "" if r is None else f"{int(getattr(r.props, 'id', 0))}",
            width_px=_W_ID, xalign=1.0, monospace=True, expand=False, is_last=False,
        ); self._view.append_column(col_id)

        col_ra = make_text_column(
            "RA (hms)",
            lambda r: "" if r is None else _hms_from_deg(float(getattr(r.props, "ra", 0.0))),
            width_px=_W_RA, xalign=0.0, monospace=True, expand=True, is_last=False,
        ); self._view.append_column(col_ra)

        col_dec = make_text_column(
            "Dec (dms)",
            lambda r: "" if r is None else _dms_from_deg(float(getattr(r.props, "dec", 0.0))),
            width_px=_W_DEC, xalign=0.0, monospace=True, expand=True, is_last=False,
        ); self._view.append_column(col_dec)

        col_mag = make_text_column(
            "Mag",
            lambda r: "" if r is None else (
                "" if (getattr(r.props, "imag", float("nan")) is None
                       or (isinstance(getattr(r.props, "imag", None), float)
                           and math.isnan(float(getattr(r.props, "imag", float('nan'))))))
                else f"{float(getattr(r.props, 'imag', float('nan'))):.3f}"
            ),
            width_px=_W_MAG, xalign=1.0, monospace=True, expand=False, is_last=True,
        ); self._view.append_column(col_mag)

        # Sorters
        def numeric_sorter(prop_name: str) -> Gtk.NumericSorter:
            expr = Gtk.PropertyExpression.new(StarRow, None, prop_name)
            s = Gtk.NumericSorter.new(expr); s.set_sort_order(Gtk.SortType.ASCENDING)
            return s

        def multi(primary: str, secondary: str) -> Gtk.MultiSorter:
            ms = Gtk.MultiSorter.new()
            ms.append(numeric_sorter(primary))
            ms.append(numeric_sorter(secondary))
            return ms

        col_ra.set_sorter(multi("ra",  "dec"))
        col_dec.set_sorter(multi("dec", "ra"))
        col_mag.set_sorter(multi("imag", "id"))
        col_id.set_sorter(multi("id", "ra"))
        self._sort_model.set_sorter(self._view.get_sorter())

        # Scroller
        scroller = Gtk.ScrolledWindow.new()
        scroller.add_css_class("lemon-scroller")
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._view)
        scroller.set_hexpand(True); scroller.set_vexpand(True)
        root.append(scroller)
        self._scroller = scroller

        # Status
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.add_css_class("lemon-statusbar")
        status_bar.set_hexpand(True)
        self._status = Gtk.Label(label="Open a .db to begin.")
        self._status.set_halign(Gtk.Align.START)
        self._status.set_selectable(True)
        self._install_status_context_menu()
        status_bar.append(self._status)
        root.append(status_bar)

        # Data holders
        self._miner = None
        self._db_path: Optional[str] = None

    # -------- UI helpers --------
    def _set_status(self, text: str) -> None:
        self._status.set_label(text)

    def _show_error(self, message: str, title: str = "Error") -> None:
        logger.error("%s", message)
        try:
            n = Gio.Notification.new(title)
            n.set_body(message)
            n.set_priority(Gio.NotificationPriority.HIGH)
            app = self.get_application()
            if app is not None:
                app.send_notification(None, n)
        except Exception:
            pass
        try:
            sys.stderr.write(f"{title}: {message}\n")
        except Exception:
            pass

    # -------- Selection & activate --------
    def _on_selection_changed(self, _model, _pos, _n_items):
        item: StarRow | None = self._sel.get_selected_item()
        if not item:
            self._status.set_label("No star selected.")
            self._cancel_reveal_retries()
            return
        sid = int(getattr(item.props, "id", -1))
        self._selected_star_id = sid
        ra  = float(getattr(item.props, "ra", 0.0))
        dec = float(getattr(item.props, "dec", 0.0))
        imag = getattr(item.props, "imag", float("nan"))
        mag_txt = "" if (imag is None or (isinstance(imag, float) and math.isnan(imag))) else f"{float(imag):.3f}"
        self._status.set_label(f"ID: {sid}  |  RA: {_hms_from_deg(ra)}  |  Dec: {_dms_from_deg(dec)}  |  Mag: {mag_txt}")
        self._queue_reveal_retries()

    def _on_view_activate(self, _view: Gtk.ColumnView, _pos: int):
        item: StarRow | None = self._sel.get_selected_item()
        if item:
            self._open_star_window(item)

    def _open_star_from_current_selection(self) -> bool:
        item: StarRow | None = self._sel.get_selected_item()
        if item:
            self._open_star_window(item)
        return False

    def _open_star_window(self, row: StarRow) -> None:
        # Close previous
        try:
            if self._star_win is not None:
                try:
                    self._star_win.destroy()
                except Exception:
                    pass
                self._star_win = None
        except Exception:
            self._star_win = None

        try:
            # Build payload directly from this row (dict expected by StarDetailsWindow)
            sid = int(getattr(row.props, "id", -1))
            ra = float(getattr(row.props, "ra", 0.0))
            dec = float(getattr(row.props, "dec", 0.0))
            imag = getattr(row.props, "imag", float("nan"))

            # Choose a default filter for this star (the one with the most points)
            default_filt = None
            default_n = -1
            try:
                for pf in (getattr(self._miner, "pfilters", []) or []):
                    try:
                        curve = self._miner.get_light_curve(sid, pf)
                    except Exception:
                        curve = None
                    n = len(curve) if curve else 0
                    if n > default_n:
                        default_n = n
                        # Prefer a human-friendly name; fall back to id
                        default_filt = getattr(pf, "name", None)
                        if default_filt is None:
                            default_filt = getattr(pf, "letter", None)
                        if default_filt is None:
                            default_filt = getattr(pf, "id", None)
            except Exception:
                pass

            data = {
                "id": sid,
                "ra_deg": ra,
                "dec_deg": dec,
                "ra_str": _hms_from_deg(ra),
                "dec_str": _dms_from_deg(dec),
                "mag": None if (imag is None or (isinstance(imag, float) and math.isnan(imag))) else float(imag),
                "filt": default_filt,
                "n_points": (None if default_n < 0 else default_n),
                "field_name": getattr(self._miner, "field_name", "") or (
                    Path(self._db_path).stem if self._db_path else ""),
                "simbad_id": None,
            }

            # Create details window (no .ui), let it query SQLAlchemy itself
            self._star_win = StarDetailsWindow(self, row_as_dict=data, miner=self._miner)
            self._star_win.present()

        except Exception as e:
            self._show_error(f"Could not open star window:\n{e}", title="Star Window Error")

    # -------- File open --------
    def _on_open_clicked(self, _btn: Gtk.Button) -> None:
        self._show_open_dialog()

    def _show_open_dialog(self) -> None:
        dlg = Gtk.FileDialog.new()
        filt = Gtk.FileFilter()
        filt.set_name("LEMON Database (*.db)")
        filt.add_pattern("*.db")
        dlg.set_default_filter(filt)

        def _on_done(_src, res):
            try:
                file = dlg.open_finish(res)
            except GLib.Error:
                return
            if not file:
                return
            path = file.get_path()
            if path:
                self.open_db(path)

        dlg.open(self, None, _on_done)

    # -------- DB ops --------
    def open_db(self, path: str) -> None:
        p = Path(path) if path else None
        try:
            if not p or not p.exists():
                raise FileNotFoundError(f"Path does not exist:\n{path}")
            if not p.is_file():
                raise IsADirectoryError(f"Expected a .db file, got a directory:\n{path}")
            if p.suffix.lower() != ".db":
                raise ValueError(f"Not a LEMON database (*.db):\n{path}")
            self._load_db(str(p))
        except Exception as e:
            self._show_error(f"Failed to open database:\n{p}\n\n{e}")

    def _load_db(self, db_path: str) -> None:
        Miner = _import_miner_class()
        miner = Miner(db_path)
        self._miner = miner
        self._db_path = db_path
        self.set_title(f"yuzu juicer {Path(db_path).name}")
        logger.info("Opened database: %s", db_path)
        self._populate_overview_from_miner(miner)

        # If CLI passed --star ?, ensure autoselect + AUTO-OPEN happens
        self._ensure_autoselect_when_ready()

    # -------- Auto-select / reveal --------
    def _angsep_deg(self, ra1, dec1, ra2, dec2) -> float:
        r1 = math.radians(ra1); d1 = math.radians(dec1)
        r2 = math.radians(ra2); d2 = math.radians(dec2)
        cos_d = (math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
        cos_d = max(-1.0, min(1.0, cos_d))
        return math.degrees(math.acos(cos_d))

    def _find_inner_listview(self) -> Optional[Gtk.ListView]:
        if self._inner_listview:
            return self._inner_listview
        try:
            def walk(w: Gtk.Widget):
                c = w.get_first_child()
                while c:
                    if isinstance(c, Gtk.ListView):
                        return c
                    r = walk(c)
                    if r:
                        return r
                    c = c.get_next_sibling()
                return None
            lv = walk(self._view)
            if isinstance(lv, Gtk.ListView):
                self._inner_listview = lv
                return lv
        except Exception:
            pass
        return None

    def _scroll_to_widget_bounds(self, widget: Gtk.Widget) -> bool:
        try:
            if not self._scroller:
                return False
            rect = widget.compute_bounds(self._scroller)
            if not isinstance(rect, Graphene.Rect):
                return False
            vadj = self._scroller.get_vadjustment()
            if not vadj:
                return False
            page = vadj.get_page_size()
            upper = vadj.get_upper()
            if page <= 0 or upper <= 0:
                return False
            target_y = max(0.0, rect.y - 0.3 * page)
            target_y = min(target_y, max(0.0, upper - page))
            if abs(vadj.get_value() - target_y) > 1.0:
                vadj.set_value(target_y)
            try: self._view.grab_focus()
            except Exception: pass
            return True
        except Exception:
            return False

    def _reveal_current_selection(self) -> bool:
        try:
            pos = None
            try:
                pos = int(self._sel.get_selected())  # type: ignore[attr-defined]
            except Exception:
                pass
            if pos is None or pos < 0:
                it = self._sel.get_selected_item()
                if it is None:
                    return False
                sid = int(getattr(it.props, "id", -1))
                m = self._sort_model.get_n_items()
                for j in range(m):
                    it2 = self._sort_model.get_item(j)
                    try:
                        if int(getattr(it2.props, "id", -2)) == sid:
                            pos = j
                            break
                    except Exception:
                        continue
            if pos is None or pos < 0:
                return False
            self._reveal_sorted_position(pos)
        except Exception:
            pass
        return False

    def _reveal_sorted_position(self, pos: int) -> None:
        """
        Safely reveal (scroll to) a row at `pos` in the *sorted* model.
        Guards against empty models and out-of-range indices to avoid
        gtk_column_view_scroll_to assertions.
        """
        try:
            sort_model = getattr(self, "_sort_model", None)
            view = getattr(self, "_view", None)
            sel = getattr(self, "_sel", None)
            if sort_model is None or view is None or sel is None:
                return

            # Normalize/validate index
            try:
                pos = int(pos)
            except Exception:
                return
            try:
                n_items = int(sort_model.get_n_items())
            except Exception:
                # Fallback if ListModel-like API is missing
                try:
                    n_items = int(view.get_model().get_n_items())  # type: ignore[attr-defined]
                except Exception:
                    n_items = 0
            if n_items <= 0 or pos < 0 or pos >= n_items:
                return

            # Select the row (if we have a selection model)
            try:
                sel.set_selected(pos)  # Gtk.SingleSelection
            except Exception:
                pass

            # Scroll to the row in the ColumnView
            try:
                from gi.repository import Gtk  # type: ignore
                view.scroll_to(pos, None, Gtk.ListScrollFlags.SELECT, None)
            except Exception:
                # Fallback: best-effort focus without scrolling
                pass

            # Try to focus the list so keyboard nav works
            try:
                view.grab_focus()
            except Exception:
                pass

        except Exception:
            # Final guard: never let UI crash here
            return

    def _select_nearest_to(self, ra_deg: float, dec_deg: float) -> None:
        best_i = -1; best_sep = float("inf"); best_id = None
        n = self._rows.get_n_items()
        for i in range(n):
            row = self._rows.get_item(i)
            try:
                r = float(getattr(row.props, "ra", float("nan")))
                d = float(getattr(row.props, "dec", float("nan")))
                if math.isnan(r) or math.isnan(d): continue
                sep = self._angsep_deg(ra_deg, dec_deg, r, d)
                if sep < best_sep:
                    best_sep, best_i, best_id = sep, i, int(getattr(row.props, "id", -1))
            except Exception:
                continue
        if best_i < 0 or best_id is None:
            logger.info("Auto-select: no candidates found.")
            return

        # Map ID to sorted index
        m = self._sort_model.get_n_items()
        target_idx = -1; target_row = None
        for j in range(m):
            it = self._sort_model.get_item(j)
            try:
                if int(getattr(it.props, "id", -2)) == best_id:
                    target_idx, target_row = j, it
                    break
            except Exception:
                continue
        if target_idx < 0 or target_row is None:
            logger.warning("Auto-select: could not map base index to sorted model.")
            return

        # Select
        ok = False
        try:
            ok = bool(self._sel.select_item(target_idx, True))  # type: ignore[attr-defined]
        except Exception:
            pass
        if not ok:
            try:
                self._sel.set_selected(target_idx)
                ok = True
            except Exception:
                ok = False
        if not ok:
            logger.warning("Auto-select: selection call did not apply.")
            return

        self._selected_star_id = best_id
        self._queue_reveal_retries()

        r = float(getattr(target_row.props, "ra", float("nan")))
        d = float(getattr(target_row.props, "dec", float("nan")))
        sep = self._angsep_deg(ra_deg, dec_deg, r, d)
        logger.info("Auto-selected nearest: ID=%s  RA=%s  Dec=%s  (dist=%.2f mas)",
                    int(getattr(target_row.props, "id", -1)),
                    _hms_from_deg(r), _dms_from_deg(d), sep * 3600000.0)

    def _do_autoselect_now(self) -> bool:
        if self._autoselect_done or not self._autoselect_radec:
            return False
        ra0, dec0 = self._autoselect_radec
        logger.info("Auto-select request: RA=%.6f deg  Dec=%.6f deg", ra0, dec0)
        try:
            self._select_nearest_to(float(ra0), float(dec0))
        finally:
            # After selection, auto-open the star details
            GLib.idle_add(self._open_star_from_current_selection)
            self._autoselect_done = True
            self._autoselect_radec = None
        return False

    def _ensure_autoselect_when_ready(self) -> None:
        if self._autoselect_done or not self._autoselect_radec:
            return
        if self._sort_model.get_n_items() > 0:
            GLib.idle_add(self._do_autoselect_now)
            return
        def on_items_changed(model, _pos, _removed, _added):
            if model.get_n_items() > 0 and not self._autoselect_done:
                GLib.idle_add(self._do_autoselect_now)
            try: model.disconnect(handler_id)
            except Exception: pass
        handler_id = self._sort_model.connect("items-changed", on_items_changed)

    # -------- Debounced reselect by remembered ID --------
    def _cancel_reveal_retries(self) -> None:
        if not self._reveal_retry_ids:
            return
        for sid in list(self._reveal_retry_ids):
            _remove_source_if_alive(sid)
        self._reveal_retry_ids.clear()

    def _queue_reveal_retries(self) -> None:
        self._cancel_reveal_retries()
        self._reveal_retry_ids.append(GLib.idle_add(self._reveal_current_selection))
        for delay in (80, 180, 360, 700, 1200):
            self._reveal_retry_ids.append(GLib.timeout_add(delay, self._reveal_current_selection))
        try:
            def _tick_once(_w, _fc):
                self._reveal_current_selection()
                return False
            self._view.add_tick_callback(_tick_once)  # type: ignore[attr-defined]
        except Exception:
            pass

    def _request_reselect(self) -> None:
        if self._reselect_idle_id is not None:
            return
        self._reselect_idle_id = GLib.idle_add(self._do_reselect_once)

    def _do_reselect_once(self) -> bool:
        try:
            self._reselect_by_star_id()
        finally:
            self._reselect_idle_id = None
        return False

    def _reselect_by_star_id(self) -> None:
        sid = self._selected_star_id
        if sid is None:
            return
        if self._reselect_guard_id == sid:
            return
        self._reselect_guard_id = sid
        try:
            cur = self._sel.get_selected_item()
            try:
                if cur is not None and int(getattr(cur.props, "id", -999)) == sid:
                    return
            except Exception:
                pass
            m = self._sort_model.get_n_items()
            for j in range(m):
                it = self._sort_model.get_item(j)
                try:
                    if int(getattr(it.props, "id", -2)) == sid:
                        try:
                            if hasattr(self._sel, "select_item"):
                                self._sel.select_item(j, True)  # type: ignore[attr-defined]
                            else:
                                self._sel.set_selected(j)
                        except Exception:
                            try:
                                self._sel.set_selected(j)
                            except Exception:
                                pass
                        try: self._view.grab_focus()
                        except Exception: pass
                        self._queue_reveal_retries()
                        logger.info("Reselected remembered ID=%s at sorted index %s", sid, j)
                        return
                except Exception:
                    continue
            logger.info("Reselect: remembered ID=%s not found in current view", sid)
        finally:
            GLib.idle_add(self._clear_reselect_guard)

    def _clear_reselect_guard(self) -> bool:
        self._reselect_guard_id = None
        return False

    # -------- Status context menu --------
    def _ensure_lemon_css(self) -> None:
        if getattr(self, "_lemon_css_loaded", False):
            return
        css = b"""
        popover.lemon-status-pop {
            background-color: #2f2f2f;
            color: #e6e6e6;
            border-radius: 10px;
            border: 1px solid #3a3a3a;
        }
        popover.lemon-status-pop * { color: #e6e6e6; }
        popover.lemon-status-pop modelbutton:hover { background-color: #3a3a3a; }
        """
        try:
            provider = Gtk.CssProvider()
            provider.load_from_data(css)
            disp = Gdk.Display.get_default()
            if disp is not None:
                Gtk.StyleContext.add_provider_for_display(
                    disp, provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                )
            self._lemon_css_loaded = True
        except Exception:
            pass

    def _copy_status_to_clipboard(self, *_a) -> None:
        try:
            txt = self._status.get_text()
            disp = Gdk.Display.get_default()
            if disp is not None:
                disp.get_clipboard().set_text(txt)
        except Exception:
            sys.stderr.write("Copy status failed.\n")

    def _select_all_status(self, *_a) -> None:
        try:
            txt = self._status.get_text()
            self._status.select_region(0, len(txt))
        except Exception:
            pass

    def _install_status_context_menu(self) -> None:
        self._ensure_lemon_css()
        act_copy = Gio.SimpleAction.new("status_copy", None)
        act_copy.connect("activate", self._copy_status_to_clipboard)
        self.add_action(act_copy)
        act_sel = Gio.SimpleAction.new("status_select_all", None)
        act_sel.connect("activate", self._select_all_status)
        self.add_action(act_sel)

        menu = Gio.Menu()
        menu.append("Copy", "win.status_copy")
        menu.append("Select All", "win.status_select_all")

        pop = Gtk.PopoverMenu()
        pop.set_menu_model(menu)
        pop.add_css_class("lemon-status-pop")
        pop.set_has_arrow(True)
        self._status_menu = pop

        def _popup_at(x: int, y: int) -> None:
            rect = Gdk.Rectangle(int(x), int(y), 1, 1)
            pop.set_parent(self._status)
            pop.set_pointing_to(rect)
            pop.popup()

        click = Gtk.GestureClick()
        click.set_button(3)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        try: click.set_exclusive(True)
        except Exception: pass

        def on_pressed(gesture, _n_press, x, y):
            try: gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            except Exception: pass
            _popup_at(int(x), int(y))

        click.connect("pressed", on_pressed)
        self._status.add_controller(click)

    # -------- Populate UI --------
    def _populate_overview_from_miner(self, miner) -> None:
        # Reset model safely
        try:
            self._rows.remove_all()
        except Exception:
            from gi.repository import Gio  # type: ignore
            self._rows = Gio.ListStore.new(StarRow)
            self._sort_model.set_model(self._rows)
            self._sel.set_model(self._sort_model)
            self._view.set_model(self._sel)

        # Clear per-row widget cache (used for reveal/scroll helpers)
        try:
            self._row_widgets.clear()
        except Exception:
            self._row_widgets = {}

        n_added = 0
        star_ids = list(getattr(miner, "star_ids", []) or [])

        for star_id in star_ids:
            try:
                s = miner.get_star(star_id)

                # Extract ra/dec/imag from various shapes
                ra = dec = imag = None

                if isinstance(s, (tuple, list)):
                    # Common miner shapes:
                    # (x, y, ra, dec, ..., imag)  -> ra=s[2], dec=s[3], imag=s[-1]
                    if len(s) >= 5:
                        ra, dec, imag = s[2], s[3], s[-1]
                    # Fallbacks (be tolerant but safe)
                    elif len(s) >= 3:
                        ra, dec = s[0], s[1]
                        imag = s[2]
                    else:
                        raise ValueError(f"Unexpected star tuple length: {len(s)}")

                elif isinstance(s, dict):
                    ra = s.get("ra")
                    dec = s.get("dec")
                    imag = s.get("imag", s.get("mag"))

                else:
                    # Likely an ORM row / object with attributes
                    ra = getattr(s, "ra")
                    dec = getattr(s, "dec")
                    imag = getattr(s, "imag", getattr(s, "mag", None))

                if ra is None or dec is None:
                    raise ValueError("Missing RA/Dec")

                row = StarRow(
                    id=int(star_id),
                    ra=float(ra),
                    dec=float(dec),
                    imag=(float(imag) if imag is not None else float("nan")),
                )
                self._rows.append(row)
                n_added += 1

            except Exception as e:
                try:
                    logger.debug("Skipping star %s: %s", star_id, e)
                except Exception:
                    pass
                continue

        # Status line: field name, counts, filters
        try:
            field = getattr(miner, "field_name", "") or Path(getattr(miner, "path", "")).name
        except Exception:
            field = ""
        try:
            n_filters = len(getattr(miner, "pfilters", []) or [])
        except Exception:
            n_filters = 0

        self._set_status(f"Field: {field} ? Stars shown: {n_added} ? Filters: {n_filters}")

    # -------- Autoselect & auto-open --------
    def _ensure_autoselect_when_ready(self) -> None:
        if self._autoselect_done or not self._autoselect_radec:
            return
        if self._sort_model.get_n_items() > 0:
            GLib.idle_add(self._do_autoselect_now)
            return
        def on_items_changed(model, _pos, _removed, _added):
            if model.get_n_items() > 0 and not self._autoselect_done:
                GLib.idle_add(self._do_autoselect_now)
            try: model.disconnect(handler_id)
            except Exception: pass
        handler_id = self._sort_model.connect("items-changed", on_items_changed)
