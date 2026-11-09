@echo off
setlocal
cd /d "%~dp0"

rem 优先用与本目录同级的 pythonw（venv 场景），其次走 PATH
set "PYW=%~dp0..\Scripts\pythonw.exe"
if exist "%PYW%" goto :run

set "PYW="
for /f "delims=" %%I in ('where pythonw.exe 2^>nul') do if not defined PYW set "PYW=%%I"
if defined PYW goto :run

set "PY="
for /f "delims=" %%I in ('where python.exe 2^>nul') do if not defined PY set "PY=%%I"
if defined PY goto :console

echo.
echo  没有找到 Python。
echo  请安装 Python 3.10 或以上版本，安装时勾选 "Add Python to PATH"。
echo  下载地址：https://www.python.org/downloads/
echo.
pause
exit /b 1

:run
start "" "%PYW%" "%~dp0float_hud.py"
exit /b 0

:console
echo  未找到 pythonw，改用控制台模式启动（关闭本窗口即退出程序）。
echo.
"%PY%" "%~dp0float_hud.py"
pause
exit /b 0
