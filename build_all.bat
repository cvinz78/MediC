@echo off
rem ===========================================================================
rem MediaCenter — baut ALLE DREI Varianten nacheinander (Nutzeranweisung 2026-10-27):
rem
rem   [1/3] Windows 10/11-Installer  (build_windows.bat)
rem   [2/3] Windows 7/8.1-EXE        (build_windows_win7.bat, Python 3.8.10/PyQt5)
rem   [3/3] Linux-Onefile            (build_linux.bat, braucht Docker)
rem
rem Danach (nur wenn alle erfolgreich): dist\sha.txt mit vollstaendigen
rem SHA-256-Hashes aller drei Artefakte. Fuer Einzelvarianten bitte die
rem jeweiligen Skripte direkt nutzen.
rem ===========================================================================
setlocal
cd /d "%~dp0"
set "NOPAUSE=1"
set "FAILED="

echo === [1/3] Windows 10/11-Installer (dist\mediacenter-installer.exe) ===
call build_windows.bat
if errorlevel 1 set "FAILED=%FAILED% build_windows.bat"

echo.
echo === [2/3] Windows 7/8.1 (dist\MediaCenter.exe) ===
call build_windows_win7.bat
if errorlevel 1 set "FAILED=%FAILED% build_windows_win7.bat"

echo.
echo === [3/3] Linux (dist\MediaCenter) ===
call build_linux.bat
if errorlevel 1 set "FAILED=%FAILED% build_linux.bat"

echo.
if not defined FAILED (
  echo === SHA-256 ueber alle drei Artefakte ===
  python scripts\write_sha.py
  echo.
  echo Alle drei Varianten erfolgreich gebaut. dist enthaelt:
  dir /b dist
) else (
  echo FEHLER — nicht erfolgreich gebaut:%FAILED%
  echo dist\sha.txt enthaelt nur die Hashes der erfolgreich gebauten Dateien.
)
pause
