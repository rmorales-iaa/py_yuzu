from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Optional, Iterable, Type

# Directory helpers for optional by-path import of mining.py
PKG_DIR = Path(__file__).resolve().parent

def _import_miner_class() -> Type:
    """
    Best-effort import of LEMONdBMiner (package/absolute/by-path).
    This mirrors your previous behavior but keeps it isolated here.
    """
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
            import sys
            sys.modules["juicer.mining"] = mod
            spec.loader.exec_module(mod)  # type: ignore[attr-defined]
            if hasattr(mod, "LEMONdBMiner"):
                return getattr(mod, "LEMONdBMiner")
            last_exc = RuntimeError("mining.py loaded but LEMONdBMiner not found")

    raise RuntimeError(
        "mining module not available. Ensure juicer/mining.py exists and is importable.\n"
        f"Last import error: {last_exc}"
    )

def _pick_lemondb_from_args(cli_hint: Optional[str],
                            argv: Optional[Iterable[str]]) -> Optional[str]:
    """Return first existing *.LEMONdB file from cli_hint or argv."""
    cand: list[str] = []
    if cli_hint:
        cand.append(cli_hint)
    cand.extend(map(str, (argv or [])))
    for c in cand:
        try:
            p = Path(c).expanduser()
        except Exception:
            continue
        if p.is_file() and p.suffix.lower() == ".lemondb":
            return str(p)
    return None
