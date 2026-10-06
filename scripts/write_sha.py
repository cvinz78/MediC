"""Schreibt dist/sha.txt und linux-version/sha.txt mit vollständigen SHA-256-Hashes."""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def line(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{path} = {digest} ({path.stat().st_size} Bytes)"


dist_files = [
    ROOT / "dist" / "mediacenter-installer.exe",
    ROOT / "dist" / "MediaCenter",
    ROOT / "dist" / "MediaCenter.exe",
]
missing = [p for p in dist_files if not p.is_file()]
if missing:
    raise SystemExit(f"FEHLT: {missing}")

(ROOT / "dist" / "sha.txt").write_text(
    "\n".join(line(p) for p in dist_files) + "\n", encoding="utf-8"
)
(ROOT / "linux-version" / "sha.txt").write_text(
    line(ROOT / "linux-version" / "MediaCenter") + "\n", encoding="utf-8"
)
for p in dist_files:
    print(line(p))
