@echo off
rem ===========================================================================
rem MediaCenter fuer Windows 7 SP1 / 8.1 als EINE Windows-EXE (PyQt5/Qt 5.15)
rem Qt 6 laeuft dort nicht — der Code in mediacenter.py erkennt das automatisch
rem und verwendet PyQt5, wenn PySide6 fehlt.
rem Ergebnis: dist\MediaCenter.exe (OneFile — kein _internal-Ordner noetig)
rem Voraussetzung: Python 3.8.10 — https://www.python.org/downloads/release/python-3810/
rem   (letzte Windows-7-taugliche Python-Version)
rem ACHTUNG: Nur dieses Skript fuer die Win7-EXE verwenden! Die EXE aus
rem build_windows.bat (MediaCenterx64.exe) ist die Win10/11-Variante (PySide6)
rem und laeuft NICHT auf Windows 7.
rem Der Build ruft den venv-Interpreter mit ABSOLUTEM Pfad auf (python -m
rem PyInstaller) — activate.bat/PATH werden bewusst nicht benutzt, damit nie
rem still ein falsches Python verwendet wird (python315.dll laeuft nicht auf
rem Win7; venvs koennen kopiert sein und dann zeigt activate.bat ins Leere).
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "VPY=%~dp0.venv-win7\Scripts\python.exe"

py -3.8 --version >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Python 3.8 nicht gefunden.
  echo Bitte Python 3.8.10 installieren — https://www.python.org/downloads/release/python-3810/
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
rem venv sicherstellen: fehlt sie oder ist defekt (Basis-Python
rem fehlt/falsch), wird sie neu angelegt. Dann VERIFIZIEREN, dass
rem genau dieser Interpreter Python 3.8 ist.
rem ---------------------------------------------------------------
if exist "%VPY%" (
  "%VPY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)" >nul 2>&1
  if errorlevel 1 (
    echo .venv-win7 ist defekt ^(Basis-Python fehlt oder nicht 3.8^) — wird neu angelegt ...
    rmdir /s /q .venv-win7
  )
)
if not exist "%VPY%" py -3.8 -m venv .venv-win7
if not exist "%VPY%" (
  echo FEHLER: .venv-win7 konnte nicht angelegt werden.
  if not defined NOPAUSE pause
  exit /b 1
)
"%VPY%" -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,8) else 1)"
if errorlevel 1 (
  echo FEHLER: .venv-win7 ist nicht Python 3.8 — Abbruch.
  echo Die Windows-7-EXE darf niemals mit einem anderen Python gebaut werden.
  if not defined NOPAUSE pause
  exit /b 1
)

"%VPY%" -m pip install --upgrade "pip<24.1"
"%VPY%" -m pip install "PyQt5==5.15.11" "Pillow<11" "pyinstaller<6"

echo === Windows-7-EXE bauen (Python 3.8, PyQt5, OneFile) ===
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name MediaCenter ^
  --icon icon\mediacenter.ico ^
  --add-data "icon\mediacenter.png;icon" ^
  --version-file scripts\win7_version.txt ^
  mediacenter.py
if errorlevel 1 (
  echo FEHLER: Build der Windows-7-EXE fehlgeschlagen.
  if not defined NOPAUSE pause
  exit /b 1
)

if not exist dist\MediaCenter.exe (
  echo FEHLER: dist\MediaCenter.exe wurde nicht erstellt.
  if not defined NOPAUSE pause
  exit /b 1
)
python scripts\write_sha.py dist\MediaCenter.exe
echo.
echo OK: dist\MediaCenter.exe (Windows-7-Variante, Python 3.8/PyQt5, OneFile)
if not defined NOPAUSE pause
