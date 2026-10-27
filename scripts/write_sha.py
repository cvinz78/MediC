"""Schreibt SHA-256-Hashes (vollständig, nie gekürzt) als dist/sha.txt.

Aufruf:
    python scripts/write_sha.py [DATEI ...]     (Pfade relativ zum Projektordner)

Mit Argumenten werden genau diese Dateien gehasht; ohne Argumente alle
vorhandenen dist-Artefakte (mediacenter-installer.exe, MediaCenter,
MediaCenter.exe). Dateien unter linux-version/ landen zusätzlich in
linux-version/sha.txt.
"""
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DEFAULTS = [
    DIST / "mediacenter-installer.exe",
    DIST / "MediaCenter",
    DIST / "MediaCenter.exe",
]


def line(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{path} = {digest} ({path.stat().st_size} Bytes)"


def write_group(paths: list[Path], target: Path) -> None:
    if paths:
        target.write_text("\n".join(line(p) for p in paths) + "\n", encoding="utf-8")


def main() -> None:
    if len(sys.argv) > 1:
        files = [(ROOT / arg).resolve() for arg in sys.argv[1:]]
    else:
        files = [p for p in DEFAULTS if p.is_file()]
    missing = [p for p in files if not p.is_file()]
    if missing:
        raise SystemExit(f"FEHLER: Datei(en) fehlen: {missing}")
    if not files:
        raise SystemExit("FEHLER: keine Datei zum Hashen vorhanden.")

    write_group([p for p in files if p.parent == DIST], DIST / "sha.txt")
    write_group(
        [p for p in files if p.parent == ROOT / "linux-version"],
        ROOT / "linux-version" / "sha.txt",
    )
    for p in files:
        print(line(p))


if __name__ == "__main__":
    main()
