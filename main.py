#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def _bootstrap() -> None:
    root = Path(__file__).resolve().parent
    src_dir = root / "src" / "py_yuzu"
    sys.path.insert(0, str(src_dir))


def main() -> int:
    _bootstrap()
    import main_cli
    return int(main_cli.main())


if __name__ == "__main__":
    raise SystemExit(main())
