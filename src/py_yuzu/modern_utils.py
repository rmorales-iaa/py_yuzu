from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Sequence, Optional

logger = logging.getLogger(__name__)

def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

def which(cmd: str) -> Optional[str]:
    return shutil.which(cmd)

def run_cmd(
    args: Sequence[str] | str,
    cwd: Optional[Path | str] = None,
    env: Optional[dict[str, str]] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=str(cwd) if isinstance(cwd, Path) else cwd,
        env=env,
        capture_output=True,
        text=True,
        check=check,
    )
    if result.stderr:
        logger.debug("stderr from %s: %s", args, result.stderr.strip())
    return result

def ensure_dir(p: Path | str) -> Path:
    path = Path(p)
    path.mkdir(parents=True, exist_ok=True)
    return path

def read_text(p: Path | str, encoding: str = "utf-8") -> str:
    return Path(p).read_text(encoding=encoding)

def write_text(p: Path | str, data: str, encoding: str = "utf-8") -> None:
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    Path(p).write_text(data, encoding=encoding)
