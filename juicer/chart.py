# juicer/chart.py ? GTK4 finding chart (dark theme)
from __future__ import annotations

import io
import math
from typing import Iterable, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gtk, Gdk, Gio, GLib, GdkPixbuf  # type: ignore

from conf_manager.conf_manager import cfg

# Try GTK4Agg (interactive). If missing, fall back to Agg + Gtk.Picture
_BACKEND = "gtk4"
try:
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.ticker import MultipleLocator
    from matplotlib.backends.backend_gtk4agg import FigureCanvasGTK4Agg as FigureCanvas
    from matplotlib.backends.backend_gtk4 import NavigationToolbar2GTK4 as NavigationToolbar
except Exception:
    _BACKEND = "png"
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib.figure import Figure
    from matplotlib.ticker import MultipleLocator

# Optional SIMBAD helper
try:
    from .simbad import coordinate_query as _simbad_open  # type: ignore
except Exception:
    _simbad_open = None

# --- Theme colors (match app) ---
_BG = cfg.get("theme", "background", "#2a2f38")   # dark gray background
_FG = cfg.get("theme", "foreground", "#ffffff")   # white foreground

# Ensure DejaVu Sans (has arrows) unless user overrode
matplotlib.rcParams["font.family"] = cfg.get("find_chart", "font_family", "DejaVu Sans")
matplotlib.rcParams["text.color"] = _FG
matplotlib.rcParams["axes.labelcolor"] = _FG
matplotlib.rcParams["xtick.color"] = _FG
matplotlib.rcParams["ytick.color"] = _FG


# ---------- small helpers ----------
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

def _neighbors_from_miner(miner, ra0_deg: float, dec0_deg: float, radius_arcmin: float):
    """Return list of (dx_arcmin, dy_arcmin, imag) within a SQUARE box of ±radius."""
    out: list[tuple[float, float, float]] = []
    cosd = math.cos(math.radians(dec0_deg))
    half_box_deg = radius_arcmin / 60.0
    for i, sid in enumerate(getattr(miner, "star_ids", [])):
        # Hard cap to avoid plotting millions of points accidentally
        if i >= 20000:
            break
        try:
            _x, _y, ra, dec, *_rest, imag = miner.get_star(sid)
            dra_deg = (float(ra) - ra0_deg) * cosd
            ddec_deg = float(dec) - dec0_deg
            # Square window: ensures all quadrants get filled; no radial cut
            if abs(dra_deg) <= half_box_deg and abs(ddec_deg) <= half_box_deg:
                out.append((dra_deg * 60.0, ddec_deg * 60.0, float(imag)))
        except Exception:
            continue
    return out


def _style_axes(ax, title: str, ra_deg: float, dec_deg: float):
    """Apply dark background + white foreground to the axes & figure."""
    # Figure & axes background
    ax.figure.set_facecolor(_BG)
    ax.set_facecolor(_BG)

    if title:
        ax.set_title(title, pad=8.0, color=_FG)

    # Labels ? short, unambiguous text (avoid glyphs that may miss)
    ax.set_xlabel("?RA (arcmin, East +, left)", color=_FG, labelpad=18)
    ax.set_ylabel("?Dec (arcmin, North +, up)", color=_FG)

    # Spines & ticks (white)
    for sp in ax.spines.values():
        sp.set_color(_FG)
    ax.tick_params(colors=_FG)

    # Subtitle (center line) BELOW the X label, using two-line layout:
    # We push X label a bit up via labelpad above; put footer near ~0.04
    ra_txt = _hms_from_deg(ra_deg)
    dec_txt = _dms_from_deg(dec_deg)
    ax.figure.text(0.5, 0.04, f"Center: RA {ra_txt}, Dec {dec_txt}",
                   ha="center", va="bottom", color=_FG)


def _draw_finder(ax, ra_deg: float, dec_deg: float, radius_arcmin: float,
                 neighbors: Optional[Iterable[tuple[float, float, float]]],
                 title: str):
    r = float(radius_arcmin)
    # Square axes, equal scaling, East to the left
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(r, -r)   # reverse X so RA increases to the left (East +)
    ax.set_ylim(-r, r)

    # Grid at 1 arcmin (white, faint)
    ax.xaxis.set_major_locator(MultipleLocator(1.0))
    ax.yaxis.set_major_locator(MultipleLocator(1.0))
    ax.grid(True, linewidth=0.7, alpha=0.18, color=_FG)

    # Neighbors (white points) ? size inversely with magnitude (clamped)
    if neighbors:
        xs, ys, sizes = [], [], []
        for dx, dy, mag in neighbors:
            xs.append(float(dx))
            ys.append(float(dy))
            try:
                m = float(mag)
            except Exception:
                m = 15.0
            m = max(min(m, 18.0), 8.0)
            sizes.append((18.0 - m) ** 1.6 + 6.0)
        ax.scatter(xs, ys, s=sizes, marker="o", alpha=0.9, linewidths=0.0, c=_FG)

    # Target crosshair & bullseye (white)
    ax.axhline(0, linewidth=1.0, color=_FG)
    ax.axvline(0, linewidth=1.0, color=_FG)
    ax.scatter([0], [0], s=90, marker="*", c=_FG)  # target star

    # Cardinal markers (ASCII-safe): place them near frame edges
    ax.text(r * 0.02,  r * 0.96, "N", color=_FG, ha="left", va="top")
    ax.text(-r * 0.98, 0.02,     "E", color=_FG, ha="left", va="bottom")

    _style_axes(ax, title, ra_deg, dec_deg)


def _render_png_bytes(fig: Figure) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=140)
    return buf.getvalue()


def _png_to_texture(png: bytes) -> Gdk.Texture:
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(png)
    loader.close()
    pb = loader.get_pixbuf()
    return Gdk.Texture.new_for_pixbuf(pb)


