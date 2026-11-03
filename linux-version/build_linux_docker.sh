#!/usr/bin/env bash
# Inneres Build-Script fuer den Docker-Aufruf in build_windows.bat:
#
#   docker run --rm -v <linux-version>:/app:ro -v <projekt>/icon:/icon:ro \
#     -v <dist>:/out python:3.12-slim bash /app/build_linux_docker.sh
#
# Baut linux-version/mediacenter.py als Schnellstart-Variante (onedir-Paket)
# und verpackt sie zusammen mit launcher_stub.py in die einzelne ELF-Datei
# /out/MediaCenter (Smart-Cache: entpackt einmalig nach /tmp/MediaCenter/<Version>,
# startet danach ohne Entpacken; neu entpacken nur bei geleertem Ordner oder
# aelterer Version).
# Das App-Icon wird von /icon/mediacenter.png ins Bundle uebernommen (--add-data),
# damit das Programm es zur Laufzeit als Fenster-/Taskleisten-Icon setzt.
set -eu

echo "=== Build-Umgebung installieren ==="
# apt-Quellen auf HTTPS: manche Netze verfaelschen HTTP-Inhalte (Hash Sum
# mismatch). Danach Update+Install mit Wiederholungen.
sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
  /etc/apt/sources.list.d/debian.sources 2>/dev/null || true
apt_ok=0
for versuch in 1 2 3 4; do
  if apt-get update -qq >/dev/null \
     && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq binutils patchelf >/dev/null; then
    apt_ok=1
    break
  fi
  echo "  apt Versuch $versuch fehlgeschlagen — erneuter Versuch..."
  sleep 3
done
if [ "$apt_ok" != "1" ]; then
  echo "FEHLER: apt nach 4 Versuchen fehlgeschlagen." >&2
  exit 1
fi

echo "=== venv fuer den Build anlegen (PyInstaller + Abhaengigkeiten) ==="
python3 -m venv /build
/build/bin/pip install --quiet --upgrade pip
/build/bin/pip install --quiet pyinstaller pyinstaller-hooks-contrib PySide6 Pillow

# Qt-Plugins explizit mitbuendeln: ohne sie findet die fertige Datei weder
# xcb/wayland/offscreen (Fenster) noch den JPEG-Codec (Poster).
SITE=/build/lib/python3.12/site-packages/PySide6/Qt/plugins
PLUGIN_ARGS=""
for d in platforms imageformats iconengines styles platformthemes \
         platforminputcontexts xcbglintegrations egldeviceintegrations \
         wayland-decoration-client wayland-graphics-integration-client \
         wayland-shell-integration; do
  if [ -d "$SITE/$d" ]; then
    PLUGIN_ARGS="$PLUGIN_ARGS --add-binary $SITE/$d/*.so:PySide6/Qt/plugins/$d"
  fi
done
cd /app
ICON_ARGS=""
if [ -f /icon/mediacenter.png ]; then
  ICON_ARGS="--add-data /icon/mediacenter.png:icon"
fi

echo "=== Schritt 1: Schnellstart-Paket (onedir) bauen ==="
# Nur Zwischenprodukt in /tmp/payload — dist enthaelt am Ende NUR die
# eine ausfuehrbare Datei MediaCenter (Nutzeranweisung 1.0).
/build/bin/pyinstaller \
  --onedir \
  --windowed \
  --name MediaCenter-fast \
  $ICON_ARGS \
  $PLUGIN_ARGS \
  --distpath /tmp/payload \
  --workpath /tmp/build \
  --specpath /tmp/build \
  --noconfirm \
  mediacenter.py

echo "=== Schritt 2: Launcher (onefile, Smart-Cache) bauen ==="
python3 -c "import shutil; shutil.make_archive('/tmp/mediacenter-payload', 'zip', '/tmp/payload/MediaCenter-fast')"
/build/bin/pyinstaller \
  --onefile \
  --windowed \
  --name MediaCenter \
  --add-data /tmp/mediacenter-payload.zip:. \
  --distpath /out \
  --workpath /tmp/build \
  --specpath /tmp/build \
  --noconfirm \
  launcher_stub.py

chmod 755 /out/MediaCenter
# Gehoerschaft korrigieren, wenn der Aufrufer BUILD_UID/BUILD_GID mitgibt
# (sonst gehoert die ELF dem Container-Root).
if [ -n "${BUILD_UID:-}" ] && [ -n "${BUILD_GID:-}" ]; then
  chown -R "$BUILD_UID:$BUILD_GID" /out
fi
echo "=== Fertig: $(ls -lh /out/MediaCenter | awk '{print $5, $9}') ==="
