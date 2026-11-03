# MANUAL — MediaCenter as a single file (`mediacenter.py`)

A complete media center in **a single Python file**: poster wall, library scan
(movies/TV shows with NFO, poster, fanart, trailer, subtitles), playback via VLC
(default) or other players, playlists with M3U8 export/import and editable
keyboard shortcuts.

There are two ways to use the program:

- **Chapter A — venv (with pip):** the classic way, recommended for developing and customising.
- **Chapter B — a single executable file (without Python/pip):** the finished Linux
  program with PySide6/Pillow already built in. No Windows and no .exe —
  PyInstaller produces a native ELF binary on Linux, launched with `./MediaCenter`.

## Chapter A: venv build (with pip)

```bash
sudo apt install python3 python3-venv python3-pip
# Player (VLC is the default; mpv is the popular alternative):
sudo apt install vlc
# optional:  sudo apt install mpv
```

`Pillow` and `PySide6` go into the venv as Python packages — no system packages
needed for them (both ship as wheels; libjpeg/zlib are therefore not required).

## 2. venv build (recommended)

The file `mediacenter.py` can live in any folder, e.g.:

```bash
mkdir -p ~/mediacenter && cp mediacenter.py ~/mediacenter/
cd ~/mediacenter

# create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# install the two dependencies
pip install PySide6 Pillow

# start
python3 mediacenter.py
```

For the next start, this is enough:

```bash
cd ~/mediacenter
source .venv/bin/activate
python3 mediacenter.py
```

Tip: if you like, create a launcher:

```bash
# ~/.local/bin/mediacenter  (don't forget chmod +x)
#!/bin/bash
source "$HOME/mediacenter/.venv/bin/activate"
exec python3 "$HOME/mediacenter/mediacenter.py" "$@"
```

## Chapter B: The finished Linux program — without Python and without pip

`build_app.sh` builds the Linux program as **a single executable file**
(`dist/MediaCenter`, ~110 MB — containing Python, PySide6 and Pillow in full).
If you receive this file you need **no Python, no venv, no pip** — just a normal
Linux desktop with VLC (or another player).

**Prerequisite for building:** Docker (the build runs in a `python:3.12-slim`
container so the result works on common Debian/Ubuntu desktops — regardless of
which Python is installed locally).

```bash
./build_app.sh          # builds dist/MediaCenter

# Run the result (copy the file anywhere you like):
mkdir -p ~/Programme && cp dist/MediaCenter ~/Programme/
~/Programme/MediaCenter
```

**Smart cache:** the file extracts the application **once** to
`/tmp/MediaCenter/<version>` and starts it from there. On exit **nothing is
deleted** — every further start goes straight from the cache. Re-extraction only
happens when the folder has been emptied or an older version is present (update).
Disk-cleanup tools may empty the folder — the next start simply extracts again.

What matters here:

- **No .exe, no Windows:** PyInstaller produces native ELF binaries on Linux
  (recognisable with `file dist/MediaCenter`).
- **Size:** around 110 MB — it contains Python, Qt and Pillow in full
  (compressed as an archive; the first extraction to /tmp takes a few seconds,
  after that the file starts directly).
- **Data folder:** the program does NOT write next to itself, but into
  **`~/.medi`** — created automatically on first launch; config/
  (settings.json), data/ (library.db, cache/) and logs/ live there.
  Redirectable via `MEDIACENTER_DATA_DIR=/your/path ./MediaCenter`.
- **Configuring libraries:** as described in Chapter A
  (Settings → Libraries).
- **Very minimal systems** (without a graphical environment) may need
  `sudo apt install libgl1 libegl1 libxkbcommon0 libglib2.0-0
  libdbus-1-3 libfontconfig1` — on normal desktop installations these
  libraries are already present.

If you prefer to build without Docker (at your own risk — the result then only
runs on systems with the same or newer glibc):

```bash
python3 -m venv .venv-build
source .venv-build/bin/activate
pip install pyinstaller PySide6 Pillow
pyinstaller --onefile --windowed --name MediaCenter mediacenter.py
# Result: dist/MediaCenter
```

## 3. First steps in the app

1. **Settings → Libraries → Add**: choose a folder, e.g.
   `~/Documents` (containing movie folders like `8 Mile (2002)/`).
   For each library set a name and a type — next to "Movies" and "TV Shows"
   there is **"Custom category…"**: this lets you create your own types
   (e.g. documentary, comedy); each category gets its own entry in
   the navigation. After changing a type, press **F5 to scan** once.
2. The scan runs automatically in the background (progress with a cancel button);
   afterwards the **poster wall** appears.
3. **Click a poster** = the movie starts in the default player (VLC).
4. **Right-click a poster** = context menu: play, play trailer,
   details/media info, add to playlist, open folder, re-read
   metadata.
