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
from .models import StarRow  # relies on your GObject model with props: id, ra, dec, imag
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

# Sexagesimal formatters (format in the cell; do not require ra_str/dec_str on the model)
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

    def __init__(self, app: Gtk.Application, title_suffix: str = ""):
        super().__init__(application=app, title=f"LEMON Juicer{title_suffix}")
        self.set_default_size(1200, 800)

        _DARK_LIGHT_BG = "#2a2f38"
        # Header in blue (as requested)
        _HEADER_BG        = "#1f4aa8"
        _HEADER_BG_HOVER  = "#2456bf"
        _HEADER_BG_ACTIVE = "#1a3e8f"
        _SELECT_BG = "#375a7f"   # selected row background
        _SELECT_FG = "#ffd84d"   # selected row label (also status text)

        # ---------- CSS ----------
        provider = Gtk.CssProvider()
        css = f"""
        /* Dark background for scroller, viewport, rows, and descendants */
        scrolledwindow.lemon-scroller,
        scrolledwindow.lemon-scroller viewport,
        scrolledwindow.lemon-scroller viewport > *,
        columnview.lemon-dark,
        columnview.lemon-dark > *,
        columnview.lemon-dark listview,
        columnview.lemon-dark listview > *,
        columnview.lemon-dark row,
        columnview.lemon-dark row > * {{
          background-color: {_DARK_LIGHT_BG};
        }}

        /* Default cell text */
        columnview.lemon-dark label {{
          color: #e7ebef;
        }}

        /* Selected row */
        columnview.lemon-dark row:selected {{
          background-color: {_SELECT_BG};
        }}
        columnview.lemon-dark row:selected label {{
          color: {_SELECT_FG};
        }}

        /* Status bar */
        .lemon-statusbar {{
          background-color: {_DARK_LIGHT_BG};
          padding: 6px 10px;
        }}
        .lemon-statusbar * {{
          color: {_SELECT_FG}; /* dark yellow requested earlier */
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
        columnview.lemon-dark > header button:hover {{
          background-color: {_HEADER_BG_HOVER};
        }}
        columnview.lemon-dark > header button:active,
        columnview.lemon-dark > header button:checked {{
          background-color: {_HEADER_BG_ACTIVE};
        }}
        columnview.lemon-dark > header button image {{
          color: #ffffff;
        }}
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
        self._sel.connect("selection-changed", self._on_selection_changed)

        # Track last opened to avoid reopening on the same click
        self._last_opened_star_id: Optional[int] = None
        self._star_win: Optional[Gtk.Window] = None

        # ---------- Column factory ----------
        def make_text_column(title: str, get_text,
                             *, width_chars: int | None,
                             xalign: float, monospace: bool = True,
                             expand: bool = False,
                             label_width_chars: int | None = None) -> Gtk.ColumnViewColumn:
            """get_text(row: StarRow) -> str"""
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
                col.set_fixed_width(int(width_chars * 9 + 24))  # rough monospace char→px
            return col

        # ---------- ColumnView ----------
        self._view = Gtk.ColumnView.new(self._sel)
        self._view.add_css_class("lemon-dark")
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)

        # Activate (double-click/Enter)
        try:
            self._view.connect("activate", self._on_view_activate)
        except TypeError:
            pass

        # Also open on single mouse click (after selection)
        click = Gtk.GestureClick.new()
        click.set_button(0)  # any button
        click.connect("released", self._on_row_click_released)
        self._view.add_controller(click)

        # --- Define columns (ID=9, RA=20, Dec=20, Mag=8; RA/Dec expand) ---
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

        # ---------- Sorters (compound; ascending comparators; NaN/None last) ----------
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
            """Ascending on primary; tie-break on secondary. NaN/None go last."""
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
                # tie-break
                sa = _num(a.get_property(secondary)); sb = _num(b.get_property(secondary))
                sna = math.isnan(sa); snb = math.isnan(sb)
                if sna and snb: return Gtk.Ordering.EQUAL
                if sna:         return Gtk.Ordering.LARGER
                if snb:         return Gtk.Ordering.SMALLER
                return _ord(sa, sb)
            return Gtk.CustomSorter.new(cmp)

        # RA(hms): primary RA, secondary Dec
        col_ra.set_sorter(_pair_numeric_sorter("ra",  "dec"))
        # Dec(dms): primary Dec, secondary RA
        col_dec.set_sorter(_pair_numeric_sorter("dec", "ra"))
        # Mag: primary imag, secondary ID (stable)
        col_mag.set_sorter(_pair_numeric_sorter("imag", "id"))
        # ID: primary id, secondary RA (stable)
        col_id.set_sorter(_pair_numeric_sorter("id", "ra"))

        # Hook ColumnView sorter into the SortListModel (header-driven sorting)
        self._sort_model.set_sorter(self._view.get_sorter())

        # Default sort on startup: RA then Dec, both DESCENDING
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
        GLib.printerr(f"{title}: {message}\n")

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
        # Avoid re-opening the same star repeatedly on the same selection
        if self._last_opened_star_id == sid:
            return False
        self._last_opened_star_id = sid
        self._open_star_window(item)
        return False

    def _open_star_window(self, row: StarRow) -> None:
        """Open your StarDetailsWindow (from juicer/star_window.py)."""
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
        self.set_title(f"LEMON Juicer — {Path(db_path).name}")
        logger.info("Opened database: %s", db_path)

        self._populate_overview_from_miner(miner)

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
                # Construct StarRow robustly (accept kwargs or set properties)
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
                        # Last resort: setattr on props
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
