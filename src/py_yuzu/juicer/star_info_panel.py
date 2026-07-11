# juicer/star_info_panel.py
from __future__ import annotations
import html
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gtk, Gdk  # type: ignore

# ----- configuration helpers (conf_manager optional) -----
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

# Base theme
_BG        = _cfg_get("theme", "background", "#2a2f38")
_FG        = _cfg_get("theme", "foreground", "#e7ebef")
_ACC       = _cfg_get("theme", "accent",     "#ffd84d")
_BLUE_HDR  = _cfg_get("theme", "blue",       "#0d6efd")
_DIV       = _cfg_get("theme", "divider",    "#ffd84d")
_MONO_FONT = _cfg_get("theme", "mono_font",
                      "JetBrains Mono, Fira Mono, Menlo, Consolas, monospace")

# Star-info specific
_KEY_ORANGE  = _cfg_get("star_info", "key_color",
                 _cfg_get("theme", "key_color", "#ffa500"))
_SIMBAD_BTN  = _cfg_get("star_info", "simbad_button_variant", "ghost")   # "ghost" | "primary"
_SAVE_BTN    = _cfg_get("star_info", "save_button_variant",   "ghost")   # CHANGED default: "ghost"

def _install_theme_css() -> None:
    try:
        css = f"""
                .lemon-surface {{ background: {_BG}; color: {_FG}; }}
                .lemon-surface * {{ color: {_FG}; }}
                .monospace {{ font-family: {_MONO_FONT}; }}

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
                    padding: 6px;
                }}

                /* Buttons (unified border) */
                .btn {{
                    border-radius: 10px;
                    padding: 6px 12px;
                    border: 1px solid {_DIV};   /* <- same border for both */
                }}
                .btn-primary {{
                    background: {_ACC};
                    color: #000;
                    /* keep the same border color as .btn */
                }}
                .btn-primary:hover {{ filter: brightness(0.95); }}
                .btn-ghost {{
                    background: transparent;
                    color: {_FG};
                }}
                .btn-ghost:hover {{ background: rgba(255,255,255,0.06); }}
                """

        prov = Gtk.CssProvider()
        prov.load_from_data(css.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    except Exception:
        pass

def _style_button(btn: Gtk.Button, variant: str = "primary") -> None:
    for klass in ("btn", "btn-primary", "btn-ghost", "lemon-surface"):
        try: btn.remove_css_class(klass)
        except Exception: pass
    btn.add_css_class("btn"); btn.add_css_class("lemon-surface")
    if (variant or "").lower() == "ghost":
        btn.add_css_class("btn-ghost")
    else:
        btn.add_css_class("btn-primary")
    try:
        child = btn.get_child()
        if isinstance(child, Gtk.Widget):
            child.add_css_class("lemon-surface")
    except Exception:
        pass

def _make_key_label(text: str) -> Gtk.Label:
    """Left-column key label with forced orange color via markup."""
    lbl = Gtk.Label(xalign=1.0)
    lbl.add_css_class("monospace")
    lbl.set_use_markup(True)
    safe = html.escape(text)
    lbl.set_markup(f'<span foreground="{_KEY_ORANGE}">{safe}:</span>')
    return lbl

class StarInfoPanel(Gtk.Frame):
    """
    Star info panel with orange left labels + actions:
      • Open in SIMBAD
      • Save to disk
    """
    def __init__(self) -> None:
        super().__init__(label=None)
        _install_theme_css()
        self.set_hexpand(True); self.set_vexpand(True)
        self.add_css_class("lemon-surface")
        self.add_css_class("section-frame")

        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hdr.add_css_class("section-header")
        title = Gtk.Label(label="Star info", xalign=0.0)
        title.add_css_class("monospace")
        title.set_hexpand(True)
        hdr.append(title)

        # Body
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(8); body.set_margin_bottom(8)
        body.set_margin_start(10); body.set_margin_end(10)
        body.add_css_class("lemon-surface")

        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        body.append(grid)

        # Value labels
        self._labels: dict[str, Gtk.Label] = {}
        rows = [
            ("id",      "ID"),
            ("ra",      "RA"),
            ("dec",     "Dec"),
            ("pixel_x", "Pixel X"),
            ("pixel_y", "Pixel Y"),
            ("imag",    "Mag"),
            ("filter",  "Filter"),
            ("npts",    "Curve points"),
            ("stdev",   "stdev"),
        ]
        for i, (key, txt) in enumerate(rows):
            k_lbl = _make_key_label(txt)
            v_lbl = Gtk.Label(label="—", xalign=0.0)
            v_lbl.add_css_class("monospace")
            v_lbl.set_selectable(True)
            grid.attach(k_lbl, 0, i, 1, 1)
            grid.attach(v_lbl, 1, i, 1, 1)
            self._labels[key] = v_lbl

        # Actions
        btn_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_bar.add_css_class("lemon-surface")

        self.btn_simbad = Gtk.Button.new_with_label("Open in SIMBAD")
        self.btn_save   = Gtk.Button.new_with_label("Save to disk")

        _style_button(self.btn_simbad, _SIMBAD_BTN)
        _style_button(self.btn_save,   _SAVE_BTN)

        btn_bar.append(self.btn_simbad)
        btn_bar.append(self.btn_save)

        # Pack
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        outer.add_css_class("lemon-surface")
        outer.append(hdr)
        outer.append(body)
        outer.append(btn_bar)
        self.set_child(outer)

        # Mirrors
        self.star_id: int = -1
        self.ra_hms: str = ""
        self.dec_dms: str = ""
        self.field_name: str = ""

    # ---- public API
    def set(self, key: str, value) -> None:
        """Set a field text. 'stdev' values are formatted to 3 decimals."""
        lbl = self._labels.get(key)
        if not lbl:
            return
        if key == "stdev":
            try:
                txt = "—" if value is None else f"{float(value):.3f}"
            except Exception:
                txt = str(value)
        else:
            txt = value if isinstance(value, str) else str(value)
        lbl.set_label(txt)

    def apply_theme_to_buttons(self) -> None:
        _style_button(self.btn_simbad, _SIMBAD_BTN)
        _style_button(self.btn_save,   _SAVE_BTN)
