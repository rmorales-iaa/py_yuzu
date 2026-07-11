# juicer/light_curve_view.py
from __future__ import annotations
import io
import logging
from typing import Any, List, Optional, Tuple

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gio", "2.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gio, GLib, Gdk  # type: ignore

logger = logging.getLogger(__name__)

# ----- theme & config (optional conf_manager) -----
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

def _cfg_getbool(section: str, key: str, default: bool) -> bool:
    try:
        v = cfg.get(section, key) if cfg else None  # type: ignore[attr-defined]
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        pass
    try:
        return cfg.getboolean(section, key, default)  # type: ignore[attr-defined]
    except Exception:
        return default

# colors from config (with safe fallbacks)
_BG        = _cfg_get("theme", "background", "#0b1220")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_ACC       = _cfg_get("theme", "accent",     "#ffd84d")  # curve line
_OK        = _cfg_get("theme", "ok",         "#3ee07b")  # point fill
_ERR       = _cfg_get("theme", "error",      "#ff5a5a")  # error bars & bad markers

# chart tuning
_INVERT_Y    = _cfg_getbool ("chart", "invert_mag_axis", True)
_SNR_BAD     = _cfg_getfloat("chart", "snr_bad_threshold", 5.0)
_PT_SIZE     = _cfg_getfloat("chart", "point_size", 18.0)
_ERR_LW      = _cfg_getfloat("chart", "error_linewidth", 1.0)
_ERR_CAPSIZE = _cfg_getfloat("chart", "error_capsize", 2.0)
_BAD_X_SIZE  = _cfg_getfloat("chart", "bad_marker_size", 36.0)

