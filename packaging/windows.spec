# Build this file on Windows only. PyInstaller does not cross-compile.
from PyInstaller.utils.hooks import collect_data_files
from pathlib import Path

datas = collect_data_files("mcp")
ROOT = Path(SPECPATH).parent
a = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=["PySide6", "playwright"], excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, name="红薯雷达", console=False, icon=str(ROOT / "assets/app-icon.ico"))
mcp_a = Analysis([str(ROOT / "mcp_server.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=["mcp.server.fastmcp"], excludes=["tkinter", "PySide6"])
mcp_pyz = PYZ(mcp_a.pure)
mcp_exe = EXE(mcp_pyz, mcp_a.scripts, mcp_a.binaries, mcp_a.zipfiles, mcp_a.datas, name="红薯雷达MCP", console=False, icon=str(ROOT / "assets/app-icon.ico"))
