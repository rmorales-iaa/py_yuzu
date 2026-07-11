"""Project-level user configuration paths."""

from pathlib import Path

CONFIG_FILENAME = "~/.yuzurc"
CONFIG_PATH = Path(CONFIG_FILENAME).expanduser()