def _install_theme_css() -> None:
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(f"""
            .lemon-surface {{ background: {_BG}; }}
            .lemon-surface * {{ color: {_FG}; }}
            .lemon-button {{
                background: {_ACC};
                color: #000;
                border-radius: 10px;
                padding: 6px 10px;
            }}
            .toolbar-surface * {{ color: {_FG}; }}
        """.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

# ----- normalize incoming curve -----
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

class LightCurveView(Gtk.Box):
    """
    Matplotlib (GTK4Agg) viewer:
      • curve line = accent
      • point centers = green
      • error bars = red (σ ≈ 1.0857 / SNR)
      • low/missing SNR marked with red ×
      • y-axis orientation forced by explicit limits order
    """
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        _install_theme_css()
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
                self._toolbar.set_hexpand(True)
                self._toolbar.add_css_class("lemon-surface")
                self._toolbar.add_css_class("toolbar-surface")
                # theme toolbar buttons
                try:
                    child = self._toolbar.get_first_child()
                    while child is not None:
                        try:
                            child.add_css_class("lemon-surface")
                            if isinstance(child, Gtk.Button):
                                child.add_css_class("lemon-button")
                                inner = child.get_child()
                                if isinstance(inner, Gtk.Widget):
                                    inner.add_css_class("lemon-surface")
                        except Exception:
                            pass
                        child = child.get_next_sibling()
                except Exception:
                    pass
                self.append(self._toolbar); self.append(self._canvas); return
            except Exception:
                self._canvas = None; self._toolbar = None

        # Fallback: render to PNG and show in Gtk.Picture
        self._picture = Gtk.Picture.new()
        self._picture.set_hexpand(True); self._picture.set_vexpand(True)
        self.append(self._picture)

    def _style_axes(self, ax, *, title: str, xlab: str, ylab: str) -> None:
        try:
            ax.set_facecolor(_BG)
            self._fig.patch.set_facecolor(_BG)  # type: ignore[union-attr]
        except Exception:
            pass
        ax.set_title(title, color=_FG)
        ax.set_xlabel(xlab, color=_FG); ax.set_ylabel(ylab, color=_FG)
        for s in ("top", "right", "left", "bottom"):
            ax.spines[s].set_color(_FG)
        ax.tick_params(axis="x", colors=_FG)
        ax.tick_params(axis="y", colors=_FG)
        ax.grid(True, color=_FG, alpha=0.15)

    def _apply_y_limits(self, ax, ys: List[float], *, invert: bool) -> None:
        if not ys:
            return
        ymin = min(ys); ymax = max(ys)
        if ymin == ymax:
            pad = 0.5 if ymin == 0 else abs(ymin) * 0.05
            ymin, ymax = ymin - pad, ymax + pad
        span = max(1e-9, ymax - ymin)
        lo, hi = ymin - 0.05 * span, ymax + 0.05 * span
        if invert:
            ax.set_ylim(hi, lo)  # lower numeric values appear higher
        else:
            ax.set_ylim(lo, hi)
        try:
            ax.set_autoscale_on(False)
        except Exception:
            pass

    def _finalize_static_picture(self) -> None:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        buf = io.BytesIO()
        FigureCanvasAgg(self._fig).print_png(buf)  # type: ignore[arg-type]
        data = buf.getvalue()
        texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(data))  # type: ignore
        self._picture.set_paintable(texture)

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
                # X as dates if possible
                try:
                    import matplotlib.dates as mdates
                    xs = [mdates.epoch2num(float(t)) for t, _, _ in trip]
                    is_date = True
                except Exception:
                    xs = [float(t) for t, _, _ in trip]
                    is_date = False
                ys   = [float(m) for _, m, _ in trip]
                snrs = [s for *_, s in trip]

                # Curve line
                if is_date:
                    ax.plot_date(xs, ys, linestyle="-", marker=None, linewidth=1.0, color=_ACC, zorder=2)
                else:
                    ax.plot(xs, ys, linestyle="-", marker=None, linewidth=1.0, color=_ACC, zorder=2)

                # Error bars (σ ≈ 1.0857 / SNR) in red
                x_good: List[float] = []
                y_good: List[float] = []
                e_good: List[float] = []
                missing_or_bad_idx: List[int] = []
                for i, (x, y, snr) in enumerate(zip(xs, ys, snrs)):
                    if snr is None or snr <= 0:
                        missing_or_bad_idx.append(i)
                    else:
                        err = 1.0857362047581296 / float(snr)
                        if snr < _SNR_BAD:
                            # still draw errorbar; also mark as 'bad'
                            missing_or_bad_idx.append(i)
                        x_good.append(x); y_good.append(y); e_good.append(err)

                if x_good:
                    eb = ax.errorbar(
                        x_good, y_good, yerr=e_good,
                        fmt="none", ecolor=_ERR, elinewidth=_ERR_LW,
                        capsize=_ERR_CAPSIZE, zorder=4
                    )

                # Point centers in green (all samples)
                ax.scatter(xs, ys, s=_PT_SIZE, marker="o",
                           edgecolor="none", facecolor=_OK, alpha=0.95, zorder=5)

                # Bad/missing SNR marks — small red × on top
                if missing_or_bad_idx:
                    ax.scatter([xs[i] for i in missing_or_bad_idx],
                               [ys[i] for i in missing_or_bad_idx],
                               s=_BAD_X_SIZE, marker="x",
                               color=_ERR, linewidths=1.2, zorder=6)

                # Nice date formatter
                if is_date:
                    import matplotlib.dates as mdates
                    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d\n%H:%M:%S"))
                    self._fig.autofmt_xdate()

                # Y limits + forced orientation
                invert = _cfg_getbool("chart", "invert_mag_axis", _INVERT_Y)
                self._apply_y_limits(ax, ys, invert=invert)
            else:
                ax.text(0.5, 0.5, "No curve points", ha="center", va="center",
                        transform=ax.transAxes, color=_FG)

            if self._canvas is not None:
                self._canvas.queue_draw()
            else:
                self._finalize_static_picture()
        except Exception as e:
            logger.warning("Plot error: %s", e)
