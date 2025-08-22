# juicer/star_window.py — Star Details window (GTK4, safe builder composition + auto-populate)
# - Tries to auto-fetch current selection data from the parent main window
#   via several common method/attribute names (best-effort, non-fatal)
# - Keeps: GtkListView uses Gtk.SingleSelection(StringList), no builder-signal tricks,
#   programmatic close button, ConfigManager theming, finding-chart dialog

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List, Callable

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
from gi.repository import Gtk, Gio, GLib  # type: ignore

# ---- Optional project helpers (importable either as package or top-level)
try:
    from .conf_manager import cfg  # type: ignore
except Exception:
    try:
        from conf_manager import cfg  # type: ignore
    except Exception:
        cfg = None  # type: ignore

try:
    from . import chart as chart_mod
except Exception:
    chart_mod = None

try:
    from . import simbad as simbad_mod
except Exception:
    simbad_mod = None


# ---------- UI resolution ----------

_DEFAULT_UI_DIR = Path("/mnt/uxmal_groups/common_data/apps/py_yuzu/juicer/gui")
_ENV_UI_DIR = Path(os.environ.get("LEMON_GLADE_DIR", str(_DEFAULT_UI_DIR)))

def _first_existing(*names: str) -> Optional[Path]:
    for nm in names:
        for base in (_ENV_UI_DIR, _DEFAULT_UI_DIR):
            p = base / nm
            if p.is_file():
                return p
    return None


# ---------- Data model ----------

@dataclass
class StarRecord:
    id: str | int | None = None
    ra_deg: float | None = None
    dec_deg: float | None = None
    ra_str: str | None = None
    dec_str: str | None = None
    mag: float | str | None = None
    filt: str | None = None
    n_points: int | None = None
    field_name: str | None = None
    simbad_id: str | None = None

    @classmethod
    def from_mapping(cls, data: Dict[str, Any]) -> "StarRecord":
        def _fnum(v):
            try: return float(v)
            except Exception: return None
        def _fint(v):
            try: return int(v)
            except Exception: return None
        def _fstr(v):
            if v is None: return None
            s = str(v).strip()
            return s or None

        return cls(
            id=data.get("id") or data.get("ID") or data.get("star_id") or data.get("StarID"),
            ra_deg=_fnum(data.get("ra_deg") or data.get("ra") or data.get("RA_deg")),
            dec_deg=_fnum(data.get("dec_deg") or data.get("dec") or data.get("Dec_deg")),
            ra_str=_fstr(data.get("ra_str") or data.get("RA")),
            dec_str=_fstr(data.get("dec_str") or data.get("Dec")),
            mag=data.get("mag") or data.get("MAG") or data.get("m"),
            filt=data.get("filt") or data.get("filter") or data.get("F"),
            n_points=_fint(data.get("n_points") or data.get("N") or data.get("n")),
            field_name=_fstr(data.get("field_name") or data.get("field")),
            simbad_id=_fstr(data.get("simbad_id") or data.get("SIMBAD")),
        )


# ---------- Star Details Window ----------

