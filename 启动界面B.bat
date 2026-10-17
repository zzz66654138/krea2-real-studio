@echo off
chcp 65001 >nul
title 界面B · ComfyUI 工具集
cd /d "%~dp0"

rem Python 路径：优先用同目录/上级的 venv，其次 PATH 里的 python
set "PY="
for %%D in ("%~dp0." "%~dp0.." "%~dp0..\..") do (
    if not defined PY if exist "%%~fD\venv\Scripts\python.exe" set "PY=%%~fD\venv\Scripts\python.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PY set "PY=%%P"
)
set "COMFY=http://127.0.0.1:8188"
set "PORT=8191"

if not exist "%PY%" (
    echo [错误] 找不到 Python: %PY%
    echo 请编辑本文件，把 PY 改成你自己的 python.exe 路径。
    pause
    exit /b
)

echo ============================================================
echo   界面 B · ComfyUI 工具集
echo   地址   : http://127.0.0.1:%PORT%
echo   工具   : 骨架生成 / 图变高清 / 图解析Tag
echo ============================================================
echo.
echo 正在检查 ComfyUI (%COMFY%) ...
"%PY%" -c "import urllib.request,sys;urllib.request.urlopen('%COMFY%/system_stats',timeout=5)" >nul 2>nul
if %errorlevel% neq 0 (
    echo [警告] 连不上 ComfyUI。请先启动 ComfyUI（双击 启动ComfyUI.bat），
    echo        然后再运行本脚本。现在仍会打开界面，但所有功能都会失败。
    echo.
)

start "" http://127.0.0.1:%PORT%
"%PY%" run.py --port %PORT% --comfy-url %COMFY% --no-browser
pause
