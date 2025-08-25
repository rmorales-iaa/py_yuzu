from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from typing import Optional, Iterable, Type

# Directory helpers for optional by-path import of mining.py
PKG_DIR = Path(__file__).resolve().parent
_SQLITE_MAGIC = b"SQLite format 3\x00"
_PREFERRED_SUFFIXES = {".lemondb", ".db", ".sqlite", ".sqlite3", ".db3"}

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
    """
    Return the first existing database path from cli_hint or argv.
    Accepts .LEMONdB, .db, .sqlite*, or any file that looks like SQLite.
    """
    def _is_sqlite_file(p: Path) -> bool:
        try:
            with p.open("rb") as f:
                return f.read(16) == _SQLITE_MAGIC
        except Exception:
            return False

    # 1) Explicit hint wins if it exists (any suffix)
    if cli_hint:
        p = Path(cli_hint).expanduser()
        if p.is_file():
            return str(p)

    # 2) Scan argv for existing files
    files: list[Path] = []
    for tok in (argv or []):
        try:
            p = Path(str(tok)).expanduser()
        except Exception:
            continue
        if p.is_file():
            files.append(p)

    # 3) Prefer known suffixes
    for p in files:
        if p.suffix.lower() in _PREFERRED_SUFFIXES:
            return str(p)

    # 4) As a fallback, detect SQLite by magic header
    for p in files:
        if _is_sqlite_file(p):
            return str(p)

    return None