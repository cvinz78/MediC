#!/usr/bin/env bash
# Baut das Linux-Programm (dist/MediaCenter) — läuft ohne Python/pip beim Nutzer.
# Kein Windows, keine .exe: PyInstaller erzeugt unter Linux eine native ELF-Binärdatei.
#
#   ./build_app.sh [zielordner]        (Default: dist)
#
# Zwei Wege, automatisch gewählt:
#   1. Docker installiert UND lauffähig → Build im Container (python:3.12-slim),
#      damit das Ergebnis auf gängigen Debian-/Ubuntu-Desktops läuft.
#   2. kein (laufender) Docker → direkter Build im lokalen venv (.venv-build).
#      Benötigt: python3, pip, binutils (unter Arch: sudo pacman -S binutils).
#
# Smart-Cache (wie die Windows-Version): gebaut wird die Schnellstart-Variante
# (onedir-Paket aus linux-version/mediacenter.py) plus Launcher (onefile,
# linux-version/launcher_stub.py). Die fertige Datei entpackt beim Start
# EINMALIG nach /tmp/MediaCenter/<Version> und startet von dort — beim Beenden
# wird nichts gelöscht, neu entpackt wird nur bei geleertem Ordner oder
# älterer Version.
#
# Das App-Icon (icon/mediacenter.png) wird in beiden Fällen ins Bundle
# übernommen — es erscheint in der Fenstertitelleiste und in der Taskleiste.
set -euo pipefail
cd "$(dirname "$0")"

OUT_DIR="${1:-dist}"
mkdir -p "$OUT_DIR"

if ! [ -f icon/mediacenter.png ]; then
  echo "FEHLER: icon/mediacenter.png nicht gefunden." >&2
  exit 1
fi

# Qt-Plugin-Ordner der angegebenen PySide6-Installation → --add-binary-Argumente
# (explizit mitbündeln: ohne sie findet die fertige Datei weder xcb/wayland/
# offscreen (Fenster) noch den JPEG-Codec (Poster)).
plugin_args() {
  local SITE="$1" d
  for d in platforms imageformats iconengines styles platformthemes \
           platforminputcontexts xcbglintegrations egldeviceintegrations \
           wayland-decoration-client wayland-graphics-integration-client \
           wayland-shell-integration; do
    if [ -d "$SITE/$d" ]; then
      printf '%s\n' "--add-binary=$SITE/$d/*.so:PySide6/Qt/plugins/$d"
    fi
  done
}

