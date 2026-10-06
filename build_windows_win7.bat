@echo off
rem ===========================================================================
rem MediaCenter fuer Windows 7 SP1 / 8.1 als EINE Windows-EXE (PyQt5/Qt 5.15)
rem Qt 6 laeuft dort nicht — der Code in mediacenter.py erkennt das automatisch
rem und verwendet PyQt5, wenn PySide6 fehlt.
rem Ergebnis: dist\MediaCenter.exe
rem Voraussetzung: Python 3.8.10 — https://www.python.org/downloads/release/python-3810/
rem   (letzte Windows-7-taugliche Python-Version; beim Setup "Add to PATH" ankreuzen)
rem ===========================================================================
setlocal
cd /d "%~dp0"

py -3.8 --version >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Python 3.8 nicht gefunden.
  echo Bitte Python 3.8.10 installieren und dabei "Add python.exe to PATH" ankreuzen.
  pause
  exit /b 1
)

if not exist .venv-win7 py -3.8 -m venv .venv-win7
call .venv-win7\Scripts\activate.bat
python -m pip install --upgrade "pip<24.1"
pip install "PyQt5==5.15.11" "Pillow<11" "pyinstaller<6"

pyinstaller --noconfirm --clean --onefile --windowed --name MediaCenter ^
  --icon icon\mediacenter.ico ^
  --add-data "icon\mediacenter.png;icon" ^
  --version-file scripts\win7_version.txt ^
  mediacenter.py

echo.
echo Fertig: dist\MediaCenter.exe (Windows-7-Variante)
pause