5. **Poster wall ↔ list**: the button at the top right switches the movie,
   TV and category views between poster wall and list (TV shows:
   title/year/rating).
6. **"Random" button** at the top right: plays a random movie or, in the TV
   view, a random episode — with an active filter (search, genre, year) from
   the filtered entries; in the season/episode view from the currently opened
   show/season.
6a. **TV navigation**: in the TV view, **"◀ Back"** and **"Forward ▶"**
   (top left, next to the breadcrumb) take you one level back or forward —
   like in a browser. "Forward" becomes active after you have gone back;
   Esc and the clickable names in the breadcrumb work as before.
7. **Without NFO/poster**: the title is derived from the **file name**
   (including the year), a frame from the video serves as the poster (with
   ffmpeg) or the show poster — with neither, a rendered placeholder.
8. **Playlists** on the left in the navigation: new/rename/delete, reorder
   entries with ↑/↓, export/import as M3U8.
9. **Keyboard shortcuts** (Settings → "Shortcuts" tab): double-click an
   action, press the new key. Defaults: Enter=play, i=details, t=trailer,
   Ctrl+F=search, F5=scan, Esc=back, Del=remove playlist entry.
10. **Language** (Settings → "View" tab): **German** (default) or
    **English**. The new language is fully applied after confirming — the
    easiest way is via the offered restart (or later by starting the program
    again).

## 3a. The finished file `dist/MediaCenter` (smart cache)

`dist/MediaCenter` is the **standalone Onefile ELF** (~108 MB): it contains
the complete program as a compressed archive and extracts it **once** to
`/tmp/MediaCenter/<version>` (standard temp folder) — then it starts the app
from there. On exit **nothing is deleted**: every further start goes straight
from the cache. Re-extraction only happens when the folder has been emptied
or an **older version** is present (update). Disk-cleanup tools may empty the
folder — the next start simply extracts again.

## 4. File storage

On first launch the MediaCenter automatically creates the folder **`~/.medi`**.
Settings, database and logs live there — regardless of whether you start it via
`python3 mediacenter.py` or as the finished file `dist/MediaCenter`:

| Folder             | Contents                                 |
|--------------------|------------------------------------------|
| `~/.medi/config/`  | settings.json (libraries, players, theme, shortcuts) |
| `~/.medi/data/`    | library.db (SQLite), `cache/` (thumbnails), `tmp/` (playlist M3U8s) |
| `~/.medi/logs/`    | mediacenter.log (rotating)               |

Saving elsewhere? Set the environment variable:

```bash
MEDIACENTER_DATA_DIR="$HOME/.local/share/mediacenter" python3 mediacenter.py
```

Adopting existing data from an older version (project folder):

```bash
mkdir -p ~/.medi
cp -r /path/to/project-folder/config /path/to/project-folder/data /path/to/project-folder/logs ~/.medi/
```

## 5. Configuring players

The default is VLC (auto-detection: `$PATH` → `/usr/bin` → `/usr/local/bin` →
`/snap/bin` → Flatpak). Additional players in **Settings → Players**:

| Player  | Path/command                  | Arguments                    |
|---------|-------------------------------|------------------------------|
| VLC     | `/usr/bin/vlc`                | `{file}`                     |
| mpv     | `/usr/bin/mpv`                | `{file} --fullscreen`        |
| Flatpak | `flatpak run org.videolan.VLC` | `{file}`                   |

Placeholders: `{file}` = video file, `{subtitle}` = the selected subtitle file
(VLC: `--sub-file {subtitle}`, mpv: `--sub-file={subtitle}`). If no player is
installed / misconfigured, a dialog with a path input opens at playback — it
never fails silently.

## 6. Detected file names (NFO scheme)

- Videos: `.mkv .mp4 .avi .m2ts .iso .wmv .mov .webm .ts .mpg .mpeg .flv` (case-insensitive)
- NFO: `movie.nfo`, `tvshow.nfo`, `<name>.nfo` / `<episode-name>.nfo`
- Poster: `poster.jpg` › `folder.jpg` › `cover.jpg` › `<name>.jpg` › `<name>-poster.jpg` › `<folder>.jpg(-poster.jpg)`
- Fanart: `fanart.jpg`, banner: `banner.jpg`
- Trailer: `trailer.mp4|mkv|mov` › `<name>-trailer.*` › folder `trailer(s)/` › `<trailer>` URL from the NFO (browser or VLC)
- Subtitles: `<name>.srt/.ass/.sub/.vtt`, also in the `subs/` folder
- TV shows: `ShowName/Season 01/S01E02.mkv` (also `1x02`)

## 7. Troubleshooting

- View the log: `less ~/mediacenter/logs/mediacenter.log`
- Scan finds nothing? Check the library path (case sensitivity!), read the status bar
- GUI doesn't start in Docker/on a server: set `QT_QPA_PLATFORM=offscreen` (only useful for testing)
