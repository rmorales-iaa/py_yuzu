#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

from .app import run_app


def _detect_db_path(cli_hint: Optional[str] = None,
                    argv: Optional[Iterable[str]] = None) -> Optional[str]:
    """
    Prefer an explicit cli_hint; otherwise scan argv for the first
    existing file with .LEMONdB suffix. Ignore directories.
    """
    candidates: list[str] = []
    if cli_hint:
        candidates.append(cli_hint)

    args = list(argv) if argv is not None else sys.argv[1:]
    candidates.extend(str(a) for a in args)

    for c in candidates:
        try:
            p = Path(c).expanduser()
        except Exception:
            continue
        if p.is_file() and p.suffix.lower() == ".lemondb":
            return str(p)
    return None


def main(db_path: Optional[str] = None, **_kwargs) -> int:
    resolved = _detect_db_path(db_path)
    if resolved is None and db_path and Path(db_path).is_dir():
        # Helpful hint if a directory (like the package path) was passed
        sys.stderr.write(
            f"Hint: '{db_path}' is a directory; pass a *.LEMONdB file path.\n"
        )
    return run_app(db_path=resolved)


if __name__ == "__main__":
    raise SystemExit(main())