if docker info >/dev/null 2>&1; then
  # Image per Umgebungsvariable ueberschreibbar (AGENTS § 14: Arbeit mit Clone,
  # z. B. MEDIAC_DOCKER_IMAGE=mediac-build:3.12-slim ./build_app.sh)
  DOCKER_IMAGE="${MEDIAC_DOCKER_IMAGE:-python:3.12-slim}"
  echo "=== Docker gefunden — Build im Container ($DOCKER_IMAGE) ==="
  BUILD_UID="$(id -u)"
  BUILD_GID="$(id -g)"
  docker run --rm \
    -v "$PWD":/app:ro \
    -v "$PWD/$OUT_DIR":/out \
    -e BUILD_UID -e BUILD_GID \
    "$DOCKER_IMAGE" bash -c '
      set -e
      echo "=== Build-Umgebung installieren ==="
      # apt-Quellen auf HTTPS: manche Netze verfaelschen HTTP-Inhalte (Hash
      # Sum mismatch). Danach Update+Install mit Wiederholungen.
      # Achtung: dieses Inline-Skript steht in einfachen Anfuehrungszeichen —
      # hier nur DOPPELTE Anfuehrungszeichen verwenden!
      sed -i "s|http://deb.debian.org|https://deb.debian.org|g" \
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

      echo "=== venv für den Build anlegen (PyInstaller + Abhängigkeiten) ==="
      python3 -m venv /build
      /build/bin/pip install --quiet --upgrade pip
      /build/bin/pip install --quiet pyinstaller pyinstaller-hooks-contrib PySide6 Pillow

      cd /app
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
      PYI=/build/bin/pyinstaller

      echo "=== Schritt 1: Schnellstart-Paket (onedir) bauen ==="
      # Payload nach /tmp/payload (wie build_linux_docker.sh) — dist enthält
      # am Ende NUR die eine ausführbare Datei.
      $PYI \
        --onedir \
        --windowed \
        --name MediaCenter-fast \
        --add-data /app/icon/mediacenter.png:icon \
        $PLUGIN_ARGS \
        --distpath /tmp/payload \
        --workpath /tmp/build \
        --specpath /tmp/build \
        --noconfirm \
        linux-version/mediacenter.py

      echo "=== Schritt 2: Launcher (onefile, Smart-Cache) bauen ==="
      # Keine einfachen Anfuehrungszeichen innerhalb dieses bash-c-Blocks:
      # Argumente landen in sys.argv statt im Python-Quelltext.
      python3 -c "import shutil,sys; shutil.make_archive(sys.argv[1], sys.argv[2], sys.argv[3])" /tmp/mediacenter-payload zip /tmp/payload/MediaCenter-fast
      $PYI \
        --onefile \
        --windowed \
        --name MediaCenter \
        --add-data /tmp/mediacenter-payload.zip:. \
        --distpath /out \
        --workpath /tmp/build \
        --specpath /tmp/build \
        --noconfirm \
        linux-version/launcher_stub.py
      chown -R "$BUILD_UID:$BUILD_GID" /out
    '
  # Eigentümer absichern: beobachtet wurde, dass der chown im Container ohne
  # Fehlermeldung greifen kann, ohne Wirkung zu zeigen — daher Host-Check mit
  # Container-Fallback.
  if [ -O "$OUT_DIR/MediaCenter" ]; then
    echo "=== Eigentümer OK ==="
  else
    echo "=== Korrigiere Datei-Eigentümer (Container-Fallback) ==="
    docker run --rm -v "$PWD/$OUT_DIR":/fix "$DOCKER_IMAGE" \
      chown -R "$BUILD_UID:$BUILD_GID" /fix
  fi
else
  echo "=== Kein lauffähiges Docker — direkter Build im lokalen venv (.venv-build) ==="
  if ! command -v python3 >/dev/null 2>&1; then
    echo "FEHLER: python3 nicht gefunden (unter Arch: sudo pacman -S python)." >&2
    exit 1
  fi
  [ -d .venv-build ] || python3 -m venv .venv-build
  .venv-build/bin/pip install --quiet --upgrade pip
  .venv-build/bin/pip install --quiet pyinstaller pyinstaller-hooks-contrib PySide6 Pillow
  SITE="$(.venv-build/bin/python -c 'import PySide6, os; print(os.path.join(os.path.dirname(PySide6.__file__), "Qt", "plugins"))')"
  mapfile -t PLUGIN_ARGS < <(plugin_args "$SITE")

  echo "=== Schritt 1: Schnellstart-Paket (onedir) bauen ==="
  .venv-build/bin/pyinstaller \
    --onedir \
    --windowed \
    --name MediaCenter-fast \
    --add-data icon/mediacenter.png:icon \
    "${PLUGIN_ARGS[@]}" \
    --distpath "$OUT_DIR/build-tmp/payload" \
    --workpath "$OUT_DIR/build-tmp" \
    --specpath "$OUT_DIR/build-tmp" \
    --noconfirm \
    linux-version/mediacenter.py

  echo "=== Schritt 2: Launcher (onefile, Smart-Cache) bauen ==="
  .venv-build/bin/python -c "import shutil; shutil.make_archive('$OUT_DIR/build-tmp/mediacenter-payload', 'zip', '$OUT_DIR/build-tmp/payload/MediaCenter-fast')"
  .venv-build/bin/pyinstaller \
    --onefile \
    --windowed \
    --name MediaCenter \
    --add-data "$OUT_DIR/build-tmp/mediacenter-payload.zip:." \
    --distpath "$OUT_DIR" \
    --workpath "$OUT_DIR/build-tmp" \
    --specpath "$OUT_DIR/build-tmp" \
    --noconfirm \
    linux-version/launcher_stub.py
  chmod 755 "$OUT_DIR/MediaCenter"
  rm -rf "$OUT_DIR/build-tmp"
fi

echo
echo "Ausführbare Datei: $OUT_DIR/MediaCenter"
echo "Start:             ./${OUT_DIR%/}/MediaCenter"
