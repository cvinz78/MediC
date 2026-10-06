"""Erzeugt icon/mediacenter.ico (Multi-Size 16–256 px) aus icon/mediacenter.png.

Aufruf: python scripts/make_ico.py
Wird vom Build gebraucht: PyInstaller (--icon) erwartet unter Windows ein ICO.
"""
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "icon" / "mediacenter.png"
DST = ROOT / "icon" / "mediacenter.ico"

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> None:
    img = Image.open(SRC)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    img.save(DST, format="ICO", sizes=SIZES)
    print(f"OK: {DST} ({DST.stat().st_size} Bytes)")


if __name__ == "__main__":
    main()
