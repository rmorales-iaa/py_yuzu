# juicer/__init__.py
from __future__ import annotations
from version import __version__, __version_info__

from .star_details_window import StarDetailsWindow
from .star_info_panel import StarInfoPanel
from .string_list_frame import StringListFrame
from .light_curve_view import LightCurveView

__all__ = [
    "StarDetailsWindow",
    "StarInfoPanel",
    "StringListFrame",
    "LightCurveView",
    "__version__", "__version_info__"
]