# ---------- public API ----------
class FindingChartDialog(Gtk.Dialog):
    """Modal dialog showing a finder chart."""

    def __init__(
        self,
        parent: Gtk.Window,
        *,
        ra_deg: float,
        dec_deg: float,
        radius_arcmin: float = 6.0,
        miner=None,
        star_id: Optional[int] = None,
        field_name: str | None = None,
    ):
        title_parts = ["Finding Chart"]
        if field_name:
            title_parts.append(f"? {field_name}")
        if star_id is not None:
            title_parts.append(f"(ID {star_id})")
        super().__init__(title=" ".join(title_parts), transient_for=parent, use_header_bar=True)
        self.set_modal(True)

        # Size from config
        self.set_default_size(
            cfg.getint("find_chart", "width", 780),
            cfg.getint("find_chart", "height", 780),
        )

        # Theme CSS (ensure applied even when opened alone)
        try:
            from conf_manager.conf_manager import cfg as _cfg
            _cfg.install_css_for_display(Gdk.Display.get_default())
        except Exception:
            pass

        # Header actions: Save & SIMBAD
        hb = self.get_header_bar()
        if isinstance(hb, Gtk.HeaderBar):
            save_btn = Gtk.Button.new_from_icon_name("document-save-symbolic")
            save_btn.set_tooltip_text("Save chart as PNG")
            save_btn.add_css_class("lemon-btn")
            save_btn.connect("clicked", self._on_save_clicked)
            hb.pack_start(save_btn)

            if _simbad_open is not None:
                simbad_btn = Gtk.Button.new_with_label("Open in SIMBAD")
                simbad_btn.add_css_class("lemon-btn")
                simbad_btn.connect("clicked", lambda *_: _simbad_open(_hms_from_deg(ra_deg), _dms_from_deg(dec_deg)))
                hb.pack_end(simbad_btn)

            close_btn = Gtk.Button.new_with_label("Close")
            close_btn.add_css_class("lemon-btn")
            close_btn.connect("clicked", lambda *_: self.response(Gtk.ResponseType.CLOSE))
            hb.pack_end(close_btn)

        content = self.get_content_area()
        content.set_margin_top(6)
        content.set_margin_bottom(6)
        content.set_margin_start(6)
        content.set_margin_end(6)
        content.add_css_class("lemon-surface")

        # Neighbors
        neighbors = None
        if miner is not None:
            try:
                neighbors = _neighbors_from_miner(miner, ra_deg, dec_deg, radius_arcmin)
                if cfg.getbool("find_chart", "recenter_one_sided_cloud", True) and neighbors:
                    # If stars cluster on one side, re-center by the median to fill quadrants
                    mx = sorted(d[0] for d in neighbors)
                    my = sorted(d[1] for d in neighbors)
                    if mx and my:
                        medx = mx[len(mx)//2]
                        medy = my[len(my)//2]
                        neighbors = [(dx - medx, dy - medy, m) for (dx, dy, m) in neighbors]
            except Exception:
                neighbors = None

        # Figure (dark facecolor)
        fig = Figure(figsize=(5.6, 5.6), dpi=120, layout="constrained", facecolor=_BG)
        ax = fig.add_subplot(111)
        _draw_finder(ax, ra_deg, dec_deg, radius_arcmin, neighbors, field_name or "")

        if _BACKEND == "gtk4":
            canvas = FigureCanvas(fig)
            toolbar = NavigationToolbar(canvas)

            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            vbox.add_css_class("lemon-surface")
            vbox.append(canvas)
            vbox.append(toolbar)
            vbox.set_hexpand(True)
            vbox.set_vexpand(True)

            content.append(vbox)
            self._canvas = canvas
            self._figure = fig
            self._picture = None
        else:
            png = _render_png_bytes(fig)
            texture = _png_to_texture(png)
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)  # avoid stretch/distortion
            pic.set_hexpand(True)
            pic.set_vexpand(True)
            content.append(pic)
            self._canvas = None
            self._figure = fig
            self._picture = pic

        # Remember size/pos on close
        cfg.attach_window(self, "find_chart")
        self.connect("response", lambda *_: self.destroy())

    # --- actions ---
    def _on_save_clicked(self, _btn: Gtk.Button) -> None:
        dlg = Gtk.FileDialog.new()
        dlg.set_initial_name("finding-chart.png")

        def _finish(_src, res):
            try:
                file = dlg.save_finish(res)
            except GLib.Error:
                return
            if not file:
                return
            path = file.get_path()
            if not path:
                return
            try:
                self._figure.savefig(path, dpi=140)
            except Exception:
                pass

        dlg.save(self, None, _finish)


# Convenience entry points
def show_finding_chart(
    parent: Gtk.Window,
    ra_deg: float,
    dec_deg: float,
    *,
    radius_arcmin: float = 6.0,
    miner=None,
    star_id: Optional[int] = None,
    field_name: Optional[str] = None,
) -> None:
    dlg = FindingChartDialog(
        parent,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        radius_arcmin=radius_arcmin,
        miner=miner,
        star_id=star_id,
        field_name=field_name or "",
    )
    dlg.present()


def show_finding_chart_for_item(
    parent: Gtk.Window,
    item,
    miner=None,
    *,
    radius_arcmin: float = 6.0,
    field_name: Optional[str] = None,
) -> None:
    ra = float(getattr(getattr(item, "props", item), "ra"))
    dec = float(getattr(getattr(item, "props", item), "dec"))
    sid = int(getattr(getattr(item, "props", item), "id"))
    show_finding_chart(parent, ra, dec, radius_arcmin=radius_arcmin, miner=miner, star_id=sid, field_name=field_name)