class StarDetailsWindow(Gtk.Window):
    """
    Flexible constructor to support legacy call sites. Loads star-details.ui with Gtk.Builder,
    then *extracts* its titlebar + child and hosts them in this window. No __class__/__dict__ hacks.

    Auto-populates UI by querying the parent main window for selected star details using
    several common method/attribute names (best-effort).
    """

    _ID_WINDOW = "star_details_window"
    _ID_LIST   = "list_refs"     # GtkListView
    _ID_TREE   = "tree_points"   # GtkTreeView (legacy)

    def __init__(self, *args, **kwargs) -> None:
        # --- Extract parent (first Gtk.Window among args/kwargs), and optional data mapping ---
        parent: Optional[Gtk.Window] = kwargs.pop("parent", None)
        data: Optional[Dict[str, Any]] = kwargs.pop("data", None)

        for obj in args:
            if isinstance(obj, Gtk.Window):
                parent = obj
                break
        if data is None and args:
            maybe = args[-1]
            if isinstance(maybe, dict):
                data = maybe  # best-effort

        super().__init__(transient_for=parent, modal=False)

        # internal state
        self._record: StarRecord = StarRecord()

        self._lbl_id: Optional[Gtk.Label] = None
        self._lbl_ra: Optional[Gtk.Label] = None
        self._lbl_dec: Optional[Gtk.Label] = None
        self._lbl_mag: Optional[Gtk.Label] = None
        self._lbl_filt: Optional[Gtk.Label] = None
        self._lbl_npts: Optional[Gtk.Label] = None

        self._btn_chart: Optional[Gtk.Button] = None
        self._btn_simbad: Optional[Gtk.Button] = None

        # Refs list: store + selection + view (GTK4 requirement)
        self._list_refs: Optional[Gtk.ListView] = None
        self._refs_store: Optional[Gtk.StringList] = None
        self._refs_sel: Optional[Gtk.SingleSelection] = None

        # Points table (legacy GtkTreeView)
        self._tree_points: Optional[Gtk.TreeView] = None
        self._points_store: Optional[Gtk.ListStore] = None

        # Build UI via composition
        ui_path = _first_existing("star-details.ui", "star-details.glade")
        if ui_path:
            builder = Gtk.Builder.new_from_file(str(ui_path))
            built_root = builder.get_object(self._ID_WINDOW)

            if isinstance(built_root, Gtk.Window):
                # titlebar
                try:
                    tb = built_root.get_titlebar()
                except Exception:
                    tb = None
                if tb is not None:
                    built_root.set_titlebar(None)
                    self.set_titlebar(tb)
                # title
                try:
                    title = built_root.get_title()
                    if title:
                        self.set_title(title)
                except Exception:
                    pass
                # child content
                child = built_root.get_child()
                if isinstance(child, Gtk.Widget):
                    built_root.set_child(None)
                    self.set_child(child)
                try:
                    built_root.destroy()
                except Exception:
                    pass

                # Resolve widgets
                self._lbl_id   = builder.get_object("lbl_id")
                self._lbl_ra   = builder.get_object("lbl_ra")
                self._lbl_dec  = builder.get_object("lbl_dec")
                self._lbl_mag  = builder.get_object("lbl_mag")
                self._lbl_filt = builder.get_object("lbl_filt")
                self._lbl_npts = builder.get_object("lbl_npts")

                self._btn_chart  = builder.get_object("btn_chart")
                self._btn_simbad = builder.get_object("btn_simbad")
                btn_close = builder.get_object("btn_close")
                if isinstance(btn_close, Gtk.Button):
                    btn_close.connect("clicked", self._on_close_clicked)

                self._list_refs = builder.get_object(self._ID_LIST)
                self._ensure_refs_view_ready()

                self._tree_points = builder.get_object(self._ID_TREE)
                self._ensure_points_view_ready()

                if isinstance(self._btn_chart, Gtk.Button):
                    self._btn_chart.connect("clicked", self._on_chart_dialog)
                if isinstance(self._btn_simbad, Gtk.Button):
                    self._btn_simbad.connect("clicked", self._on_simbad)

            else:
                # Root is a widget; embed directly
                if isinstance(built_root, Gtk.Widget):
                    self.set_child(built_root)
                self.set_title("Star details")
                self.set_default_size(840, 600)

                self._lbl_id   = builder.get_object("lbl_id")
                self._lbl_ra   = builder.get_object("lbl_ra")
                self._lbl_dec  = builder.get_object("lbl_dec")
                self._lbl_mag  = builder.get_object("lbl_mag")
                self._lbl_filt = builder.get_object("lbl_filt")
                self._lbl_npts = builder.get_object("lbl_npts")

                self._btn_chart  = builder.get_object("btn_chart")
                self._btn_simbad = builder.get_object("btn_simbad")
                btn_close = builder.get_object("btn_close")
                if isinstance(btn_close, Gtk.Button):
                    btn_close.connect("clicked", self._on_close_clicked)

                self._list_refs = builder.get_object(self._ID_LIST)
                self._ensure_refs_view_ready()

                self._tree_points = builder.get_object(self._ID_TREE)
                self._ensure_points_view_ready()

                if isinstance(self._btn_chart, Gtk.Button):
                    self._btn_chart.connect("clicked", self._on_chart_dialog)
                if isinstance(self._btn_simbad, Gtk.Button):
                    self._btn_simbad.connect("clicked", self._on_simbad)

        else:
            # Programmatic fallback (rare)
            self.set_title("Star details")
            self.set_default_size(840, 600)
            grid = Gtk.Grid(column_spacing=12, row_spacing=10,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
            self.set_child(grid)

            def add_row(r: int, title: str) -> Gtk.Label:
                grid.attach(Gtk.Label(label=title, xalign=0), 0, r, 1, 1)
                lbl = Gtk.Label(xalign=0)
                grid.attach(lbl, 1, r, 1, 1)
                return lbl

            r = 0
            self._lbl_id  = add_row(r, "ID");   r += 1
            self._lbl_ra  = add_row(r, "RA");   r += 1
            self._lbl_dec = add_row(r, "Dec");  r += 1
            self._lbl_mag = add_row(r, "Mag");  r += 1
            self._lbl_filt= add_row(r, "Filter"); r += 1
            self._lbl_npts= add_row(r, "# Points"); r += 1

            # Actions
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._btn_chart = Gtk.Button(label="Finding chart")
            self._btn_simbad = Gtk.Button(label="Open in SIMBAD")
            for b in (self._btn_chart, self._btn_simbad):
                box.append(b)
            grid.attach(box, 0, r, 2, 1); r += 1
            self._btn_chart.connect("clicked", self._on_chart_dialog)
            self._btn_simbad.connect("clicked", self._on_simbad)

            # Refs (ListView with selection model)
            self._refs_store = Gtk.StringList.new([])
            self._refs_sel = Gtk.SingleSelection.new(self._refs_store)
            self._list_refs = Gtk.ListView(model=self._refs_sel, factory=self._make_string_factory())
            sc1 = Gtk.ScrolledWindow(min_content_height=120); sc1.set_child(self._list_refs)
            frame_refs = Gtk.Frame(label="Reference stars"); frame_refs.set_child(sc1)
            grid.attach(frame_refs, 0, r, 2, 1); r += 1

            # Points (legacy TreeView)
            self._points_store = Gtk.ListStore.new([str, str])
            self._tree_points = Gtk.TreeView(model=self._points_store)
            for i, title in enumerate(("HJD", "Mag")):
                renderer = Gtk.CellRendererText()
                col = Gtk.TreeViewColumn(title, renderer, text=i)
                self._tree_points.append_column(col)
            sc2 = Gtk.ScrolledWindow(min_content_height=160); sc2.set_child(self._tree_points)
            frame_pts = Gtk.Frame(label="Light-curve points"); frame_pts.set_child(sc2)
            grid.attach(frame_pts, 0, r, 2, 1); r += 1

        # Theme + geometry via ConfigManager (optional)
        try:
            if cfg:
                cfg.install_css_for_display()
                cfg.attach_window(self, "star_window")
        except Exception:
            pass

        # If caller provided initial data, set it now; else best-effort fetch from parent
        if isinstance(data, dict):
            try:
                self.set_star_data(data)
            except Exception:
                pass
        else:
            try:
                # Run after idle so parent has time to update its selection
                GLib.idle_add(lambda: (self._try_autofill_from_parent() or False))
            except Exception:
                pass

    # ---------- Public API ----------

    def set_star_data(
        self,
        data: Dict[str, Any],
        *,
        reference_rows: Optional[List[str]] = None,
        point_rows: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self._record = StarRecord.from_mapping(data or {})

        def set_label(widget: Optional[Gtk.Label], text: str) -> None:
            if isinstance(widget, Gtk.Label):
                widget.set_text(text)

        ra_txt = self._record.ra_str or (
            f"{self._record.ra_deg:.6f} deg" if self._record.ra_deg is not None else "—"
        )
        dec_txt = self._record.dec_str or (
            f"{self._record.dec_deg:.6f} deg" if self._record.dec_deg is not None else "—"
        )

        set_label(self._lbl_id,  str(self._record.id) if self._record.id is not None else "—")
        set_label(self._lbl_ra,  ra_txt)
        set_label(self._lbl_dec, dec_txt)
        set_label(self._lbl_mag, str(self._record.mag) if self._record.mag is not None else "—")
        set_label(self._lbl_filt, self._record.filt or "—")
        set_label(self._lbl_npts, str(self._record.n_points) if self._record.n_points is not None else "—")

        if reference_rows is not None:
            self._populate_refs(reference_rows)

        if point_rows is not None:
            self._populate_points(point_rows)

    # Allow parent code to push data in one call if it knows how
    def set_star_payload(
        self,
        payload: Tuple[Dict[str, Any], Optional[List[str]], Optional[List[Tuple[str, str]]]]
    ) -> None:
        data, refs, pts = payload
        self.set_star_data(data, reference_rows=refs, point_rows=pts)

    # ---------- Autofill from parent ----------

    def _try_autofill_from_parent(self) -> bool:
        parent = self.get_transient_for()
        if parent is None:
            return False

        # 1) Look for a tuple provider: (data, refs, pts)
        tuple_methods = [
            "get_selected_star_payload",
            "star_details_for_selection",
            "fetch_star_details_for_current_selection",
        ]
        for name in tuple_methods:
            fn = getattr(parent, name, None)
            if callable(fn):
                try:
                    payload = fn()
                    if isinstance(payload, tuple) and payload:
                        data = payload[0] if isinstance(payload[0], dict) else None
                        refs = payload[1] if len(payload) > 1 else None
                        pts  = payload[2] if len(payload) > 2 else None
                        if data:
                            self.set_star_data(data, reference_rows=refs, point_rows=pts)
                            return False
                except Exception:
                    pass

        # 2) Single dict provider
        dict_methods = [
            "get_selected_star_data",
            "get_selected_star_dict",
            "get_current_star_record",
            "export_star_details",
        ]
        for name in dict_methods:
            fn = getattr(parent, name, None)
            if callable(fn):
                try:
                    data = fn()
                    if isinstance(data, dict):
                        self.set_star_data(data)
                        # Try optional refs/points if available via separate accessors
                        self._maybe_fill_refs_points(parent)
                        return False
                except Exception:
                    pass

        # 3) Attributes containing dict
        dict_attrs = ["current_star", "selected_star", "selected_row_data", "star_details"]
        for name in dict_attrs:
            val = getattr(parent, name, None)
            if isinstance(val, dict):
                self.set_star_data(val)
                self._maybe_fill_refs_points(parent)
                return False

        # 4) Build from selected row via miner/db if discoverable
        try:
            get_rows = getattr(parent, "get_selected_rows", None)
            miner = getattr(parent, "miner", None)
            if callable(get_rows) and miner:
                rows = get_rows()
                if rows:
                    star_id = None
                    # common helper names
                    for helper in ("get_row_star_id", "row_to_star_id", "get_star_id_from_row"):
                        fn = getattr(parent, helper, None)
                        if callable(fn):
                            try:
                                star_id = fn(rows[0])
                                break
                            except Exception:
                                pass
                    if star_id is None:
                        # As last resort, if parent can provide data for row
                        for helper in ("get_row_data", "get_star_data", "get_row_dict"):
                            fn = getattr(parent, helper, None)
                            if callable(fn):
                                try:
                                    data = fn(rows[0])
                                    if isinstance(data, dict):
                                        self.set_star_data(data)
                                        self._maybe_fill_refs_points(parent)
                                        return False
                                except Exception:
                                    pass
                    # If we have an ID and miner knows how to fetch details
                    if star_id is not None:
                        for helper in ("get_star_by_id", "get_details_by_id", "star_details_by_id"):
                            fn = getattr(miner, helper, None)
                            if callable(fn):
                                try:
                                    data = fn(star_id)
                                    if isinstance(data, dict):
                                        self.set_star_data(data)
                                        self._maybe_fill_refs_points(parent)
                                        return False
                                except Exception:
                                    pass
        except Exception:
            pass

        return False  # do not reschedule

    def _maybe_fill_refs_points(self, parent: Gtk.Window) -> None:
        # Optional refs
        for name in ("get_reference_rows_for_selection", "get_selected_reference_strings", "get_reference_labels"):
            fn = getattr(parent, name, None)
            if callable(fn):
                try:
                    rows = fn()
                    if isinstance(rows, list):
                        # normalize to str
                        self._populate_refs([str(x) for x in rows])
                        break
                except Exception:
                    pass
        # Optional points
        for name in ("get_lightcurve_points_for_selection", "get_selected_points", "get_points_for_star"):
            fn = getattr(parent, name, None)
            if callable(fn):
                try:
                    pts = fn()
                    if isinstance(pts, list):
                        # Normalize to list[tuple[str,str]]
                        norm: List[Tuple[str, str]] = []
                        for row in pts:
                            if isinstance(row, (list, tuple)) and len(row) >= 2:
                                norm.append((str(row[0]), str(row[1])))
                            elif isinstance(row, dict):
                                # common keys
                                hjd = row.get("HJD") or row.get("hjd") or row.get("x") or row.get("t")
                                mag = row.get("Mag") or row.get("mag") or row.get("y")
                                if hjd is not None and mag is not None:
                                    norm.append((str(hjd), str(mag)))
                        if norm:
                            self._populate_points(norm)
                            break
                except Exception:
                    pass

    # ---------- Models & population ----------

    def _ensure_refs_view_ready(self) -> None:
        """Ensure GtkListView uses a Gtk.SelectionModel (Gtk.SingleSelection) over a StringList."""
        if not isinstance(self._list_refs, Gtk.ListView):
            return
        if self._refs_store is None:
            self._refs_store = Gtk.StringList.new([])
        if self._refs_sel is None:
            self._refs_sel = Gtk.SingleSelection.new(self._refs_store)
        current = self._list_refs.get_model()
        if not isinstance(current, Gtk.SelectionModel):
            self._list_refs.set_model(self._refs_sel)
        if self._list_refs.get_factory() is None:
            self._list_refs.set_factory(self._make_string_factory())

    def _make_string_factory(self) -> Gtk.SignalListItemFactory:
        fac = Gtk.SignalListItemFactory()
        def setup(_fac, item: Gtk.ListItem):
            item.set_child(Gtk.Label(xalign=0))
        def bind(_fac, item: Gtk.ListItem):
            lbl: Gtk.Label = item.get_child()  # type: ignore[assignment]
            itm = item.get_item()
            lbl.set_text(str(itm) if itm is not None else "")
        fac.connect("setup", setup)
        fac.connect("bind", bind)
        return fac

    def _ensure_points_view_ready(self) -> None:
        if isinstance(self._tree_points, Gtk.TreeView):
            if self._points_store is None:
                self._points_store = Gtk.ListStore.new([str, str])
                self._tree_points.set_model(self._points_store)
                if not self._tree_points.get_columns():
                    for i, title in enumerate(("HJD", "Mag")):
                        renderer = Gtk.CellRendererText()
                        col = Gtk.TreeViewColumn(title, renderer, text=i)
                        self._tree_points.append_column(col)

    def _populate_refs(self, rows: List[str]) -> None:
        if isinstance(self._refs_store, Gtk.StringList):
            self._refs_store.splice(0, len(self._refs_store), rows)

    def _populate_points(self, rows: List[Tuple[str, str]]) -> None:
        if isinstance(self._points_store, Gtk.ListStore):
            self._points_store.clear()
            for r in rows:
                self._points_store.append([str(r[0]), str(r[1])])

    # ---------- SIMBAD & Finding Chart ----------

    def _on_close_clicked(self, *_args):
        try:
            self.close()
        except Exception:
            pass

    def _on_simbad(self, *_args):
        if simbad_mod is None:
            return
        ra = self._record.ra_deg
        dec = self._record.dec_deg
        if ra is None or dec is None:
            return
        try:
            ra_s = self._record.ra_str or f"{ra:.6f}"
            dec_s = self._record.dec_str or f"{dec:.6f}"
            simbad_mod.coordinate_query(ra_s, dec_s)
        except Exception:
            pass

    def _on_chart_dialog(self, *_args):
        ra = self._record.ra_deg
        dec = self._record.dec_deg
        if ra is None or dec is None:
            return

        if chart_mod and hasattr(chart_mod, "show_finding_chart"):
            try:
                if cfg:
                    try:
                        _ = cfg.getfloat("finding_chart", "radius_arcmin", 6.0)  # type: ignore[attr-defined]
                    except Exception:
                        pass
                chart_mod.show_finding_chart(
                    self.get_transient_for() or self,
                    ra, dec,
                    miner=getattr(self.get_transient_for(), "miner", None),
                    star_id=_safe_int(self._record.id),
                    field_name=self._record.field_name,
                )
                return
            except Exception:
                pass

        self._open_finding_chart_dialog(ra, dec)

    def _open_finding_chart_dialog(self, ra_deg: float, dec_deg: float) -> None:
        ui_path = _first_existing("finding-chart-dialog.ui", "finding-chart-dialog.glade")
        if not ui_path:
            if chart_mod and hasattr(chart_mod, "show_finding_chart"):
                try:
                    chart_mod.show_finding_chart(self.get_transient_for() or self, ra_deg, dec_deg)
                except Exception:
                    pass
            return

        try:
            builder = Gtk.Builder.new_from_file(str(ui_path))
            dialog = builder.get_object("finding_chart_dialog")
            if not isinstance(dialog, Gtk.Dialog):
                return

            chart_area = builder.get_object("chart_area")  # optional DrawingArea
            lbl_status = builder.get_object("lbl_status")
            btn_close = builder.get_object("btn_close")
            if isinstance(btn_close, Gtk.Button):
                btn_close.connect("clicked", self._on_close_clicked)

            try:
                if cfg:
                    cfg.install_css_for_display()
                    cfg.attach_window(dialog, "finding_chart_dialog")
            except Exception:
                pass

            rendered = False
            if chart_mod is not None:
                if not rendered and chart_area is not None and hasattr(chart_mod, "populate_drawing_area"):
                    try:
                        chart_mod.populate_drawing_area(chart_area, ra_deg, dec_deg)  # type: ignore[misc]
                        rendered = True
                    except Exception:
                        pass
                if not rendered and hasattr(chart_mod, "build_widget"):
                    try:
                        w = chart_mod.build_widget(ra_deg, dec_deg)  # type: ignore[misc]
                        if isinstance(chart_area, Gtk.Widget) and isinstance(w, Gtk.Widget):
                            parent = chart_area.get_parent()
                            if isinstance(parent, Gtk.Widget):
                                try:
                                    parent.remove(chart_area)  # type: ignore[attr-defined]
                                except Exception:
                                    pass
                                try:
                                    parent.append(w)  # type: ignore[attr-defined]
                                except Exception:
                                    pass
                                rendered = True
                    except Exception:
                        pass
                if not rendered and hasattr(chart_mod, "show_finding_chart"):
                    try:
                        chart_mod.show_finding_chart(self.get_transient_for() or self, ra_deg, dec_deg)
                        rendered = True
                    except Exception:
                        pass

            if isinstance(lbl_status, Gtk.Label):
                lbl_status.set_text("Ready." if rendered else "Chart rendering not available.")

            dialog.set_transient_for(self.get_transient_for())
            dialog.present()

        except Exception:
            if chart_mod and hasattr(chart_mod, "show_finding_chart"):
                try:
                    chart_mod.show_finding_chart(self.get_transient_for() or self, ra_deg, dec_deg)
                except Exception:
                    pass


# ---------- Convenience factory ----------

def open_star_window(
    parent: Gtk.Window,
    data: Optional[Dict[str, Any]] = None,
    *,
    reference_rows: Optional[List[str]] = None,
    point_rows: Optional[List[Tuple[str, str]]] = None,
) -> StarDetailsWindow:
    """Create, populate and show a StarDetailsWindow."""
    win = StarDetailsWindow(parent, data=data if data else None)
    if data:
        win.set_star_data(data, reference_rows=reference_rows, point_rows=point_rows)
    win.present()
    return win


# ---------- Utils ----------

def _safe_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        return None
