@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m pip install -r requirements.txt || exit /b 1
py -3.11 -m playwright install chromium
py -3.11 -m PyInstaller --noconfirm --clean packaging\windows.spec || exit /b 1
if not exist "dist\红薯雷达\红薯雷达.exe" exit /b 1
if not exist "dist\红薯雷达MCP.exe" exit /b 1
for %%F in ("dist\红薯雷达\红薯雷达.exe" "dist\红薯雷达MCP.exe") do @echo Built %%~fF - %%~zF bytes - Windows
