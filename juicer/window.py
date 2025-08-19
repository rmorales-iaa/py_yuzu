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
from gi.repository import Gtk, Gio, GLib, Gdk, GObject, Pango  # type: ignore

logger = logging.getLogger(__name__)
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent
for p in (str(PKG_DIR), str(ROOT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Use your models + star details window
from .models import StarRow  # GObject with props: id (int), ra (float), dec (float), imag (float|None)
from .star_window import StarDetailsWindow


# ---------- Helpers ----------
def _import_miner_class() -> Type:
    """Best-effort import of LEMONdBMiner (package/absolute/by-path)."""
    last_exc: Exception | None = None
    try:
        from .mining import LEMONdBMiner  # type: ignore
        return LEMONdBMiner
    except Exception as e1:
        last_exc = e1
    try:
        from mining import LEMONdBMiner  # type: ignore
        return LEMONdBMiner
    except Exception as e2:
        last_exc = e2
    candidate = PKG_DIR / "mining.py"
    if candidate.exists():
        spec = importlib.util.spec_from_file_location("juicer.mining", candidate)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            sys.modules["juicer.mining"] = mod
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            if hasattr(mod, "LEMONdBMiner"):
                return getattr(mod, "LEMONdBMiner")
            last_exc = RuntimeError("mining.py loaded but LEMONdBMiner not found")
    raise RuntimeError(
        "mining module not available. Ensure juicer/mining.py exists and is importable.\n"
        f"Last import error: {last_exc}"
    )


# Sexagesimal formatters (format in the cell)
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


# ---------- Main Window ----------
class LEMONJuicerWindow(Gtk.ApplicationWindow):
    """GTK4 window with a ColumnView listing stars from the DB."""

    def __init__(self, app: Gtk.Application, title_suffix: str = "",
                 autoselect_radec: Optional[tuple[float, float]] = None):
        super().__init__(application=app, title=f"LEMON Juicer{title_suffix}")
        self.set_default_size(1200, 800)

        # Remember requested RA/Dec (degrees) for one-shot autoselection
        self._autoselect_radec: Optional[tuple[float, float]] = autoselect_radec
        self._autoselect_done: bool = False
        self._autoselect_attempts: int = 0  # small retry loop to wait for sort/model readiness

        self._last_opened_star_id: Optional[int] = None
        self._star_win: Optional[Gtk.Window] = None

        _DARK_LIGHT_BG = "#2a2f38"
        _HEADER_BG        = "#1f4aa8"
        _HEADER_BG_HOVER  = "#2456bf"
        _HEADER_BG_ACTIVE = "#1a3e8f"
        _SELECT_BG = "#375a7f"
        _SELECT_FG = "#ffd84d"

        # ---------- CSS ----------
        provider = Gtk.CssProvider()

        css = f"""
        /* Keep it minimal to avoid painting over list items */
        columnview.lemon-dark listview {{
          background-color: {_DARK_LIGHT_BG};
        }}
        columnview.lemon-dark row {{
          background-color: {_DARK_LIGHT_BG};
        }}
        /* Text color in all cells */
        columnview.lemon-dark label {{
          color: #e7ebef;
        }}
        /* Visible selection for both ListView node flavors */
        columnview.lemon-dark row:selected,
        columnview.lemon-dark listitem:selected {{
          background-color: {_SELECT_BG};
        }}
        columnview.lemon-dark row:selected label,
        columnview.lemon-dark listitem:selected label {{
          color: {_SELECT_FG};
        }}

        /* Status bar (unchanged) */
        .lemon-statusbar {{
          background-color: {_DARK_LIGHT_BG};
          padding: 6px 10px;
        }}
        .lemon-statusbar * {{
          color: {_SELECT_FG};
        }}

        /* Column headers (blue) */
        columnview.lemon-dark > header,
        columnview.lemon-dark > header > * {{
          background-color: {_HEADER_BG};
          color: #ffffff;
        }}
        columnview.lemon-dark > header button {{
          background-color: {_HEADER_BG};
          color: #ffffff;
          font-weight: 600;
          padding: 6px 8px;
          border: 0;
        }}
        columnview.lemon-dark > header button:hover {{ background-color: {_HEADER_BG_HOVER}; }}
        columnview.lemon-dark > header button:active,
        columnview.lemon-dark > header button:checked {{ background-color: {_HEADER_BG_ACTIVE}; }}
        columnview.lemon-dark > header button image {{ color: #ffffff; }}
        """


        provider.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # ---------- HeaderBar ----------
        hb = Gtk.HeaderBar.new()
        self.set_titlebar(hb)
        open_btn = Gtk.Button.new_from_icon_name("document-open-symbolic")
        open_btn.set_tooltip_text("Open LEMON database…")
        open_btn.connect("clicked", self._on_open_clicked)
        hb.pack_start(open_btn)

        # ---------- Root layout ----------
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.set_hexpand(True)
        root.set_vexpand(True)
        self.set_child(root)

        # ---------- Model pipeline: rows -> sort -> selection ----------
        self._rows = Gio.ListStore.new(StarRow)
        self._sort_model = Gtk.SortListModel.new(model=self._rows, sorter=None)
        self._sel = Gtk.SingleSelection.new(self._sort_model)
        self._sel.set_autoselect(False)           # prevent first-row autoselection
        self._sel.connect("selection-changed", self._on_selection_changed)

        # ---------- Column factory ----------
        def make_text_column(title: str, get_text,
                             *, width_chars: int | None,
                             xalign: float, monospace: bool = True,
                             expand: bool = False,
                             label_width_chars: int | None = None) -> Gtk.ColumnViewColumn:
            factory = Gtk.SignalListItemFactory()

            def setup(_f, li: Gtk.ListItem):
                lbl = Gtk.Label(xalign=xalign)
                lbl.set_single_line_mode(True)
                lbl.set_ellipsize(Pango.EllipsizeMode.END)
                if monospace:
                    lbl.add_css_class("monospace")
                if label_width_chars is not None:
                    lbl.set_width_chars(label_width_chars)
                li.set_child(lbl)

            def bind(_f, li: Gtk.ListItem):
                row: StarRow | None = li.get_item()
                txt = get_text(row) if row is not None else ""
                li.get_child().set_text(txt)  # type: ignore[attr-defined]

            factory.connect("setup", setup)
            factory.connect("bind", bind)

            col = Gtk.ColumnViewColumn(title=title, factory=factory)
            col.set_resizable(True)
            col.set_expand(expand)
            if width_chars is not None and not expand:
                col.set_fixed_width(int(width_chars * 9 + 24))
            return col

        # ---------- ColumnView ----------
        self._view = Gtk.ColumnView.new(self._sel)
        self._view.add_css_class("lemon-dark")
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)

        try:
            self._view.connect("activate", self._on_view_activate)
        except TypeError:
            pass

        click = Gtk.GestureClick.new()
        click.set_button(0)
        click.connect("released", self._on_row_click_released)
        self._view.add_controller(click)

        # --- Define columns ---
        col_id = make_text_column(
            "ID",
            lambda r: "" if r is None else f"{int(getattr(r.props, 'id', 0))}",
            width_chars=9, xalign=1.0, monospace=True, expand=False, label_width_chars=9,
        ); self._view.append_column(col_id)

        col_ra = make_text_column(
            "RA (hms)",
            lambda r: "" if r is None else _hms_from_deg(float(getattr(r.props, "ra", 0.0))),
            width_chars=None, xalign=0.0, monospace=True, expand=True, label_width_chars=20,
        ); self._view.append_column(col_ra)

        col_dec = make_text_column(
            "Dec (dms)",
            lambda r: "" if r is None else _dms_from_deg(float(getattr(r.props, "dec", 0.0))),
            width_chars=None, xalign=0.0, monospace=True, expand=True, label_width_chars=20,
        ); self._view.append_column(col_dec)

        col_mag = make_text_column(
            "Mag",
            lambda r: "" if r is None else (
                "" if (getattr(r.props, "imag", float("nan")) is None or math.isnan(float(getattr(r.props, "imag", float("nan")))))
                else f"{float(getattr(r.props, 'imag', float('nan'))):.3f}"
            ),
            width_chars=8, xalign=1.0, monospace=True, expand=False, label_width_chars=8,
        ); self._view.append_column(col_mag)

        # ---------- Sorters ----------
        def _ord(a: float, b: float) -> Gtk.Ordering:
            if a < b: return Gtk.Ordering.SMALLER
            if a > b: return Gtk.Ordering.LARGER
            return Gtk.Ordering.EQUAL

        def _num(v) -> float:
            try:
                if v is None:
                    return float('nan')
                return float(v)
            except Exception:
                return float('nan')

        def _pair_numeric_sorter(primary: str, secondary: str) -> Gtk.Sorter:
            def cmp(a, b, _data):
                pa = _num(a.get_property(primary)); pb = _num(b.get_property(primary))
                na = math.isnan(pa); nb = math.isnan(pb)
                if na and nb:
                    sa = _num(a.get_property(secondary)); sb = _num(b.get_property(secondary))
                    sna = math.isnan(sa); snb = math.isnan(sb)
                    if sna and snb: return Gtk.Ordering.EQUAL
                    if sna:         return Gtk.Ordering.LARGER
                    if snb:         return Gtk.Ordering.SMALLER
                    return _ord(sa, sb)
                if na: return Gtk.Ordering.LARGER
                if nb: return Gtk.Ordering.SMALLER
                o = _ord(pa, pb)
                if o != Gtk.Ordering.EQUAL:
                    return o
                sa = _num(a.get_property(secondary)); sb = _num(b.get_property(secondary))
                sna = math.isnan(sa); snb = math.isnan(sb)
                if sna and snb: return Gtk.Ordering.EQUAL
                if sna:         return Gtk.Ordering.LARGER
                if snb:         return Gtk.Ordering.SMALLER
                return _ord(sa, sb)
            return Gtk.CustomSorter.new(cmp)

        col_ra.set_sorter(_pair_numeric_sorter("ra",  "dec"))
        col_dec.set_sorter(_pair_numeric_sorter("dec", "ra"))
        col_mag.set_sorter(_pair_numeric_sorter("imag", "id"))
        col_id.set_sorter(_pair_numeric_sorter("id", "ra"))
        self._sort_model.set_sorter(self._view.get_sorter())
        self._view.sort_by_column(col_ra, Gtk.SortType.DESCENDING)

        # ---------- Scroller ----------
        scroller = Gtk.ScrolledWindow.new()
        scroller.add_css_class("lemon-scroller")
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self._view)
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        root.append(scroller)

        # ---------- Status bar ----------
        status_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        status_bar.add_css_class("lemon-statusbar")
        status_bar.set_hexpand(True)
        self._status = Gtk.Label(label="Open a .LEMONdB to begin.")
        self._status.set_halign(Gtk.Align.START)
        self._status.set_selectable(True)
        self._install_status_context_menu()
        status_bar.append(self._status)
        root.append(status_bar)

        # ---------- Data holders ----------
        self._miner = None
        self._db_path: Optional[str] = None

    # ---- UI helpers ----
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

    # ---- Selection & click/activate handlers ----
    def _on_selection_changed(self, _model, _pos, _n_items):
        item: StarRow | None = self._sel.get_selected_item()
        if not item:
            self._status.set_label("No star selected.")
            return
        sid = int(getattr(item.props, "id", -1))
        ra  = float(getattr(item.props, "ra", 0.0))
        dec = float(getattr(item.props, "dec", 0.0))
        imag = getattr(item.props, "imag", float("nan"))
        mag_txt = "" if (imag is None or (isinstance(imag, float) and math.isnan(imag))) else f"{float(imag):.3f}"
        self._status.set_label(f"ID: {sid}  |  RA: {_hms_from_deg(ra)}  |  Dec: {_dms_from_deg(dec)}  |  Mag: {mag_txt}")

    def _on_view_activate(self, _view: Gtk.ColumnView, _pos: int):
        GLib.idle_add(self._open_selected_star_once)

    def _on_row_click_released(self, _gesture, _n_press, _x, _y):
        GLib.idle_add(self._open_selected_star_once)

    def _open_selected_star_once(self):
        item: StarRow | None = self._sel.get_selected_item()
        if not item:
            return False
        sid = int(getattr(item.props, "id", -1))
        if self._last_opened_star_id == sid:
            return False
        self._last_opened_star_id = sid
        self._open_star_window(item)
        return False

    def _open_star_window(self, row: StarRow) -> None:
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
            self._star_win = StarDetailsWindow(self, row, self._miner)
            self._star_win.present()
        except Exception as e:
            self._show_error(f"Could not open star window:\n{e}", title="Star Window Error")

    # ---- Open handlers ----
    def _on_open_clicked(self, _btn: Gtk.Button) -> None:
        self._show_open_dialog()

    def _show_open_dialog(self) -> None:
        dlg = Gtk.FileDialog.new()
        pattern = "*.LEMONdB"
        filt = Gtk.FileFilter()
        filt.set_name(f"LEMON Database ({pattern})")
        filt.add_pattern(pattern)
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

    # ---- DB ops ----
    def open_db(self, path: str) -> None:
        p = Path(path) if path else None
        try:
            if not p or not p.exists():
                raise FileNotFoundError(f"Path does not exist:\n{path}")
            if not p.is_file():
                raise IsADirectoryError(f"Expected a .LEMONdB file, got a directory:\n{path}")
            if p.suffix.lower() != ".lemondb":
                raise ValueError(f"Not a LEMON database (*.LEMONdB):\n{path}")
            self._load_db(str(p))
        except Exception as e:
            self._show_error(f"Failed to open database:\n{p}\n\n{e}")

    def _load_db(self, db_path: str) -> None:
        Miner = _import_miner_class()
        miner = Miner(db_path)

        self._miner = miner
        self._db_path = db_path
        self.set_title(f"yuzu juicer — {Path(db_path).name}")
        logger.info("Opened database: %s", db_path)

        self._populate_overview_from_miner(miner)

    # ---- Auto-select helpers ----
    def _angsep_deg(self, ra1, dec1, ra2, dec2) -> float:
        r1 = math.radians(ra1); d1 = math.radians(dec1)
        r2 = math.radians(ra2); d2 = math.radians(dec2)
        cos_d = (math.sin(d1) * math.sin(d2)
                 + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
        cos_d = max(-1.0, min(1.0, cos_d))
        return math.degrees(math.acos(cos_d))

    def _select_nearest_to(self, ra_deg: float, dec_deg: float) -> None:
        # Find closest in the BASE store
        best_i = -1
        best_sep = float("inf")
        best_id = None
        n = self._rows.get_n_items()
        for i in range(n):
            row = self._rows.get_item(i)
            try:
                r = float(getattr(row.props, "ra", float("nan")))
                d = float(getattr(row.props, "dec", float("nan")))
                if math.isnan(r) or math.isnan(d):
                    continue
                sep = self._angsep_deg(ra_deg, dec_deg, r, d)
                if sep < best_sep:
                    best_sep, best_i = sep, i
                    best_id = int(getattr(row.props, "id", -1))
            except Exception:
                continue

        if best_i < 0 or best_id is None:
            logger.info("Auto-select: no candidates found.")
            return

        # Map by STAR ID into the SORTED model
        m = self._sort_model.get_n_items()
        target_sorted_index = -1
        target_row = None
        for j in range(m):
            it = self._sort_model.get_item(j)
            try:
                if int(getattr(it.props, "id", -2)) == best_id:
                    target_sorted_index = j
                    target_row = it
                    break
            except Exception:
                continue

        if target_sorted_index < 0 or target_row is None:
            logger.warning("Auto-select: could not map base index to sorted model.")
            return

        # Apply selection (robustly)
        applied = False
        try:
            # SelectionModel API (works across GTK4)
            applied = self._sel.select_item(target_sorted_index, True)  # type: ignore[attr-defined]
        except Exception:
            pass
        if not applied:
            try:
                self._sel.set_selected(target_sorted_index)
                applied = True
            except Exception:
                applied = False

        if not applied:
            logger.warning("Auto-select: selection call did not apply.")
            return

        # Log the pick & ensure it’s visible
        r = float(getattr(target_row.props, "ra", float("nan")))
        d = float(getattr(target_row.props, "dec", float("nan")))
        sep = self._angsep_deg(ra_deg, dec_deg, r, d)
        logger.info("Auto-selected nearest: ID=%s  RA=%s  Dec=%s  (sep=%.2f arcmin)",
                    int(getattr(target_row.props, "id", -1)),
                    _hms_from_deg(r), _dms_from_deg(d), sep * 60.0)

        try:
            # ColumnView scroll_to signature varies; try generous variants
            try:
                self._view.scroll_to(target_sorted_index, None, Gtk.ListScrollFlags.SELECT, 0.5)  # type: ignore[attr-defined]
            except Exception:
                self._view.scroll_to(target_sorted_index, None)  # type: ignore[arg-type]
        except Exception:
            pass

        # Open details after selection is set
        GLib.idle_add(self._open_selected_star_once)

    def _do_autoselect_now(self) -> bool:
        if self._autoselect_done or not self._autoselect_radec:
            return False
        ra0, dec0 = self._autoselect_radec
        try:
            self._select_nearest_to(float(ra0), float(dec0))
            self._autoselect_done = True
            self._autoselect_radec = None  # one-shot
        except Exception as e:
            logger.warning("Auto-select failed: %s", e)
        return False

    def _tick_autoselect(self) -> bool:
        """Small retry loop (every 120ms) until items exist and sorter has applied."""
        if self._autoselect_done or not self._autoselect_radec:
            return False
        if self._sort_model.get_n_items() == 0:
            self._autoselect_attempts += 1
            return self._autoselect_attempts < 25  # ~3s max
        # model has items → do selection on idle, then stop retrying
        GLib.idle_add(self._do_autoselect_now)
        return False

    def _ensure_autoselect_when_ready(self) -> None:
        """Run autoselect once the SortListModel has items; retries briefly."""
        if self._autoselect_done or not self._autoselect_radec:
            return
        if self._sort_model.get_n_items() > 0:
            GLib.idle_add(self._do_autoselect_now)
            return
        # periodic poll to wait for the first batch (avoids picking too early)
        GLib.timeout_add(120, self._tick_autoselect)

    # ---- Status context menu (dark, copy-only) ----
    def _ensure_lemon_css(self) -> None:
        """Install CSS to darken the custom popover (once per display)."""
        if getattr(self, "_lemon_css_loaded", False):
            return
        css = b"""
        popover.lemon-status-pop {
            background-color: #2f2f2f;
            color: #e6e6e6;
            border-radius: 10px;
            border: 1px solid #3a3a3a;
        }
        popover.lemon-status-pop contents,
        popover.lemon-status-pop stack,
        popover.lemon-status-pop box,
        popover.lemon-status-pop viewport,
        popover.lemon-status-pop scrolledwindow,
        popover.lemon-status-pop listview {
            background-color: #2f2f2f;
        }
        popover.lemon-status-pop modelbutton,
        popover.lemon-status-pop modelbutton:focus,
        popover.lemon-status-pop modelbutton:checked,
        popover.lemon-status-pop modelbutton:active {
            background-color: #2f2f2f;
            color: #e6e6e6;
        }
        popover.lemon-status-pop modelbutton:hover { background-color: #3a3a3a; }
        popover.lemon-status-pop label { color: #e6e6e6; }
        popover.lemon-status-pop separator { background-color: #3a3a3a; }
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
        """Replace the default label context menu with a dark, copy-only one."""
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
        try:
            click.set_exclusive(True)
        except Exception:
            pass

        def on_pressed(gesture, n_press, x, y):
            try:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            except Exception:
                pass
            _popup_at(x, y)

        click.connect("pressed", on_pressed)
        self._status.add_controller(click)

        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

        def on_key(_ctrl, keyval, _keycode, state):
            if keyval == Gdk.KEY_Menu or (keyval == Gdk.KEY_F10 and (state & Gdk.ModifierType.SHIFT_MASK)):
                _popup_at(self._status.get_allocated_width() - 10, self._status.get_allocated_height() // 2)
                return True
            return False

        key.connect("key-pressed", on_key)
        self._status.add_controller(key)

    # ---- Populate UI from miner ----
    def _populate_overview_from_miner(self, miner) -> None:
        """Fill the ColumnView with star rows + update status line."""
        try:
            self._rows.remove_all()
        except Exception:
            self._rows = Gio.ListStore.new(StarRow)
            self._sort_model.set_model(self._rows)
            self._sel.set_model(self._sort_model)
            self._view.set_model(self._sel)

        n_added = 0
        for star_id in getattr(miner, "star_ids", []):
            try:
                x, y, ra, dec, *_rest, imag = miner.get_star(star_id)
                try:
                    row = StarRow(id=int(star_id), ra=float(ra), dec=float(dec), imag=float(imag))
                except Exception:
                    row = StarRow()
                    try:
                        row.set_property("id", int(star_id))
                        row.set_property("ra", float(ra))
                        row.set_property("dec", float(dec))
                        row.set_property("imag", float(imag))
                    except Exception:
                        try:
                            row.props.id = int(star_id)
                            row.props.ra = float(ra)
                            row.props.dec = float(dec)
                            row.props.imag = float(imag)
                        except Exception:
                            continue
                self._rows.append(row)
                n_added += 1
            except Exception:
                continue

        n_filters = len(getattr(miner, "pfilters", []))
        field = getattr(miner, "field_name", "") or Path(getattr(miner, "path", "")).name
        self._set_status(f"Field: {field} — Stars shown: {n_added} — Filters: {n_filters}")

        # Defer the nearest-star selection until the model is ready
        self._ensure_autoselect_when_ready()
