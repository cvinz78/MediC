#!/usr/bin/env python3
"""MediaCenter — eigenständige Onefile-ELF mit Smart-Cache (Linux).

Die ELF enthält das komplette Programm als komprimiertes Archiv
(mediacenter-payload.zip, beim Build eingebettet). Verhalten:
- Erster Start / geleerter Ordner / ältere Version: Archiv wird EINMALIG
  nach /tmp/MediaCenter/<Version> entpackt und die App von dort gestartet.
- Weitere Starts mit gleicher Version: direkt aus dem Cache, OHNE Entpacken.
- Beim Beenden wird der Cache NICHT gelöscht.
- Das Eigen-Entpacken des Launchers bleibt (Onefile-Physik), beschränkt sich
  aber auf wenige Dateien (Python-Laufzeit + ein Archiv).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

APP_VERSION = "1.6"  # muss zur Version des eingebetteten Pakets passen
APP_NAME = "MediaCenter"
PAYLOAD_ZIP = "mediacenter-payload.zip"
BINARY_NAME = "MediaCenter-fast"
RUNTIME_ROOT = Path(tempfile.gettempdir()) / "MediaCenter"


def payload_zip() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / PAYLOAD_ZIP
        if candidate.is_file():
            return candidate
    return Path(__file__).resolve().parent / "dist" / PAYLOAD_ZIP


def version_in(runtime: Path) -> str:
    try:
        return (runtime / "version.txt").read_text(encoding="ascii").strip()
    except OSError:
        return ""


def show_error(message: str) -> None:
    if os.name == "nt":
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
    else:
        print(message, file=sys.stderr)


def extract(runtime: Path) -> None:
    """Archiv nach runtime entpacken; alte/alte Versionsordner beforehand räumen."""
    archive = payload_zip()
    if not archive.is_file():
        raise FileNotFoundError(f"Anwendungspaket fehlt: {archive}")
    if runtime.parent.exists():
        # Nur die aktuelle Version behalten — /tmp nicht vermüllen.
        shutil.rmtree(runtime.parent, ignore_errors=True)
    runtime.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(runtime)
    binary = runtime / BINARY_NAME
    if binary.is_file():
        os.chmod(binary, 0o755)
    (runtime / "version.txt").write_text(APP_VERSION, encoding="ascii")


def main() -> int:
    runtime = RUNTIME_ROOT / APP_VERSION
    exe = runtime / BINARY_NAME
    if version_in(runtime) != APP_VERSION or not exe.is_file():
        try:
            extract(runtime)
        except Exception as exc:  # noqa: BLE001 — Launcher: Fehler sichtbar machen
            show_error(
                f"MediaCenter konnte nicht entpackt werden:\n{exc}\n\n"
                f"Ziel: {runtime}\nBitte prüfen, ob der Temp-Ordner schreibbar ist."
            )
            return 1
    if not exe.is_file():
        show_error(f"Anwendung nicht gefunden: {exe}")
        return 1
    subprocess.Popen([str(exe)], cwd=str(runtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
