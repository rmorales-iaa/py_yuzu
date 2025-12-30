from __future__ import annotations

import math
from gi.repository import GObject  # type: ignore

# Simple RA/Dec string formatters (sexagesimal)
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

class StarRow(GObject.Object):
    """Row object for Gtk.ColumnView via Gio.ListStore."""
    id = GObject.Property(type=int)
    ra = GObject.Property(type=float)
    dec = GObject.Property(type=float)
    ra_str = GObject.Property(type=str)
    dec_str = GObject.Property(type=str)
    imag = GObject.Property(type=float)

    def __init__(self, *, id: int, ra: float, dec: float, imag: float) -> None:
        super().__init__()
        self.props.id = int(id)
        self.props.ra = float(ra)
        self.props.dec = float(dec)
        self.props.ra_str = _hms_from_deg(ra)
        self.props.dec_str = _dms_from_deg(dec)
        self.props.imag = float(imag) if imag is not None and not math.isnan(imag) else float("nan")
