# Build this file on macOS only, using a native interpreter for the target architecture.
from PyInstaller.utils.hooks import collect_data_files
from pathlib import Path

datas = collect_data_files("mcp")
ROOT = Path(SPECPATH).parent
a = Analysis([str(ROOT / "app.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=["PySide6", "playwright"], excludes=["tkinter"])
pyz = PYZ(a.pure)
app_exe = EXE(pyz, a.scripts, a.binaries, a.zipfiles, a.datas, name="红薯雷达", console=False)
app = BUNDLE(app_exe, name="红薯雷达.app", icon=str(ROOT / "assets/app-icon.icns"), bundle_identifier="com.tongfangyuan.redpotatoradar")
mcp_a = Analysis([str(ROOT / "mcp_server.py")], pathex=[str(ROOT)], datas=datas, hiddenimports=["mcp.server.fastmcp"], excludes=["tkinter", "PySide6"])
mcp_pyz = PYZ(mcp_a.pure)
mcp_exe = EXE(mcp_pyz, mcp_a.scripts, mcp_a.binaries, mcp_a.zipfiles, mcp_a.datas, name="红薯雷达MCP", console=False)
