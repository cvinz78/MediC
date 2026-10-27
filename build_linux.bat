@echo off
rem ===========================================================================
rem MediaCenter — baut NUR die Linux-Variante (Nutzeranweisung 2026-10-27):
rem
rem   dist\MediaCenter — Linux-Onefile (Smart-Cache-Launcher)
rem
rem Der Build laeuft bewusst im Docker-Container python:3.12-slim, damit das
rem Ergebnis auf gängigen Distributionen läuft — PyInstaller kann nicht
rem kreuzkompilieren.
rem Nicht Teil dieses Skripts: Windows (build_windows.bat / build_windows_win7.bat).
rem ===========================================================================
setlocal
cd /d "%~dp0"

where docker >nul 2>&1
if errorlevel 1 (
  echo FEHLER: Docker nicht gefunden.
  echo Bitte Docker Desktop starten/installieren — der Linux-Build laeuft im Container.
  if not defined NOPAUSE pause
  exit /b 1
)
if not exist linux-version\mediacenter.py (
  echo FEHLER: linux-version\mediacenter.py nicht gefunden.
  if not defined NOPAUSE pause
  exit /b 1
)
if not exist icon\mediacenter.png (
  echo FEHLER: icon\mediacenter.png nicht gefunden.
  if not defined NOPAUSE pause
  exit /b 1
)

echo === Linux: MediaCenter (Onefile) im Docker-Container bauen ===
docker run --rm -v "%~dp0linux-version":/app:ro -v "%~dp0icon":/icon:ro -v "%~dp0dist":/out python:3.12-slim bash /app/build_linux_docker.sh
if errorlevel 1 (
  echo FEHLER: Docker-Build fehlgeschlagen.
  if not defined NOPAUSE pause
  exit /b 1
)
if not exist dist\MediaCenter (
  echo FEHLER: dist\MediaCenter wurde nicht erstellt.
  if not defined NOPAUSE pause
  exit /b 1
)
rem Kopie als Fallback/Referenz im Quellordner aktualisieren + Hashes schreiben
copy /Y dist\MediaCenter linux-version\MediaCenter >nul
python scripts\write_sha.py dist\MediaCenter linux-version\MediaCenter
echo.
echo OK: dist\MediaCenter (Linux)
if not defined NOPAUSE pause
