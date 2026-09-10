from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


APP_NAME = "RedPotatoRadar"
SHANGHAI_TZ = "Asia/Shanghai"


def default_data_dir() -> Path:
    """Return a writable, user-scoped data location without using cwd."""
    override = os.environ.get("RED_POTATO_RADAR_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME


@dataclass(frozen=True)
class AppPaths:
    data_dir: Path

    @property
    def database(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def browser_profile(self) -> Path:
        return self.data_dir / "browser-profile"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    def ensure(self) -> "AppPaths":
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.browser_profile.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return self


def app_paths() -> AppPaths:
    return AppPaths(default_data_dir()).ensure()
