@echo off
rem ===========================================================================
rem MediaCenter — Build: es werden NUR 2 Dateien erstellt (Nutzeranweisung 1.0):
rem
rem   dist\mediacenter-installer.exe — Windows-Installer (enthaelt die portable
rem                                    UND die installierte Version; App-EXE
rem                                    heisst MediaCenterx64.exe)
rem   dist\MediaCenter               — Linux-Onefile (Smart-Cache)
rem
rem Keine weiteren Ausgaben in dist: kein Starter, kein App-Ordner, kein Zip.
rem Voraussetzung Windows-Seite: Python 3.10+ ("Add python.exe to PATH"!).
rem Optional: Docker fuer den Linux-Build.
rem ===========================================================================
setlocal
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Python nicht gefunden.
  echo Bitte Python 3.10+ installieren und dabei "Add python.exe to PATH" ankreuzen.
  pause
  exit /b 1
)

if not exist icon\mediacenter.png (
  echo FEHLER: icon\mediacenter.png nicht gefunden.
  pause
  exit /b 1
)
python scripts\make_ico.py

if not exist .venv-win python -m venv .venv-win
call .venv-win\Scripts\activate.bat
python -m pip install --upgrade pip
pip install PySide6 Pillow pyinstaller

rem ---------------------------------------------------------------
rem 1) Windows-App (onedir) — Zwischenprodukt in build\payload,
rem    landet NICHT in dist. App-EXE: MediaCenterx64.exe
rem ---------------------------------------------------------------
echo === [1/2] Windows-App als Installations-/Portable-Paket bauen ===
pyinstaller --noconfirm --clean --onedir --windowed --name MediaCenterx64 ^
  --icon icon\mediacenter.ico ^
  --add-data "icon\mediacenter.png;icon" ^
  --distpath build\payload ^
  --workpath build\win ^
  --specpath build\win ^
  mediacenter.py
if errorlevel 1 (
  echo FEHLER: Build der Windows-App fehlgeschlagen.
  pause
  exit /b 1
)

rem ---------------------------------------------------------------
rem 2) Windows-Installer (enthaelt portable + installierte Version)
rem ---------------------------------------------------------------
echo === [2/2] Windows-Installer: mediacenter-installer.exe ===
pyinstaller --noconfirm --clean --onefile --windowed --name mediacenter-installer ^
  --icon icon\mediacenter.ico ^
  --add-data "build\payload\MediaCenterx64;MediaCenterx64" ^
  installer.py
if errorlevel 1 (
  echo FEHLER: Build von mediacenter-installer fehlgeschlagen.
  pause
  exit /b 1
)
echo OK: dist\mediacenter-installer.exe

rem ---------------------------------------------------------------
rem 3) Linux-Onefile: dist\MediaCenter (Smart-Cache)
rem ---------------------------------------------------------------
echo === Linux: MediaCenter (Onefile) ===
if not exist linux-version\mediacenter.py (
  echo FEHLER: linux-version\mediacenter.py nicht gefunden.
  pause
  exit /b 1
)

where docker >nul 2>&1
if not errorlevel 1 (
  echo Docker gefunden — Linux-Version wird im Container neu gebaut, bitte warten...
  docker run --rm -v "%~dp0linux-version":/app:ro -v "%~dp0icon":/icon:ro -v "%~dp0dist":/out python:3.12-slim bash /app/build_linux_docker.sh
  if not errorlevel 1 goto linux_ok
  echo WARNUNG: Docker-Build fehlgeschlagen — fertige Datei wird uebernommen.
)

if not exist linux-version\MediaCenter (
  echo FEHLER: Kein Docker und keine fertige Datei linux-version\MediaCenter vorhanden.
  pause
  exit /b 1
)
copy /Y linux-version\MediaCenter dist\MediaCenter >nul
:linux_ok
if exist dist\MediaCenter echo OK: dist\MediaCenter ^(Linux^)

rem ---------------------------------------------------------------
rem 4) SHA-256-Hashes nach dist\sha.txt schreiben (nur die 2 Dateien)
rem ---------------------------------------------------------------
set "SHAFILE=dist\sha.txt"
del "%SHAFILE%" >nul 2>&1
for %%F in ("mediacenter-installer.exe" "MediaCenter") do (
  if exist "dist\%%~F" (
    for /f "tokens=1" %%H in ('certutil -hashfile "dist\%%~F" SHA256 ^| findstr /r /v "[^0-9a-fA-F]"') do (
      echo "%~dp0dist\%%~F" = %%H>>"%SHAFILE%"
    )
  )
)
echo Hashes geschrieben: dist\sha.txt

echo.
echo Fertig — dist enthaelt nur:
dir /b dist
pause
