from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def hidden_process_kwargs() -> dict:
    """Avoid a Windows console flash without passing Windows flags on macOS."""
    if sys.platform != "win32": return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def browser_install_hint() -> str:
    return "未检测到可用浏览器。请安装最新版 Google Chrome。Windows 也可使用 Microsoft Edge；随后在设置页点击“打开浏览器处理验证”。"


def open_path(path: Path) -> None:
    if sys.platform == "darwin": subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32": os.startfile(str(path))
    else: subprocess.Popen(["xdg-open", str(path)])
