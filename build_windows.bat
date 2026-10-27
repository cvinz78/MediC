@echo off
rem ===========================================================================
rem MediaCenter — baut NUR die Windows-10/11-Variante (Nutzeranweisung 2026-10-27):
rem
rem   dist\mediacenter-installer.exe — Installer (enthaelt portable UND
rem                                    installierte Version; App-EXE heisst
rem                                    MediaCenterx64.exe)
rem
rem Nicht Teil dieses Skripts:
rem   Windows 7/8.1  -> build_windows_win7.bat  (Python 3.8.10, PyQt5)
rem   Linux          -> build_linux.bat         (Docker)
rem   alle drei      -> build_all.bat
rem
rem Keine weiteren Ausgaben in dist. Voraussetzung: Python 3.10+
rem ("Add python.exe to PATH"!).
rem Der Build ruft den venv-Interpreter mit ABSOLUTEM Pfad auf (python -m
rem PyInstaller) — activate.bat/PATH werden bewusst nicht benutzt, damit nie
rem still ein falsches Python verwendet wird (kopierte venvs zeigen mit
rem activate.bat sonst ins Leere).
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv-win\Scripts\python.exe"

python --version >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Python nicht gefunden.
  echo Bitte Python 3.10+ installieren und dabei "Add python.exe to PATH" ankreuzen.
  if not defined NOPAUSE pause
  exit /b 1
)

if not exist icon\mediacenter.png (
  echo FEHLER: icon\mediacenter.png nicht gefunden.
  if not defined NOPAUSE pause
  exit /b 1
)
python scripts\make_ico.py
if errorlevel 1 (
  if exist icon\mediacenter.ico (
    echo WARNUNG: make_ico.py fehlgeschlagen — vorhandene ICO wird verwendet.
  ) else (
    echo FEHLER: weder neue noch vorhandene icon\mediacenter.ico.
    if not defined NOPAUSE pause
    exit /b 1
  )
)

rem ---------------------------------------------------------------
rem venv sicherstellen: defekte venv (Basis-Python deinstalliert)
rem wird automatisch neu angelegt. Danach wird verifiziert, dass
rem genau dieser Interpreter Python 3.10+ ist (kein stiller Fallback
rem auf das globale Python).
rem ---------------------------------------------------------------
if exist "%VPY%" (
  "%VPY%" -c "import sys" >nul 2>&1
  if errorlevel 1 (
    echo .venv-win ist defekt ^(Basis-Python fehlt^) — wird neu angelegt ...
    rmdir /s /q .venv-win
  )
)
if not exist "%VPY%" python -m venv .venv-win
if not exist "%VPY%" (
  echo FEHLER: .venv-win konnte nicht angelegt werden.
  if not defined NOPAUSE pause
  exit /b 1
)
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
  echo FEHLER: .venv-win ist nicht Python 3.10+ — Abbruch.
  echo Kein Build mit dem falschen Python-Interpreter.
  if not defined NOPAUSE pause
  exit /b 1
)

"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install PySide6 Pillow pyinstaller

rem ---------------------------------------------------------------
rem 1) Windows-App (onedir) — Zwischenprodukt in build\payload,
rem    landet NICHT in dist. App-EXE: MediaCenterx64.exe
rem ---------------------------------------------------------------
echo === [1/2] Windows-App als Installations-/Portable-Paket bauen ===
"%VPY%" -m PyInstaller --noconfirm --clean --onedir --windowed --name MediaCenterx64 ^
  --icon icon\mediacenter.ico ^
  --add-data "icon\mediacenter.png;icon" ^
  --distpath build\payload ^
  --workpath build\win ^
  --specpath build\win ^
  mediacenter.py
if errorlevel 1 (
  echo FEHLER: Build der Windows-App fehlgeschlagen.
  if not defined NOPAUSE pause
  exit /b 1
)

rem ---------------------------------------------------------------
rem 2) Windows-Installer (enthaelt portable + installierte Version)
rem ---------------------------------------------------------------
echo === [2/2] Windows-Installer: mediacenter-installer.exe ===
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name mediacenter-installer ^
  --icon icon\mediacenter.ico ^
  --add-data "build\payload\MediaCenterx64;MediaCenterx64" ^
  installer.py
if errorlevel 1 (
  echo FEHLER: Build von mediacenter-installer fehlgeschlagen.
  if not defined NOPAUSE pause
  exit /b 1
)

python scripts\write_sha.py dist\mediacenter-installer.exe
echo.
echo OK: dist\mediacenter-installer.exe
if not defined NOPAUSE pause
