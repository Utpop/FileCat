@echo off
setlocal enabledelayedexpansion

rem FileCat console launcher.
rem Double-click to start. Keeps the console open so real errors stay visible.
rem Prefer FileCat.vbs if you do not want a console window.
rem
rem ASCII only on purpose: cmd.exe codepage handling of non-ASCII literals in
rem .bat files is unreliable, and a broken launcher is worse than a plain one.
rem Do not chcp here: Python already picks the console codepage, and changing it
rem makes Python's output unreadable on a zh-CN console.

cd /d "%~dp0"

set "PY="

rem 1) interpreter bundled with the project
if exist "%~dp0_vendor\python\python.exe" set "PY=%~dp0_vendor\python\python.exe"

rem 2) common install locations
if not defined PY if exist "C:\Python314\python.exe" set "PY=C:\Python314\python.exe"
if not defined PY if exist "C:\Python313\python.exe" set "PY=C:\Python313\python.exe"
if not defined PY if exist "C:\Python312\python.exe" set "PY=C:\Python312\python.exe"
if not defined PY if exist "C:\Python311\python.exe" set "PY=C:\Python311\python.exe"
if not defined PY if exist "C:\Python310\python.exe" set "PY=C:\Python310\python.exe"

rem 3) per-user installs
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"

rem 4) whatever is on PATH
if not defined PY for %%I in (python.exe) do if not "%%~$PATH:I"=="" set "PY=%%~$PATH:I"

if not defined PY goto nopython

echo [FileCat] Python: !PY!
echo [FileCat] Starting the GUI. Close the window to exit.
echo.

"!PY!" "%~dp0main.py" %*
set "RC=!ERRORLEVEL!"

if not "!RC!"=="0" goto failed
exit /b 0

:nopython
echo.
echo [FileCat] No Python interpreter found.
echo.
echo FileCat needs Python 3.12 or newer.
echo Install it, then double-click this file again, or put python.exe on PATH.
echo.
pause
exit /b 3

:failed
echo.
echo [FileCat] Exit code !RC! -- the error above is the real one, please report it.
pause
exit /b !RC!
