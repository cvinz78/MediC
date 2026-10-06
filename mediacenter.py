#!/usr/bin/env python3
"""MediaCenter — Mediathek-GUI für Linux und Windows, als Einzeldatei.

Posterwand: Klick auf ein Poster startet den Film/Serie im
Standard-Player (VLC, alternativ mpv oder eigener Eintrag), Rechtsklick öffnet
Medieninfos (Details, Trailer, Untertitel), Playlisten mit M3U8-Export/Import,
editierbare Tastenkürzel (Einstellungen → Tab „Tastenkürzel").

Bauen & Starten (siehe auch ANLEITUNG.md):

    python3 -m venv .venv
    source .venv/bin/activate
    pip install PySide6 Pillow
    python3 mediacenter.py

Konfiguration: config/settings.json neben dieser Datei (oder unter dem in der
Umgebungsvariablen MEDIACENTER_DATA_DIR gesetzten Ordner). Datenbank und
Bildercache liegen in data/, Logs in logs/.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import queue
import random
import re
import shlex
import sqlite3
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha1
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFile, ImageFont, ImageOps

# DecompressionBomb-Schutz aufheben: Auch sehr große Poster/Fanarts werden geladen
# und für die Anzeige per ImageOps.contain auf Kachelgröße runterskaliert.
Image.MAX_IMAGE_PIXELS = None
# Abgeschnittene Bilder (unvollständiger Download/Scan) rendern, statt sie
# still als Platzhalter enden zu lassen — gerendert wird, was da ist.
ImageFile.LOAD_TRUNCATED_IMAGES = True

# ===========================================================================
# 1. Pfade & Einstellungen
# ===========================================================================

logger = logging.getLogger("mediacenter")

ENV_DATA_DIR = "MEDIACENTER_DATA_DIR"

SHORTCUT_ACTIONS: dict[str, str] = {
    "play": "Return",
    "details": "I",
    "trailer": "T",
    "search": "Ctrl+F",
    "scan": "F5",
    "back": "Esc",
    "playlist_remove": "Del",
}
VALID_THEMES = ("darkskin", "bluemoon", "creamy")

#: Version der settings.json-Struktur. Dateien ohne diesen Schlüssel stammen
#: aus einer älteren Programmversion und werden beim Laden einmalig migriert.
SETTINGS_VERSION = 2

#: Programmversion — sichtbar im Fenstertitel (Build-Kennzeichnung).
APP_VERSION = "1.6"


def data_root() -> Path:
    """Wurzel aller schreibbaren App-Verzeichnisse (config/, data/, logs/).

    Standard: ~/.medi — beim ersten Start automatisch angelegt (settings.json,
    library.db, Logs, Thumbnail-Cache). MEDIACENTER_DATA_DIR überschreibt die
    Wahl; gilt auch für die als ausführbare Datei gebaute Variante.
    """
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".medi"


def config_dir() -> Path:
    return data_root() / "config"


def data_dir() -> Path:
    return data_root() / "data"


def logs_dir() -> Path:
    return data_root() / "logs"


def cache_dir() -> Path:
    return data_dir() / "cache"


def settings_path() -> Path:
    return config_dir() / "settings.json"


def app_icon_path() -> Path | None:
    """Mitgeliefertes App-Icon (Titelleiste/Taskleiste) finden.

    Als ausführbare Datei (PyInstaller) liegt es über --add-data in
    sys._MEIPASS/icon/, sonst neben dieser Datei im icon/-Ordner.
    """
    base = getattr(sys, "_MEIPASS", None) if getattr(sys, "frozen", False) else None
    if not base:
        base = str(Path(__file__).resolve().parent)
    icon = Path(base) / "icon" / "mediacenter.png"
    return icon if icon.is_file() else None


def _flatpak_app_installed(app_id: str) -> bool:
    flatpak = shutil_which("flatpak")
    if not flatpak:
        return False
    try:
        result = subprocess.run([flatpak, "info", app_id], capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("Flatpak-Erkennung für %s fehlgeschlagen: %s", app_id, exc)
        return False
    return result.returncode == 0


def shutil_which(name: str) -> str | None:
    import shutil

    return shutil.which(name)


def _windows_player_candidates(exe_name: str) -> list[str]:
    """Windows-Fallback-Kette für Player: Registry › Programmordner.

    Der PATH wird vorher schon über shutil_which abgefragt — der VLC-Installer
    unter Windows trägt sich aber meist NICHT in den PATH ein, deshalb werden
    Registry (HKLM/HKCU, auch WOW6432Node) und die üblichen Installationsordner
    zusätzlich geprüft.
    """
    if os.name != "nt":
        return []
    candidates: list[str] = []
    try:
        import winreg

        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for subkey in (r"SOFTWARE\VideoLAN\VLC", r"SOFTWARE\WOW6432Node\VideoLAN\VLC"):
                try:
                    with winreg.OpenKey(root, subkey) as key:
                        value, _ = winreg.QueryValueEx(key, "InstallDir")
                except OSError:
                    continue
                exe = Path(str(value)) / exe_name
                if exe.exists():
                    candidates.append(str(exe))
    except ImportError:
        pass
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    subdirs = ["VideoLAN/VLC"] if exe_name == "vlc.exe" else ["mpv"]
    for base in (program_files, program_files_x86):
        for sub in subdirs:
            candidates.append(str(Path(base) / sub / exe_name))
    if local_appdata:
        candidates.append(str(Path(local_appdata) / "Programs" / "mpv" / exe_name))
    return candidates


def _detect_vlc() -> dict | None:
    for cand in [
        shutil_which("vlc"),
        *_windows_player_candidates("vlc.exe"),
        "/usr/bin/vlc",
        "/usr/local/bin/vlc",
        "/snap/bin/vlc",
    ]:
        if cand and Path(cand).exists():
            return {"name": "VLC", "path": cand, "args": ["{file}"], "default": True}
    if shutil_which("flatpak") and _flatpak_app_installed("org.videolan.VLC"):
        return {
            "name": "VLC (Flatpak)",
            "path": "flatpak run org.videolan.VLC",
            "args": ["{file}"],
            "default": True,
        }
    return None


def _detect_mpv() -> dict | None:
    for cand in [shutil_which("mpv"), *_windows_player_candidates("mpv.exe"), "/usr/bin/mpv"]:
        if cand and Path(cand).exists():
            return {"name": "mpv", "path": cand, "args": ["{file}"], "default": False}
    return None


def default_players() -> list[dict]:
    players = [p for p in (_detect_vlc(), _detect_mpv()) if p]
    if players and not any(p.get("default") for p in players):
        players[0]["default"] = True
    return players


def defaults() -> dict:
    return {
        "libraries": [],
        "players": [],
        "playback": {"fullscreen": False, "youtube_trailer": "browser"},
        "ui": {"theme": "darkskin", "poster_width": 200, "label_under_poster": True},
        "scan": {"scan_on_start": True},
        "shortcuts": dict(SHORTCUT_ACTIONS),
    }


def _deep_merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def normalize_library_type(raw: str) -> str:
    """Bibliothekstyp normalisieren.

    „movies“/„shows“ (und gängige Schreibweisen) bleiben die festen Typen;
    alles andere (z. B. „Doku“) ist eine eigene Kategorie und wird mit
    M3U-kompatibler Groß-/Kleinschreibung so übernommen, wie sie eingegeben wurde.
    """
    value = (raw or "").strip()
    if not value:
        return "movies"
    lowered = value.casefold()
    if lowered in ("movies", "movie", "film", "filme"):
        return "movies"
    if lowered in ("shows", "show", "serie", "serien"):
        return "shows"
    return value


def _validate(cfg: dict) -> dict:
    if not isinstance(cfg.get("libraries"), list):
        cfg["libraries"] = []
    libs = []
    for lib in cfg["libraries"]:
        if not isinstance(lib, dict):
            continue
        entry = {
            "name": str(lib.get("name", "")),
            "type": normalize_library_type(str(lib.get("type", "movies"))),
            "path": str(lib.get("path", "")),
        }
        if entry["name"] and entry["path"]:
            libs.append(entry)
    cfg["libraries"] = libs

    if not isinstance(cfg.get("players"), list):
        cfg["players"] = []
    players = []
    for player in cfg["players"]:
        if not isinstance(player, dict):
            continue
        entry = {
            "name": str(player.get("name", "")),
            "path": str(player.get("path", "")),
            "args": [str(a) for a in player.get("args", ["{file}"]) if isinstance(a, str)],
            "default": bool(player.get("default", False)),
        }
        if entry["name"] and entry["path"]:
            players.append(entry)
    cfg["players"] = players

    playback = cfg.get("playback") if isinstance(cfg.get("playback"), dict) else {}
    playback["fullscreen"] = bool(playback.get("fullscreen", False))
    if playback.get("youtube_trailer") not in ("browser", "vlc"):
        playback["youtube_trailer"] = "browser"
    cfg["playback"] = playback

    ui = cfg.get("ui") if isinstance(cfg.get("ui"), dict) else {}
    if ui.get("theme") not in VALID_THEMES:
        ui["theme"] = "darkskin"
    try:
        ui["poster_width"] = max(100, min(500, int(ui.get("poster_width", 200))))
    except (TypeError, ValueError):
        ui["poster_width"] = 200
    ui["label_under_poster"] = bool(ui.get("label_under_poster", True))
    cfg["ui"] = ui

    shortcuts = cfg.get("shortcuts") if isinstance(cfg.get("shortcuts"), dict) else {}
    validated = {}
    for action_id, default_key in SHORTCUT_ACTIONS.items():
        value = shortcuts.get(action_id)
        if isinstance(value, str) and value.strip():
            validated[action_id] = value.strip()
        else:
            validated[action_id] = default_key
    cfg["shortcuts"] = validated

    scan = cfg.get("scan")
    if not isinstance(scan, dict):
        scan = {}
    cfg["scan"] = {"scan_on_start": bool(scan.get("scan_on_start", True))}
    cfg["settings_version"] = SETTINGS_VERSION
    return cfg


def load_settings() -> dict:
    """Settings laden; fehlende Datei erzeugt Defaults (inkl. Player-Auto-Erkennung).

    Alte Dateien ohne settings_version werden einmalig migriert: Bis Version 1
    öffnete der Player Filme/Serien standardmäßig im Vollbild — die neue
    Voreinstellung ist „aus" (Nutzerentscheid; Schalter: Einstellungen →
    Wiedergabe). Die Migration wird persistiert, damit sie nicht bei jedem
    Start eine geänderte Einstellung wieder überschreibt.
    """
    cfg = defaults()
    path = settings_path()
    needs_migration = False
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.error("settings.json nicht lesbar (%s) — nutze Defaults", exc)
            raw = {}
        if isinstance(raw, dict):
            cfg = _deep_merge(cfg, raw)
            needs_migration = "settings_version" not in raw
        if not cfg["players"]:
            cfg["players"] = default_players()
    else:
        cfg["players"] = default_players()
        save_settings(cfg)
    cfg = _validate(cfg)
    if needs_migration:
        cfg["playback"]["fullscreen"] = False
        logger.info(
            "Settings-Migration auf Version %d: Vollbild-Wiedergabe abgeschaltet "
            "(wieder einschaltbar unter Einstellungen → Wiedergabe)",
            SETTINGS_VERSION,
        )
        try:
            save_settings(cfg)
        except OSError as exc:
            logger.warning("Migrierte settings.json konnte nicht gespeichert werden: %s", exc)
    return cfg


def save_settings(cfg: dict) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ===========================================================================
# 2. Datenbank (SQLite)
# ===========================================================================

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS movies (
    id INTEGER PRIMARY KEY,
    title TEXT, sort_title TEXT, year INTEGER, plot TEXT,
    rating REAL, runtime_min INTEGER, genres TEXT, studio TEXT, director TEXT,
    video_path TEXT, nfo_path TEXT, poster_path TEXT, fanart_path TEXT,
    trailer_path TEXT, folder_path TEXT, date_added TEXT, mtime INTEGER,
    missing INTEGER DEFAULT 0,
    category TEXT NOT NULL DEFAULT 'movies'
);
CREATE TABLE IF NOT EXISTS shows (
    id INTEGER PRIMARY KEY,
    title TEXT, plot TEXT, rating REAL, year INTEGER, genres TEXT, studio TEXT,
    path TEXT, nfo_path TEXT, poster_path TEXT, fanart_path TEXT, banner_path TEXT,
    missing INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seasons (
    id INTEGER PRIMARY KEY,
    show_id INTEGER REFERENCES shows(id),
    number INTEGER, poster_path TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    show_id INTEGER REFERENCES shows(id),
    season INTEGER, episode INTEGER, title TEXT, plot TEXT, rating REAL,
    still_path TEXT, video_path TEXT, nfo_path TEXT,
    mtime INTEGER,
    missing INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY, name TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS playlist_items (
    id INTEGER PRIMARY KEY,
    playlist_id INTEGER REFERENCES playlists(id) ON DELETE CASCADE,
    position INTEGER, ref_type TEXT, ref_id INTEGER, custom_path TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_movies_video_path ON movies(video_path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shows_path ON shows(path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_video_path ON episodes(video_path);
CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id, season, episode);
CREATE INDEX IF NOT EXISTS idx_playlist_items ON playlist_items(playlist_id, position);
"""


@dataclass
class Movie:
    title: str = ""
    sort_title: str = ""
    year: int | None = None
    plot: str = ""
    rating: float | None = None
    runtime_min: int | None = None
    genres: list[str] = field(default_factory=list)
    studio: str = ""
    director: str = ""
    video_path: str = ""
    nfo_path: str | None = None
    poster_path: str | None = None
    fanart_path: str | None = None
    trailer_path: str | None = None
    folder_path: str = ""
    date_added: str | None = None
    mtime: int | None = None
    missing: bool = False
    category: str = "movies"  # Bibliothekstyp (eigene Kategorien, z. B. „Doku")
    id: int | None = None


@dataclass
class Show:
    title: str = ""
    plot: str = ""
    rating: float | None = None
    year: int | None = None
    genres: list[str] = field(default_factory=list)
    studio: str = ""
    path: str = ""
    nfo_path: str | None = None
    poster_path: str | None = None
    fanart_path: str | None = None
    banner_path: str | None = None
    missing: bool = False
    id: int | None = None


@dataclass
class Season:
    show_id: int | None = None
    number: int = 0
    poster_path: str | None = None
    id: int | None = None


@dataclass
class Episode:
    show_id: int | None = None
    season: int = 0
    episode: int = 0
    title: str = ""
    plot: str = ""
    rating: float | None = None
    still_path: str | None = None
    video_path: str = ""
    nfo_path: str | None = None
    mtime: int | None = None
    missing: bool = False
    id: int | None = None


@dataclass
class Playlist:
    name: str = ""
    created_at: str | None = None
    id: int | None = None


@dataclass
class PlaylistItem:
    position: int = 0
    ref_type: str = "movie"
    ref_id: int | None = None
    custom_path: str | None = None
    id: int | None = None
    playlist_id: int | None = None


def db_connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    version_raw = None
    try:
        version_raw = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    except sqlite3.OperationalError:
        pass  # frische DB ohne meta-Tabelle
    old_version = 0
    if version_raw is not None and str(version_raw["value"]).isdigit():
        old_version = int(version_raw["value"])
    conn.executescript(_SCHEMA)
    if old_version < 2:
        # v1 → v2: Kategorie-Spalte für eigene Bibliothekstypen (z. B. „Doku").
        try:
            conn.execute("ALTER TABLE movies ADD COLUMN category TEXT NOT NULL DEFAULT 'movies'")
        except sqlite3.OperationalError:
            pass  # Spalte existiert bereits (frische DB via _SCHEMA)
        # Einmalig alle Filme neu einlesen lassen: Titel-Fallback ist jetzt der
        # DATEINAME (statt Ordnername) und die Kategorie wird gesetzt. mtime=NULL
        # hebt den inkrementellen Skip auf — nach diesem einen Scan gilt er wieder.
        try:
            conn.execute("UPDATE movies SET mtime = NULL")
        except sqlite3.OperationalError as exc:
            logger.warning("Migration v2 (mtime zurücksetzen) fehlgeschlagen: %s", exc)
    if old_version < SCHEMA_VERSION:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    conn.commit()
    return conn


def _genres_to_db(genres: list[str]) -> str:
    return ", ".join(genres)


def _genres_from_db(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _row_category(row: sqlite3.Row) -> str:
    """Kategorie einer movie-Zeile — alte DBs ohne die Spalte gelten als „movies“."""
    try:
        return row["category"] or "movies"
    except (IndexError, KeyError):
        return "movies"


def _row_to_movie(row: sqlite3.Row) -> Movie:
    return Movie(
        id=row["id"],
        title=row["title"] or "",
        sort_title=row["sort_title"] or "",
        year=row["year"],
        plot=row["plot"] or "",
        rating=row["rating"],
        runtime_min=row["runtime_min"],
        genres=_genres_from_db(row["genres"]),
        studio=row["studio"] or "",
        director=row["director"] or "",
        video_path=row["video_path"] or "",
        nfo_path=row["nfo_path"],
        poster_path=row["poster_path"],
        fanart_path=row["fanart_path"],
        trailer_path=row["trailer_path"],
        folder_path=row["folder_path"] or "",
        date_added=row["date_added"],
        mtime=row["mtime"],
        missing=bool(row["missing"]),
        category=_row_category(row),
    )


def _row_to_show(row: sqlite3.Row) -> Show:
    return Show(
        id=row["id"],
        title=row["title"] or "",
        plot=row["plot"] or "",
        rating=row["rating"],
        year=row["year"],
        genres=_genres_from_db(row["genres"]),
        studio=row["studio"] or "",
        path=row["path"] or "",
        nfo_path=row["nfo_path"],
        poster_path=row["poster_path"],
        fanart_path=row["fanart_path"],
        banner_path=row["banner_path"],
        missing=bool(row["missing"]),
    )


def db_count_movies(
    conn: sqlite3.Connection, include_missing: bool = False, category: str | None = None
) -> int:
    """Anzahl vorhandener (nicht fehlender) Filme — optional je Kategorie."""
    where = "" if include_missing else " WHERE missing = 0"
    if category is not None:
        where += (" AND" if where else " WHERE") + " category = ? COLLATE NOCASE"
        return int(conn.execute(f"SELECT COUNT(*) FROM movies{where}", (category,)).fetchone()[0])
    return int(conn.execute(f"SELECT COUNT(*) FROM movies{where}").fetchone()[0])


def db_count_shows(conn: sqlite3.Connection, include_missing: bool = False) -> int:
    """Anzahl vorhandener (nicht fehlender) Serien."""
    if include_missing:
        return int(conn.execute("SELECT COUNT(*) FROM shows").fetchone()[0])
    return int(conn.execute("SELECT COUNT(*) FROM shows WHERE missing = 0").fetchone()[0])


def _is_under(path: Path, root: Path) -> bool:
    """Pfad liegt unter root — 3.8-kompatibel (is_relative_to gibt es erst ab 3.9).

    Lexikalischer Vergleich wie is_relative_to; auf Windows groß-/klein-
    schreibungsunabhängig (pathlib vergleicht dort case-insensitiv).
    """
    try:
        Path(path).relative_to(Path(root))
        return True
    except ValueError:
        return False


def db_count_library_items(conn: sqlite3.Connection, root: Path) -> tuple[int, int]:
    """(Anzahl Filme, Anzahl Serien) einer Bibliothek anhand der Pfade."""
    root = Path(root)
    movies = shows = 0
    for movie in db_list_movies(conn):
        if _is_under(movie.video_path, root) and not movie.missing:
            movies += 1
    for show in db_list_shows(conn):
        if _is_under(show.path, root) and not show.missing:
            shows += 1
    return movies, shows


def _row_to_episode(row: sqlite3.Row) -> Episode:
    return Episode(
        id=row["id"],
        show_id=row["show_id"],
        season=row["season"] or 0,
        episode=row["episode"] or 0,
        title=row["title"] or "",
        plot=row["plot"] or "",
        rating=row["rating"],
        still_path=row["still_path"],
        video_path=row["video_path"] or "",
        nfo_path=row["nfo_path"],
        mtime=row["mtime"],
        missing=bool(row["missing"]),
    )


def db_upsert_movie(conn: sqlite3.Connection, movie: Movie) -> Movie:
    existing = conn.execute(
        "SELECT id, date_added FROM movies WHERE video_path = ?", (movie.video_path,)
    ).fetchone()
    date_added = (
        existing["date_added"]
        if existing
        else (movie.date_added or datetime.now().astimezone().isoformat(timespec="seconds"))
    )
    sort_title = movie.sort_title or movie.title
    params = (
        movie.title,
        sort_title,
        movie.year,
        movie.plot,
        movie.rating,
        movie.runtime_min,
        _genres_to_db(movie.genres),
        movie.studio,
        movie.director,
        movie.video_path,
        movie.nfo_path,
        movie.poster_path,
        movie.fanart_path,
        movie.trailer_path,
        movie.folder_path,
        date_added,
        movie.mtime,
        int(movie.missing),
        movie.category or "movies",
    )
    if existing:
        conn.execute(
            """UPDATE movies SET title=?, sort_title=?, year=?, plot=?, rating=?,
               runtime_min=?, genres=?, studio=?, director=?, video_path=?, nfo_path=?,
               poster_path=?, fanart_path=?, trailer_path=?, folder_path=?,
               date_added=?, mtime=?, missing=?, category=? WHERE id=?""",
            params + (existing["id"],),
        )
        movie.id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO movies (title, sort_title, year, plot, rating, runtime_min,
               genres, studio, director, video_path, nfo_path, poster_path, fanart_path,
               trailer_path, folder_path, date_added, mtime, missing, category)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            params,
        )
        movie.id = int(cur.lastrowid)
    conn.commit()
    return movie


def db_get_movie_by_path(conn: sqlite3.Connection, video_path: str) -> Movie | None:
    row = conn.execute("SELECT * FROM movies WHERE video_path = ?", (video_path,)).fetchone()
    return _row_to_movie(row) if row else None


def db_list_movies(conn: sqlite3.Connection, category: str | None = None) -> list[Movie]:
    """Alle Filme; mit category nur die einer Kategorie (Groß-/Kleinschreibung egal)."""
    if category is None:
        rows = conn.execute("SELECT * FROM movies ORDER BY sort_title COLLATE NOCASE").fetchall()
        return [_row_to_movie(row) for row in rows]
    rows = conn.execute(
        "SELECT * FROM movies WHERE category = ? COLLATE NOCASE ORDER BY sort_title COLLATE NOCASE",
        (category,),
    ).fetchall()
    return [_row_to_movie(row) for row in rows]


def db_set_movie_missing(conn: sqlite3.Connection, movie_id: int, missing: bool) -> None:
    conn.execute("UPDATE movies SET missing = ? WHERE id = ?", (int(missing), movie_id))
    conn.commit()


def db_upsert_show(conn: sqlite3.Connection, show: Show) -> Show:
    existing = conn.execute("SELECT id FROM shows WHERE path = ?", (show.path,)).fetchone()
    params = (
        show.title,
        show.plot,
        show.rating,
        show.year,
        _genres_to_db(show.genres),
        show.studio,
        show.path,
        show.nfo_path,
        show.poster_path,
        show.fanart_path,
        show.banner_path,
        int(show.missing),
    )
    if existing:
        conn.execute(
            """UPDATE shows SET title=?, plot=?, rating=?, year=?, genres=?, studio=?,
               path=?, nfo_path=?, poster_path=?, fanart_path=?, banner_path=?, missing=?
               WHERE id=?""",
            params + (existing["id"],),
        )
        show.id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO shows (title, plot, rating, year, genres, studio, path,
               nfo_path, poster_path, fanart_path, banner_path, missing)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            params,
        )
        show.id = int(cur.lastrowid)
    conn.commit()
    return show


def db_get_show_by_path(conn: sqlite3.Connection, path: str) -> Show | None:
    row = conn.execute("SELECT * FROM shows WHERE path = ?", (path,)).fetchone()
    return _row_to_show(row) if row else None


def db_list_shows(conn: sqlite3.Connection) -> list[Show]:
    rows = conn.execute("SELECT * FROM shows ORDER BY title COLLATE NOCASE").fetchall()
    return [_row_to_show(row) for row in rows]


def db_set_show_missing(conn: sqlite3.Connection, show_id: int, missing: bool) -> None:
    conn.execute("UPDATE shows SET missing = ? WHERE id = ?", (int(missing), show_id))
    conn.commit()


def _bump_genre(raw: str, counts: dict[str, list]) -> None:
    for part in raw.split(","):
        genre = part.strip()
        if not genre:
            continue
        entry = counts.get(genre.casefold())
        if entry is None:
            counts[genre.casefold()] = [genre, 1]
        else:
            entry[1] += 1


def db_genres_with_counts(
    conn: sqlite3.Connection,
    kind: str = "all",
    category: str | None = None,
) -> list[tuple[str, int]]:
    """Alle vorhandenen Genres (aus den NFO-<genre>-Tags der Scans) alphabetisch
    mit Anzahl — Grundlage für den Genre-Filter im Hauptfenster.

    kind: "movies" = nur Filme (mit category nur die einer Kategorie),
    "shows" = nur Serien, "all" = beides."""
    counts: dict[str, list] = {}
    if kind in ("all", "movies"):
        sql = "SELECT genres FROM movies WHERE genres IS NOT NULL"
        params: list = []
        if category is not None:
            sql += " AND category = ? COLLATE NOCASE"
            params.append(category)
        for (raw,) in conn.execute(sql, params):
            if raw:
                _bump_genre(raw, counts)
    if kind in ("all", "shows"):
        for (raw,) in conn.execute("SELECT genres FROM shows WHERE genres IS NOT NULL"):
            if raw:
                _bump_genre(raw, counts)
    return sorted(((display, n) for display, n in counts.values()), key=lambda pair: pair[0].casefold())


def db_replace_seasons(conn: sqlite3.Connection, show_id: int, seasons: list[Season]) -> None:
    conn.execute("DELETE FROM seasons WHERE show_id = ?", (show_id,))
    for season in seasons:
        cur = conn.execute(
            "INSERT INTO seasons (show_id, number, poster_path) VALUES (?,?,?)",
            (show_id, season.number, season.poster_path),
        )
        season.id = int(cur.lastrowid)
        season.show_id = show_id
    conn.commit()


def db_list_seasons(conn: sqlite3.Connection, show_id: int) -> list[Season]:
    rows = conn.execute("SELECT * FROM seasons WHERE show_id = ? ORDER BY number", (show_id,)).fetchall()
    return [
        Season(id=row["id"], show_id=row["show_id"], number=row["number"], poster_path=row["poster_path"])
        for row in rows
    ]


def db_upsert_episode(conn: sqlite3.Connection, episode: Episode) -> Episode:
    existing = conn.execute("SELECT id FROM episodes WHERE video_path = ?", (episode.video_path,)).fetchone()
    params = (
        episode.show_id,
        episode.season,
        episode.episode,
        episode.title,
        episode.plot,
        episode.rating,
        episode.still_path,
        episode.video_path,
        episode.nfo_path,
        episode.mtime,
        int(episode.missing),
    )
    if existing:
        conn.execute(
            """UPDATE episodes SET show_id=?, season=?, episode=?, title=?, plot=?,
               rating=?, still_path=?, video_path=?, nfo_path=?, mtime=?, missing=?
               WHERE id=?""",
            params + (existing["id"],),
        )
        episode.id = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO episodes (show_id, season, episode, title, plot, rating,
               still_path, video_path, nfo_path, mtime, missing)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            params,
        )
        episode.id = int(cur.lastrowid)
    conn.commit()
    return episode


def db_list_episodes(conn: sqlite3.Connection, show_id: int) -> list[Episode]:
    rows = conn.execute(
        "SELECT * FROM episodes WHERE show_id = ? ORDER BY season, episode", (show_id,)
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def db_get_episode(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return _row_to_episode(row) if row else None


def db_get_episode_by_path(conn: sqlite3.Connection, video_path: str) -> Episode | None:
    row = conn.execute("SELECT * FROM episodes WHERE video_path = ?", (video_path,)).fetchone()
    return _row_to_episode(row) if row else None


def db_set_episode_missing(conn: sqlite3.Connection, episode_id: int, missing: bool) -> None:
    conn.execute("UPDATE episodes SET missing = ? WHERE id = ?", (int(missing), episode_id))
    conn.commit()


def db_delete_movie(conn: sqlite3.Connection, movie_id: int) -> None:
    """Eintrag NUR aus der Bibliothek entfernen — Dateien auf der Festplatte
    werden niemals angefasst (Löschen ausschließlich über den Datei-Explorer)."""
    conn.execute("DELETE FROM playlist_items WHERE ref_type = 'movie' AND ref_id = ?", (movie_id,))
    conn.execute("DELETE FROM movies WHERE id = ?", (movie_id,))
    conn.commit()


def db_delete_episode(conn: sqlite3.Connection, episode_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE ref_type = 'episode' AND ref_id = ?", (episode_id,))
    conn.execute("DELETE FROM episodes WHERE id = ?", (episode_id,))
    conn.commit()


def db_delete_show(conn: sqlite3.Connection, show_id: int) -> None:
    """Serie samt Staffeln und Episoden aus der Bibliothek entfernen (nur DB)."""
    conn.execute(
        "DELETE FROM playlist_items WHERE ref_type = 'episode' AND ref_id IN "
        "(SELECT id FROM episodes WHERE show_id = ?)",
        (show_id,),
    )
    conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM seasons WHERE show_id = ?", (show_id,))
    conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    conn.commit()


def _under_any_root(path_str: str | None, roots: list[Path]) -> bool:
    if not path_str:
        return False
    try:
        path = Path(path_str)
    except (TypeError, ValueError):
        return False
    return any(_is_under(path, root) for root in roots)


def db_prune_outside_libraries(conn: sqlite3.Connection, libraries: list[dict]) -> int:
    """Einträge entfernen, die unter KEINER konfigurierten Bibliothek mehr liegen
    (z. B. nach dem Entfernen einer Bibliothek in den Einstellungen).

    Löscht ausschließlich Datenbankzeilen (Filme, Serien samt Staffeln/Episoden
    inkl. Playlist-Verweise) — niemals Dateien. Rückgabe: Anzahl entfernter
    Top-Level-Einträge (Filme + Serien).
    """
    roots = [Path(lib["path"]) for lib in libraries if lib.get("path")]
    removed = 0
    for movie in db_list_movies(conn):
        if not _under_any_root(movie.video_path, roots):
            db_delete_movie(conn, movie.id)
            removed += 1
    for show in db_list_shows(conn):
        if not _under_any_root(show.path, roots):
            db_delete_show(conn, show.id)
            removed += 1
    return removed


def db_create_playlist(conn: sqlite3.Connection, name: str) -> Playlist:
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    cur = conn.execute("INSERT INTO playlists (name, created_at) VALUES (?,?)", (name, created))
    conn.commit()
    return Playlist(id=int(cur.lastrowid), name=name, created_at=created)


def db_rename_playlist(conn: sqlite3.Connection, playlist_id: int, name: str) -> None:
    conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))
    conn.commit()


def db_delete_playlist(conn: sqlite3.Connection, playlist_id: int) -> None:
    conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))
    conn.commit()


def db_list_playlists(conn: sqlite3.Connection) -> list[Playlist]:
    rows = conn.execute("SELECT * FROM playlists ORDER BY name COLLATE NOCASE").fetchall()
    return [Playlist(id=row["id"], name=row["name"] or "", created_at=row["created_at"]) for row in rows]


def db_add_playlist_item(
    conn: sqlite3.Connection, playlist_id: int, ref_type: str, ref_id: int | None, custom_path: str | None
) -> int:
    max_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS pos FROM playlist_items WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    cur = conn.execute(
        """INSERT INTO playlist_items (playlist_id, position, ref_type, ref_id, custom_path)
           VALUES (?,?,?,?,?)""",
        (playlist_id, max_row["pos"] + 1, ref_type, ref_id, custom_path),
    )
    conn.commit()
    return int(cur.lastrowid)


def db_add_episodes_to_playlist(conn: sqlite3.Connection, playlist_id: int, episode_ids: list[int]) -> None:
    """Episoden am Ende der Playlist anhängen (z. B. ganze Serie) — ein Commit."""
    if not episode_ids:
        return
    max_row = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS pos FROM playlist_items WHERE playlist_id = ?",
        (playlist_id,),
    ).fetchone()
    conn.executemany(
        """INSERT INTO playlist_items (playlist_id, position, ref_type, ref_id, custom_path)
           VALUES (?,?,?,?,?)""",
        [
            (playlist_id, max_row["pos"] + 1 + offset, "episode", episode_id, None)
            for offset, episode_id in enumerate(episode_ids)
        ],
    )
    conn.commit()


def db_list_playlist_items(conn: sqlite3.Connection, playlist_id: int) -> list[PlaylistItem]:
    rows = conn.execute(
        "SELECT * FROM playlist_items WHERE playlist_id = ? ORDER BY position", (playlist_id,)
    ).fetchall()
    return [
        PlaylistItem(
            id=row["id"],
            playlist_id=row["playlist_id"],
            position=row["position"],
            ref_type=row["ref_type"] or "movie",
            ref_id=row["ref_id"],
            custom_path=row["custom_path"],
        )
        for row in rows
    ]


def db_remove_playlist_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM playlist_items WHERE id = ?", (item_id,))
    conn.commit()


def db_reorder_playlist_items(conn: sqlite3.Connection, ordered_ids: list[int]) -> None:
    for position, item_id in enumerate(ordered_ids):
        conn.execute("UPDATE playlist_items SET position = ? WHERE id = ?", (position, item_id))
    conn.commit()


# ===========================================================================
# 3. NFO-Parser (robust)
# ===========================================================================

_ROOT_OPEN_RE = re.compile(r"<\s*([A-Za-z][\w:.-]*)")


@dataclass
class NfoData:
    title: str = ""
    sorttitle: str = ""
    plot: str = ""
    outline: str = ""
    year: int | None = None
    rating: float | None = None
    runtime_min: int | None = None
    genres: list[str] = field(default_factory=list)
    studio: str = ""
    directors: list[str] = field(default_factory=list)
    actors: list[tuple[str, str]] = field(default_factory=list)
    thumb: str = ""
    fanart: str = ""
    trailer: str = ""
    season: int | None = None
    episode: int | None = None
    aired: str = ""
    showtitle: str = ""
    root_tag: str = ""


def nfo_read_text(path: Path) -> str | None:
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        logger.warning("NFO nicht lesbar: %s (%s)", path, exc)
        return None
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_first_root(text: str) -> str | None:
    start = text.find("<")
    if start < 0:
        return None
    xml = text[start:]
    match = _ROOT_OPEN_RE.search(xml)  # überspringt <?xml…?>, <!--…-->, <!DOCTYPE…>
    if not match:
        return None
    root_name = match.group(1)
    close_tag = f"</{root_name}>"
    close_pos = xml.find(close_tag)
    if close_pos < 0:
        return xml
    return xml[: close_pos + len(close_tag)]


def _int_or_none(text: str | None) -> int | None:
    if not text:
        return None
    match = re.search(r"\d{1,4}", text)
    return int(match.group(0)) if match else None


def _float_or_none(text: str | None) -> float | None:
    if not text:
        return None
    match = re.search(r"\d+(?:[.,]\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def _tag_text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _tag_texts(root: ET.Element, tag: str) -> list[str]:
    return [(n.text or "").strip() for n in root.findall(tag) if (n.text or "").strip()]


def _tag_int(root: ET.Element, tag: str) -> int | None:
    return _int_or_none(_tag_text(root, tag))


def _tag_float(root: ET.Element, tag: str) -> float | None:
    value = _float_or_none(_tag_text(root, tag))
    if value is not None:
        return value
    node = root.find(tag)
    if node is not None:
        value = _float_or_none(_tag_text(node, "value"))
        if value is not None:
            return value
    # Modernes Format: <ratings><rating …><value>7.1</value></rating></ratings>
    nested = root.find(f"{tag}s/{tag}")
    if nested is not None:
        return _float_or_none(_tag_text(nested, "value"))
    return None


def parse_nfo_text(text: str) -> NfoData | None:
    xml = _extract_first_root(text)
    if not xml:
        return None
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("NFO nicht als XML geparst: %s", exc)
        return None
    if root.tag not in ("movie", "tvshow", "episodedetails", "musicvideo"):
        return None

    data = NfoData(root_tag=root.tag)
    data.title = _tag_text(root, "title")
    data.sorttitle = _tag_text(root, "sorttitle")
    data.plot = _tag_text(root, "plot")
    data.outline = _tag_text(root, "outline")
    data.year = _tag_int(root, "year")
    data.rating = _tag_float(root, "rating")
    data.runtime_min = _tag_int(root, "runtime")
    data.genres = _tag_texts(root, "genre")
    data.studio = _tag_text(root, "studio")
    data.directors = _tag_texts(root, "director")
    for actor in root.findall("actor"):
        name = _tag_text(actor, "name")
        if name:
            data.actors.append((name, _tag_text(actor, "role")))
    data.thumb = _tag_text(root, "thumb")
    fanart_node = root.find("fanart")
    if fanart_node is not None:
        thumbs = _tag_texts(fanart_node, "thumb")
        data.fanart = thumbs[0] if thumbs else (fanart_node.text or "").strip()
    data.trailer = _tag_text(root, "trailer")
    data.season = _tag_int(root, "season")
    data.episode = _tag_int(root, "episode")
    data.aired = _tag_text(root, "aired")
    data.showtitle = _tag_text(root, "showtitle")
    return data


def parse_nfo_file(path: Path) -> NfoData | None:
    text = nfo_read_text(Path(path))
    return parse_nfo_text(text) if text else None


# ===========================================================================
# 4. Datei-Erkennung (Poster, Trailer, Untertitel, NFOs)
# ===========================================================================

VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".m4v",
    ".avi",
    ".divx",
    ".m2ts",
    ".mts",
    ".iso",
    ".wmv",
    ".mov",
    ".webm",
    ".ts",
    ".mpg",
    ".mpeg",
    ".flv",
    ".vob",
    ".ogm",
    ".ogv",
    ".asf",
    ".3gp",
    ".rm",
    ".rmvb",
    ".m2p",
    ".tp",
}
SUBTITLE_EXTENSIONS = (".srt", ".ass", ".sub", ".vtt")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")
POSTER_PRIORITY = ("poster", "folder", "cover")

#: Episoden-Matching: SxxEyy zuerst, dann NxNN (AGENTS.md § 6)
SXE_RE = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")
NXN_RE = re.compile(r"(?<!\d)(\d{1,2})x(\d{1,3})(?!\d)")
#: Staffel-Ordner: "Season 01", "Staffel 1", "S01" (gängig, case-insensitiv)
SEASON_FOLDER_RE = re.compile(r"^(?:season|staffel|s)[\s._-]*(\d{1,2})$", re.IGNORECASE)
#: Episoden-Fallback ohne SxxEyy: "E01", "EP02", "Episode 3", "Folge04", "05 - Titel", "007"
EPISODE_NUM_PATTERNS = (
    re.compile(r"^(?:e|ep|episode|episod|folge|fol)[\s._-]*(\d{1,3})\b", re.IGNORECASE),
    re.compile(r"\b(?:e|ep|episode|episod|folge|fol)[\s._-]*(\d{1,3})$", re.IGNORECASE),
    re.compile(r"^(\d{1,3})[\s._-]"),
    re.compile(r"^(\d{1,3})$"),
)


def is_season_folder(name: str) -> int | None:
    """Staffelnummer eines Ordner-Namens (S01/Season 01/Staffel 1) oder None."""
    match = SEASON_FOLDER_RE.match(name.strip())
    return int(match.group(1)) if match else None


def fallback_episode_number(stem: str) -> int | None:
    """Episodennummer aus Dateinamen ohne SxxEyy-Muster (E01, 05 - Titel, 007)."""
    for pattern in EPISODE_NUM_PATTERNS:
        match = pattern.search(stem)
        if match:
            return int(match.group(1))
    return None


def is_video_file(path: Path) -> bool:
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def _dir_listing(folder: Path) -> list[Path]:
    try:
        return list(Path(folder).iterdir())
    except OSError as exc:
        logger.warning("Ordner nicht lesbar: %s (%s)", folder, exc)
        return []


def _by_lower_name(folder: Path) -> dict[str, Path]:
    return {entry.name.lower(): entry for entry in _dir_listing(folder) if entry.is_file()}


def _subdirs(folder: Path) -> list[Path]:
    return [entry for entry in _dir_listing(folder) if entry.is_dir()]


def _match_names(names: dict[str, Path], candidates: list[str]) -> Path | None:
    for candidate in candidates:
        hit = names.get(candidate.lower())
        if hit:
            return hit
    return None


def _image_candidates(basename: str) -> list[str]:
    return [f"{basename}{ext}" for ext in IMAGE_EXTENSIONS]


def find_nfo(folder: Path, video_basename: str, kind: str = "movie") -> Path | None:
    names = _by_lower_name(folder)
    fixed = {"movie": "movie.nfo", "tvshow": "tvshow.nfo"}
    if kind in fixed:
        hit = names.get(fixed[kind])
        if hit:
            return hit
    return _match_names(names, [f"{video_basename}.nfo"])


def find_poster(folder: Path, video_basename: str = "", folder_name: str = "") -> Path | None:
    """poster.* › folder.* › cover.* › <basename>.* › <basename>-poster.* › <ordnername>.*(-poster.*)"""
    names = _by_lower_name(folder)
    candidates: list[str] = []
    for base in POSTER_PRIORITY:
        candidates.extend(_image_candidates(base))
    if video_basename:
        candidates.extend(_image_candidates(video_basename))
        candidates.extend(_image_candidates(f"{video_basename}-poster"))
    if folder_name:
        candidates.extend(_image_candidates(folder_name))
        candidates.extend(_image_candidates(f"{folder_name}-poster"))
    return _match_names(names, candidates)


def find_fanart(folder: Path) -> Path | None:
    return _match_names(_by_lower_name(folder), _image_candidates("fanart"))


def find_banner(folder: Path) -> Path | None:
    return _match_names(_by_lower_name(folder), _image_candidates("banner"))


def find_still(folder: Path, video_basename: str) -> Path | None:
    names = _by_lower_name(folder)
    return _match_names(
        names, _image_candidates(video_basename) + _image_candidates(f"{video_basename}-thumb")
    )


def find_season_poster(show_folder: Path, season_folder: Path, season_number: int) -> Path | None:
    """season01-poster.* (auch unpadet/Staffel) im Show- UND Staffel-Ordner,
    danach poster/folder/cover im Staffel-Ordner."""
    padded = f"{season_number:02d}"
    season_names = [
        f"season{padded}-poster",
        f"season{season_number}-poster",
        f"staffel{padded}-poster",
        f"staffel{season_number}-poster",
        f"season{padded}",
        f"season{season_number}",
    ]
    candidates = [f"{name}{ext}" for name in season_names for ext in IMAGE_EXTENSIONS]
    hit = _match_names(_by_lower_name(show_folder), candidates)
    if hit:
        return hit
    hit = _match_names(_by_lower_name(season_folder), candidates)
    if hit:
        return hit
    return find_poster(season_folder, folder_name=season_folder.name)


def find_trailer(folder: Path, video_basename: str) -> Path | None:
    """trailer.mp4|mkv|mov › <basename>-trailer.* › trailer(s)/-Unterordner."""
    names = _by_lower_name(folder)
    candidates = [f"trailer{ext}" for ext in (".mp4", ".mkv", ".mov")]
    for entry in names:
        stem = Path(entry).stem
        suffix = Path(entry).suffix
        if stem.lower() == f"{video_basename.lower()}-trailer" and suffix.lower() in VIDEO_EXTENSIONS:
            candidates.append(entry)
    hit = _match_names(names, candidates)
    if hit:
        return hit
    for subdir in _subdirs(folder):
        if subdir.name.lower() in ("trailer", "trailers"):
            for entry in _dir_listing(subdir):
                if entry.is_file() and is_video_file(entry):
                    return entry
    return None


def find_subtitles(folder: Path, video_basename: str) -> list[Path]:
    result: list[Path] = []
    search_dirs = [folder]
    for subdir in _subdirs(folder):
        if subdir.name.lower() in ("subs", "subtitles", "untertitel"):
            search_dirs.append(subdir)
    stem = video_basename.lower()
    for directory in search_dirs:
        for entry in _dir_listing(directory):
            if not entry.is_file() or entry.suffix.lower() not in SUBTITLE_EXTENSIONS:
                continue
            name = entry.name.lower()
            suffix = Path(entry.name).suffix.lower()
            if (name == f"{stem}{suffix}" or name.startswith(f"{stem}.")) and entry not in result:
                result.append(entry)
    return result


# ===========================================================================
# 5. Scanner (Filme & Serien, inkrementell, Auto-Klassifikation)
# ===========================================================================

YearSuffixRe = re.compile(r"\s*\((\d{4})\)\s*$")


@dataclass
class ScanResult:
    scanned: int = 0
    updated: int = 0
    unchanged: int = 0
    missing: int = 0
    warnings: list[str] = field(default_factory=list)


def clean_title(raw: str) -> str:
    title = YearSuffixRe.sub(" ", raw).strip()
    return title or raw.strip()


#: Generische Videonamen, die als Titel nichts aussagen — bei ihnen gilt im
#: Filmordner der Ordnername als Titel (übliche Konvention).
_GENERIC_VIDEO_STEMS = {
    "movie",
    "video",
    "film",
    "cd1",
    "cd2",
    "cd3",
    "disc1",
    "disc2",
    "dvd",
    "index",
    "bdmv",
    "avchd",
    "title",
    "track1",
    "track2",
}


def _safe_mtime(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime)
    except OSError as exc:
        logger.warning("mtime nicht lesbar: %s (%s)", path, exc)
        return None


def _apply_nfo(movie: Movie, nfo: NfoData) -> None:
    if nfo.title:
        movie.title = nfo.title
    if nfo.sorttitle:
        movie.sort_title = nfo.sorttitle
    if nfo.plot:
        movie.plot = nfo.plot
    elif nfo.outline:
        movie.plot = nfo.outline
    if nfo.year is not None:
        movie.year = nfo.year
    if nfo.rating is not None:
        movie.rating = nfo.rating
    if nfo.runtime_min is not None:
        movie.runtime_min = nfo.runtime_min
    if nfo.genres:
        movie.genres = nfo.genres
    if nfo.studio:
        movie.studio = nfo.studio
    if nfo.directors:
        movie.director = ", ".join(nfo.directors)
    if nfo.trailer:
        movie.trailer_path = nfo.trailer


def build_movie(video: Path, folder: Path, title_from_folder: bool = False) -> Movie:
    basename = video.stem
    # Ohne NFO: Titel aus dem DATEINAMEN (bereinigt). Nur wenn der Videoname
    # generisch ist (movie.mkv, cd1.mkv …) und der Film in einem eigenen Ordner
    # liegt, gilt der Ordnername als Titel (übliche Konvention).
    generic_stem = video.stem.strip().casefold() in _GENERIC_VIDEO_STEMS
    if generic_stem and (title_from_folder or folder != video.parent):
        fallback_title = folder.name
    else:
        fallback_title = basename
    movie = Movie(
        title=clean_title(fallback_title),
        video_path=str(video),
        folder_path=str(folder),
        mtime=_safe_mtime(video),
    )
    # Jahr aus dem Fallback-Namen übernehmen („… (1999)“), falls kein NFO es setzt.
    year_match = YearSuffixRe.search(fallback_title)
    if year_match:
        movie.year = int(year_match.group(1))
    nfo_path = find_nfo(video.parent, basename, "movie")
    if nfo_path:
        movie.nfo_path = str(nfo_path)
        nfo = parse_nfo_file(nfo_path)
        if nfo:
            _apply_nfo(movie, nfo)
        else:
            logger.warning("Defekte NFO ignoriert (Dateinamen-Fallback): %s", nfo_path)
    poster = find_poster(video.parent, basename, folder_name=folder.name)
    if poster:
        movie.poster_path = str(poster)
    fanart = find_fanart(video.parent)
    if fanart:
        movie.fanart_path = str(fanart)
    trailer = find_trailer(video.parent, basename)
    if trailer:
        movie.trailer_path = str(trailer)
    movie.sort_title = movie.sort_title or movie.title
    return movie


def rescan_single_movie(conn: sqlite3.Connection, movie: Movie) -> Movie:
    video = Path(movie.video_path)
    folder = Path(movie.folder_path)
    title_from_folder = movie.title != clean_title(video.stem)
    fresh = build_movie(video, folder, title_from_folder=title_from_folder)
    if not fresh.mtime:
        fresh.mtime = movie.mtime
    fresh.category = movie.category
    return db_upsert_movie(conn, fresh)


def rescan_single_show(conn: sqlite3.Connection, show: Show) -> Show | None:
    """Serienordner neu einlesen (tvshow.nfo, Staffeln, Episoden) — inkrementell."""
    folder = Path(show.path)
    if not folder.is_dir():
        logger.warning("Metadaten neu einlesen: Serienordner fehlt: %s", folder)
        return None
    return scan_show(conn, folder, ScanResult())


def _videos_in_folder(folder: Path) -> list[Path]:
    videos: list[Path] = []
    try:
        for entry in Path(folder).rglob("*"):
            if entry.is_file() and is_video_file(entry):
                videos.append(entry)
    except OSError as exc:
        logger.warning("Ordner nicht lesbar: %s (%s)", folder, exc)
    return videos


def looks_like_show(folder: Path) -> bool:
    """Serien-Erkennung an der Ordnerstruktur (gängige Konventionen).

    True, wenn der Ordner tvshow.nfo enthält, Staffel-Unterordner hat
    (S01, Season 01, Staffel 1 …) oder Videodateien mit SxxEyy-Namen.
    """
    try:
        names_lower = {entry.name.lower() for entry in Path(folder).iterdir()}
    except OSError:
        return False
    if "tvshow.nfo" in names_lower:
        return True
    try:
        for entry in Path(folder).iterdir():
            if entry.is_dir() and is_season_folder(entry.name) is not None:
                return True
            if entry.is_file() and is_video_file(entry) and SXE_RE.search(entry.stem):
                return True
    except OSError:
        return False
    return False


def _scan_single_movie(
    conn: sqlite3.Connection,
    video: Path,
    folder: Path,
    result: ScanResult,
    title_from_folder: bool,
    category: str = "movies",
) -> None:
    """Einen Film inkrementell einlesen; unveränderte werden übersprungen."""
    result.scanned += 1
    mtime = _safe_mtime(video)
    existing = db_get_movie_by_path(conn, str(video))
    if existing and existing.mtime == mtime and not existing.missing:
        result.unchanged += 1
        return
    try:
        movie = build_movie(video, folder, title_from_folder=title_from_folder)
    except OSError as exc:
        message = f"Film übersprungen (IO-Fehler): {video} ({exc})"
        logger.warning(message)
        result.warnings.append(message)
        return
    movie.category = category or "movies"
    db_upsert_movie(conn, movie)
    result.updated += 1


def _prune_movies(conn: sqlite3.Connection, root: Path, result: ScanResult) -> None:
    """Verschwundene Filme aus der Bibliothek entfernen (Dateien bleiben unberührt);
    ebenso Film-Einträge, deren Ordner inzwischen als Serie erkannt wird
    (Datenreste aus alten Versionen ohne Auto-Klassifikation)."""
    for movie in db_list_movies(conn):
        path = Path(movie.video_path)
        if not _is_under(path, root):
            continue
        stale = not path.exists() or (path.parent != root and looks_like_show(path.parent))
        if stale:
            db_delete_movie(conn, movie.id)
            result.missing += 1


def scan_movie_library(
    conn: sqlite3.Connection,
    library: dict,
    progress=None,
    cancel_check=None,
) -> ScanResult:
    result = ScanResult()
    root = Path(library.get("path", ""))
    category = normalize_library_type(library.get("type", "movies"))
    if not root.is_dir():
        message = f"Bibliotheksordner fehlt: {root}"
        logger.warning(message)
        result.warnings.append(message)
        return result

    try:
        children = sorted(root.iterdir())
    except OSError as exc:
        logger.warning("Bibliotheksordner nicht lesbar: %s (%s)", root, exc)
        return result

    total = len(children)
    for index, entry in enumerate(children):
        if cancel_check and cancel_check():
            logger.info("Film-Scan abgebrochen bei %s", entry)
            break
        if progress:
            progress(index, total, entry.name)
        try:
            if entry.is_file():
                if not is_video_file(entry):
                    continue
                _scan_single_movie(conn, entry, root, result, title_from_folder=False, category=category)
            elif entry.is_dir():
                if looks_like_show(entry):
                    # Serie im Film-Ordner → in die Serien-Tabellen einsortieren.
                    scan_show(conn, entry, result)
                    continue
                videos = _videos_in_folder(entry)
                if not videos:
                    continue
                main = max(videos, key=lambda p: p.stat().st_size)
                _scan_single_movie(conn, main, entry, result, title_from_folder=True, category=category)
        except OSError as exc:
            logger.warning("Übersprungen: %s (%s)", entry, exc)

    _prune_movies(conn, root, result)

    if progress:
        progress(total, total, "")
    return result


def match_episode_numbers(filename: str) -> tuple[int, int] | None:
    match = SXE_RE.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = NXN_RE.search(filename)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def build_show(folder: Path) -> Show:
    show = Show(title=folder.name, path=str(folder))
    nfo_path = find_nfo(folder, folder.name, "tvshow")
    if nfo_path:
        show.nfo_path = str(nfo_path)
        nfo = parse_nfo_file(nfo_path)
        if nfo:
            if nfo.title:
                show.title = nfo.title
            if nfo.plot:
                show.plot = nfo.plot
            if nfo.year is not None:
                show.year = nfo.year
            if nfo.rating is not None:
                show.rating = nfo.rating
            if nfo.genres:
                show.genres = nfo.genres
            if nfo.studio:
                show.studio = nfo.studio
            # <thumb> ist bei gescrapten NFOs oft eine URL — nur echte lokale
            # Datei übernehmen, sonst bleibt das Serienposter dauerhaft leer.
            if nfo.thumb and Path(nfo.thumb).is_file():
                show.poster_path = nfo.thumb
            if nfo.fanart and Path(nfo.fanart).is_file():
                show.fanart_path = nfo.fanart
    if not show.poster_path:
        poster = find_poster(folder, folder_name=folder.name)
        if poster:
            show.poster_path = str(poster)
    if not show.fanart_path:
        fanart = find_fanart(folder)
        if fanart:
            show.fanart_path = str(fanart)
    banner = find_banner(folder)
    if banner:
        show.banner_path = str(banner)
    return show


def _collect_episodes(show_folder: Path) -> dict[int, list[tuple[Path, int, int]]]:
    """Episoden je Staffelnummer — SxxEyy/NxNN, sonst Staffel-Ordner + Fallback.

    1. SxxEyy / NxNN im Dateinamen.
    2. Fallback: Staffelnummer aus dem Staffel-Ordner (S01/Season 01/Staffel 1)
       + Episodennummer aus dem Dateinamen (E02, „02 - Titel", 002).
    """
    by_season: dict[int, list[tuple[Path, int, int]]] = {}
    try:
        walk = sorted(show_folder.rglob("*"))
    except OSError as exc:
        logger.warning("Show-Ordner nicht lesbar: %s (%s)", show_folder, exc)
        return by_season
    for entry in walk:
        if not entry.is_file() or not is_video_file(entry):
            continue
        numbers = match_episode_numbers(entry.stem)
        if numbers is None:
            season_no = is_season_folder(entry.parent.name)
            episode_no = fallback_episode_number(entry.stem)
            if season_no is None or episode_no is None:
                logger.info("Keine Episodennummer erkannt, übersprungen: %s", entry)
                continue
            numbers = (season_no, episode_no)
        by_season.setdefault(numbers[0], []).append((entry, numbers[0], numbers[1]))
    return by_season


def _build_episode(show: Show, video: Path, season_no: int) -> Episode:
    basename = video.stem
    episode = Episode(
        show_id=show.id,
        season=season_no,
        episode=0,
        title=basename,
        video_path=str(video),
        mtime=_safe_mtime(video),
    )
    nfo_path = find_nfo(video.parent, basename, "episode")
    if nfo_path:
        episode.nfo_path = str(nfo_path)
        nfo = parse_nfo_file(nfo_path)
        if nfo:
            if nfo.title:
                episode.title = nfo.title
            if nfo.plot:
                episode.plot = nfo.plot
            if nfo.rating is not None:
                episode.rating = nfo.rating
    still = find_still(video.parent, basename)
    if still:
        episode.still_path = str(still)
    return episode


def scan_show(conn: sqlite3.Connection, show_folder: Path, result: ScanResult) -> Show | None:
    show = build_show(show_folder)
    show.missing = False
    show = db_upsert_show(conn, show)

    by_season = _collect_episodes(show_folder)
    if not by_season:
        # Kein SxxEyy/NxNN und kein Staffel-Ordner-Fallback greifbar (z. B.
        # Einzelfile-Doku direkt im Serienordner): Videos NICHT verwerfen, sondern
        # als Staffel 1, Folge 1, 2, … in Namens-Reihenfolge aufnehmen.
        videos = _videos_in_folder(show_folder)
        for index, video in enumerate(sorted(videos, key=lambda p: p.name.casefold()), start=1):
            by_season.setdefault(1, []).append((video, 1, index))
        if by_season:
            logger.info("Keine Episodennummern — Videos als Staffel 1 aufgenommen: %s", show_folder)
    seasons: list[Season] = []
    for season_no in sorted(by_season):
        season_folder = min(by_season[season_no], key=lambda item: len(item[0].parts))[0].parent
        poster = find_season_poster(show_folder, season_folder, season_no)
        seasons.append(Season(show_id=show.id, number=season_no, poster_path=str(poster) if poster else None))
    computed = [(s.number, s.poster_path) for s in seasons]
    if computed != [(s.number, s.poster_path) for s in db_list_seasons(conn, show.id)]:
        db_replace_seasons(conn, show.id, seasons)

    for season_no, entries in sorted(by_season.items()):
        for video, _entry_season, entry_episode in sorted(entries, key=lambda item: item[0].name):
            mtime = _safe_mtime(video)
            existing = db_get_episode_by_path(conn, str(video))
            if existing and existing.mtime == mtime and not existing.missing:
                result.unchanged += 1
                continue
            try:
                episode = _build_episode(show, video, season_no)
            except OSError as exc:
                message = f"Episode übersprungen (IO-Fehler): {video} ({exc})"
                logger.warning(message)
                result.warnings.append(message)
                continue
            numbers = match_episode_numbers(video.stem)
            if numbers:
                episode.season, episode.episode = numbers
            else:
                # Nummer aus dem Sammel-Tupel (Staffel-Ordner-Fallback oder
                # Einzelfile-Fallback S01E01…) — Dateiname hat hier keine Nummer.
                episode.episode = fallback_episode_number(video.stem) or entry_episode
                episode.season = season_no
            db_upsert_episode(conn, episode)
            result.updated += 1
        result.scanned += len(entries)

    for episode in db_list_episodes(conn, show.id):
        if not Path(episode.video_path).exists():
            db_delete_episode(conn, episode.id)
            result.missing += 1
    return show


def scan_series_library(
    conn: sqlite3.Connection,
    library: dict,
    progress=None,
    cancel_check=None,
) -> ScanResult:
    result = ScanResult()
    root = Path(library.get("path", ""))
    if not root.is_dir():
        message = f"Bibliotheksordner fehlt: {root}"
        logger.warning(message)
        result.warnings.append(message)
        return result
    try:
        show_folders = sorted(entry for entry in root.iterdir() if entry.is_dir())
    except OSError as exc:
        logger.warning("Bibliotheksordner nicht lesbar: %s (%s)", root, exc)
        return result

    total = len(show_folders)
    for index, folder in enumerate(show_folders):
        if cancel_check and cancel_check():
            logger.info("Serien-Scan abgebrochen bei %s", folder)
            break
        if not looks_like_show(folder):
            # Kein tvshow.nfo, keine Staffel-Ordner, keine SxxEyy-Dateien:
            # kein Serienordner (verhindert Müll-Einträge für Film-Unterordner).
            logger.info("Kein Serienordner, übersprungen: %s", folder)
            continue
        if progress:
            progress(index, total, folder.name)
        try:
            scan_show(conn, folder, result)
        except OSError as exc:
            message = f"Show übersprungen (IO-Fehler): {folder} ({exc})"
            logger.warning(message)
            result.warnings.append(message)

    # Verschwundene Serien aus der Bibliothek entfernen; ebenso Film-Einträge
    # unter diesem Wurzelordner, deren Ordner als Serie erkannt wird
    # (Datenreste aus alten Versionen ohne Auto-Klassifikation).
    for show in db_list_shows(conn):
        show_path = Path(show.path)
        if not _is_under(show_path, root):
            continue
        if not show_path.exists():
            db_delete_show(conn, show.id)
            result.missing += 1
    for movie in db_list_movies(conn):
        if not _is_under(movie.video_path, root):
            continue
        if looks_like_show(Path(movie.video_path).parent):
            db_delete_movie(conn, movie.id)
            result.missing += 1

    if progress:
        progress(total, total, "")
    return result


# ===========================================================================
# 6. Player-Start & Playlisten
# ===========================================================================


class PlayerNotFoundError(Exception):
    pass


def _path_tokens(path: str) -> list[str]:
    if os.name == "nt":
        # Windows: Backslashes (C:\Program Files\…) sind keine Escape-Zeichen —
        # der POSIX-Modus von shlex würde sie verschlucken. Anführungszeichen
        # (bei Pfaden mit Leerzeichen nötig) manuell abziehen.
        try:
            return [tok.strip('"') for tok in shlex.split(path, posix=False)]
        except ValueError:
            return path.split()
    try:
        return shlex.split(path)
    except ValueError:
        return path.split()


def _player_path_tokens(path: str) -> list[str]:
    """Player-Pfad als EIN Token, wenn er als Datei existiert (C:\\Program Files\\…).

    Nur splitten, wenn der Eintrag eine Befehlszeile ist (z. B. "flatpak run
    org.videolan.VLC"). Zuvor wurde immer gesplittet — Pfade mit Leerzeichen
    zerbrachen (C:\\Program …) und kein Player startete mehr.
    """
    raw = path.strip().strip('"')
    if raw and Path(raw).exists():
        return [raw]
    return _path_tokens(path)


def _player_binary_exists(player: dict) -> bool:
    tokens = _player_path_tokens(str(player.get("path", "")))
    if not tokens:
        return False
    first = tokens[0]
    if len(tokens) > 1:
        # Befehl mit Argumenten (z. B. "flatpak run …"): nur über den PATH findbar
        return shutil_which(first) is not None
    return Path(first).exists() or shutil_which(first) is not None


def build_command(player: dict, video, subtitle=None, fullscreen: bool | None = None) -> list[str]:
    cmd = _player_path_tokens(str(player.get("path", "")))
    if not cmd:
        raise PlayerNotFoundError("Player ohne Pfad konfiguriert")
    args = [str(a) for a in player.get("args", ["{file}"])]
    built = []
    for arg in args:
        if subtitle is not None and "{subtitle}" in arg:
            arg = arg.replace("{subtitle}", str(subtitle))
        built.append(arg.replace("{file}", str(video)))
    if subtitle is not None and not any("{subtitle}" in a for a in args):
        if "mpv" in str(player.get("name", "")).lower():
            built.append(f"--sub-file={subtitle}")
        else:
            built.extend(["--sub-file", str(subtitle)])
    if fullscreen and not any("fullscreen" in a.lower() for a in built):
        built.append("--fullscreen")
    return cmd + built


def launch_player(player: dict, video, subtitle=None, fullscreen: bool | None = None) -> subprocess.Popen:
    if not _player_binary_exists(player):
        raise PlayerNotFoundError(f"Player nicht gefunden: {player.get('path', '(leer)')}")
    cmd = build_command(player, video, subtitle, fullscreen)
    logger.info("Starte Player: %s", " ".join(cmd))
    try:
        return subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as exc:
        logger.error("Player konnte nicht gestartet werden: %s (%s)", cmd, exc)
        raise


FOLDER_OPENERS = ("dolphin", "nautilus", "thunar", "pcmanfm", "nemo", "caja")


def _system_open_windows(target: str) -> tuple[bool, str]:
    """Windows-Öffner: os.startfile (Shell-Assoziation, funktioniert für Ordner
    und URLs unter XP bis Windows 11), Fallback explorer.exe. os.startfile
    meldet keinen Exitcode — Fehler (keine Zuordnung, Pfad weg) kommen als
    OSError und werden protokolliert statt still verschluckt (AGENTS § 9)."""
    last_error = "Kein Öffner gefunden"
    low = str(target).lower()
    if not low.startswith(("http://", "https://")) and not os.path.exists(target):
        # startfile/explorer mit nicht vorhandenem Ziel zeigt nur einen
        # verwirrenden Windows-Fehlerdialog — lieber hier sauber abbrechen.
        last_error = f"Ziel nicht gefunden: {target}"
        logger.warning("Öffnen nicht möglich (%s)", last_error)
        return False, last_error
    try:
        logger.info("Öffne per os.startfile: %s", target)
        os.startfile(str(target))  # Ziel stammt aus der eigenen Bibliothek
        return True, ""
    except OSError as exc:
        last_error = f"os.startfile: {exc}"
        logger.warning("Öffnen fehlgeschlagen (%s): %s", last_error, target)
    explorer = shutil_which("explorer")
    if explorer:
        logger.info("Öffne mit explorer.exe: %s", target)
        try:
            subprocess.Popen(
                [explorer, str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, ""
        except OSError as exc:
            last_error = f"explorer.exe: {exc}"
            logger.warning("Öffner nicht startbar: %s", last_error)
    return False, last_error


def system_open(target: str, folder_fallbacks: bool = False) -> tuple[bool, str]:
    """Ordner/URL über den System-Öffner starten und das Ergebnis PRÜFEN.

    xdg-open bleibt erste Wahl, meldet aber ohne gesetzten Standard-Handler
    (z. B. minimale Arch-Setups) nur einen Fehler-Exitcode — deshalb wird der
    ausgewertet und gefallbackt: `gio open`, dann erkannte Dateimanager.
    Rückgabe: (erfolgreich?, Meldung des letzten Fehlversuchs).
    """
    if os.name == "nt":
        return _system_open_windows(target)
    candidates: list[list[str]] = []
    xdg = shutil_which("xdg-open")
    if xdg:
        candidates.append([xdg, str(target)])
    gio = shutil_which("gio")
    if gio:
        candidates.append([gio, "open", str(target)])
    if folder_fallbacks:
        for name in FOLDER_OPENERS:
            binary = shutil_which(name)
            if binary:
                candidates.append([binary, str(target)])
    last_error = "Kein Öffner gefunden — bitte xdg-utils installieren (z. B. sudo pacman -S xdg-utils)"
    for args in candidates:
        logger.info("Öffne mit %s: %s", Path(args[0]).name, target)
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            last_error = f"{Path(args[0]).name}: {exc}"
            logger.warning("Öffner nicht startbar: %s", last_error)
            continue
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Opener startet die Ziel-App im Vordergrund — als Erfolg werten.
            logger.info("Öffner läuft im Vordergrund weiter: %s", args[0])
            return True, ""
        if proc.returncode == 0:
            return True, ""
        last_error = f"{Path(args[0]).name} meldete Fehlercode {proc.returncode}"
        logger.warning("Öffnen fehlgeschlagen (%s): %s", last_error, target)
    return False, last_error


def pl_resolve_item(conn: sqlite3.Connection, item: PlaylistItem) -> tuple[str, str]:
    if item.ref_type == "movie" and item.ref_id is not None:
        row = conn.execute("SELECT * FROM movies WHERE id = ?", (item.ref_id,)).fetchone()
        if row:
            return row["video_path"] or "", row["title"] or ""
    elif item.ref_type == "episode" and item.ref_id is not None:
        row = conn.execute("SELECT * FROM episodes WHERE id = ?", (item.ref_id,)).fetchone()
        if row:
            title = f"S{row['season']:02d}E{row['episode']:02d} · {row['title'] or ''}".strip(" ·")
            return row["video_path"] or "", title
    if item.custom_path:
        return item.custom_path, Path(item.custom_path).name
    return "", ""


def pl_move_item(conn: sqlite3.Connection, item: PlaylistItem, direction: int) -> bool:
    items = db_list_playlist_items(conn, item.playlist_id)
    positions = [existing.id for existing in items]
    if item.id not in positions:
        return False
    index = positions.index(item.id)
    target = index + direction
    if target < 0 or target >= len(positions):
        return False
    positions[index], positions[target] = positions[target], positions[index]
    db_reorder_playlist_items(conn, positions)
    return True


def pl_entries(conn: sqlite3.Connection, playlist: Playlist) -> list[tuple[str, str]]:
    """Aufgelöste, absolute Einträge (Pfad, Titel) einer Playlist."""
    entries: list[tuple[str, str]] = []
    for item in db_list_playlist_items(conn, playlist.id):
        video_path, title = pl_resolve_item(conn, item)
        if not video_path:
            logger.warning("Playlist %s: Eintrag ohne Pfad übersprungen", playlist.name)
            continue
        entries.append((str(Path(video_path).resolve()), title))
    return entries


def _write_playlist_file(target: Path, data: str | bytes) -> None:
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        target.write_bytes(data)
    else:
        target.write_text(data, encoding="utf-8")


def pl_export_m3u8(conn: sqlite3.Connection, playlist: Playlist, target: Path) -> int:
    # NUR einfache Wiedergabeliste — VLC behandelt EXT-X-Dateien als HLS-Streams
    # und spielt die lokalen Dateien dann nicht ab.
    entries = pl_entries(conn, playlist)
    lines = ["#EXTM3U"]
    for video_path, title in entries:
        lines.append(f"#EXTINF:0,{title}")
        lines.append(video_path)
    _write_playlist_file(target, "\n".join(lines) + "\n")
    logger.info("Playlist %s exportiert (M3U8): %s (%d Einträge)", playlist.name, target, len(entries))
    return len(entries)


def pl_export_m3u(conn: sqlite3.Connection, playlist: Playlist, target: Path) -> int:
    """Klassisches M3U: nur Pfade, UTF-8."""
    entries = pl_entries(conn, playlist)
    text = "\n".join(video_path for video_path, _title in entries)
    if entries:
        text += "\n"
    _write_playlist_file(target, text)
    logger.info("Playlist %s exportiert (M3U): %s (%d Einträge)", playlist.name, target, len(entries))
    return len(entries)


def pl_export_xspf(conn: sqlite3.Connection, playlist: Playlist, target: Path) -> int:
    """XSPF v1 (XML, VLC-kompatibel) mit file://-URIs."""
    entries = pl_entries(conn, playlist)
    playlist_el = ET.Element("playlist", {"xmlns": "http://xspf.org/ns/0/", "version": "1"})
    ET.SubElement(playlist_el, "title").text = playlist.name
    track_list = ET.SubElement(playlist_el, "trackList")
    for video_path, title in entries:
        track = ET.SubElement(track_list, "track")
        ET.SubElement(track, "location").text = Path(video_path).as_uri()
        if title:
            ET.SubElement(track, "title").text = title
    ET.indent(playlist_el)
    _write_playlist_file(target, ET.tostring(playlist_el, encoding="UTF-8", xml_declaration=True))
    logger.info("Playlist %s exportiert (XSPF): %s (%d Einträge)", playlist.name, target, len(entries))
    return len(entries)


def pl_import_m3u8(conn: sqlite3.Connection, name: str, source: Path) -> tuple[Playlist, int, int]:
    source = Path(source)
    playlist = db_create_playlist(conn, name)
    imported = skipped = 0
    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        logger.error("M3U nicht lesbar: %s (%s)", source, exc)
        raise
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        path = Path(line)
        if not path.is_absolute():
            path = source.parent / path
        if not path.exists():
            logger.warning("Playlist-Import: Datei fehlt, übersprungen: %s", path)
            skipped += 1
            continue
        entry = db_get_movie_by_path(conn, str(path))
        if entry:
            db_add_playlist_item(conn, playlist.id, "movie", entry.id, None)
        else:
            episode = db_get_episode_by_path(conn, str(path))
            if episode:
                db_add_playlist_item(conn, playlist.id, "episode", episode.id, None)
            else:
                db_add_playlist_item(conn, playlist.id, "custom_path", None, str(path))
        imported += 1
    logger.info("Playlist %s importiert: %d Einträge, %d übersprungen", name, imported, skipped)
    return playlist, imported, skipped


def pl_write_temp_playlist(conn: sqlite3.Connection, playlist: Playlist, temp_dir: Path) -> Path:
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in playlist.name)
    target = Path(temp_dir) / f"{safe_name or 'playlist'}.m3u8"
    pl_export_m3u8(conn, playlist, target)
    return target


# ===========================================================================
# 7. GUI (PySide6, Windows 7/8: PyQt5-Fallback)
# ===========================================================================

try:  # Qt 6 (PySide6) — Standard auf Linux und Windows 10/11
    from PySide6.QtCore import (
        QByteArray,
        QModelIndex,
        QObject,
        QPoint,
        QRect,
        QSize,
        QSortFilterProxyModel,
        Qt,
        QThread,
        Signal,
    )
    from PySide6.QtGui import (
        QBrush,
        QColor,
        QCursor,
        QIcon,
        QKeySequence,
        QPainter,
        QPixmap,
        QShortcut,
        QStandardItem,
        QStandardItemModel,
    )
    from PySide6.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QKeySequenceEdit,
        QLabel,
        QLayout,
        QLayoutItem,
        QLineEdit,
        QListView,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressDialog,
        QPushButton,
        QScrollArea,
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    _IS_QT5 = False
except ImportError:  # Windows 7/8.1: Qt 6 läuft dort nicht → PyQt5 (Qt 5.15)
    from PyQt5.QtCore import (
        QByteArray,
        QModelIndex,
        QObject,
        QPoint,
        QRect,
        QSize,
        QSortFilterProxyModel,
        Qt,
        QThread,
    )
    from PyQt5.QtCore import pyqtSignal as Signal
    from PyQt5.QtGui import (
        QBrush,
        QColor,
        QCursor,
        QIcon,
        QKeySequence,
        QPainter,
        QPixmap,
        QStandardItem,
        QStandardItemModel,
    )
    from PyQt5.QtWidgets import (
        QAbstractItemView,
        QApplication,
        QCheckBox,
        QComboBox,
        QDialog,
        QFileDialog,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QKeySequenceEdit,
        QLabel,
        QLayout,
        QLayoutItem,
        QLineEdit,
        QListView,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMenu,
        QMessageBox,
        QProgressDialog,
        QPushButton,
        QScrollArea,
        QShortcut,  # Qt 5: QShortcut liegt in QtWidgets, nicht in QtGui
        QSplitter,
        QStackedWidget,
        QTableWidget,
        QTableWidgetItem,
        QTabWidget,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    _IS_QT5 = True

    # Qt 5 nennt die Event-Loop-Aufrufe exec_() — Qt-6-Stil exec() nachrüsten,
    # damit der restliche Code binding-übergreifend identisch bleibt.
    if not hasattr(QApplication, "exec"):
        QApplication.exec = QApplication.exec_  # type: ignore[attr-defined]
    if not hasattr(QMenu, "exec"):
        QMenu.exec = QMenu.exec_  # type: ignore[attr-defined]
    if not hasattr(QDialog, "exec"):
        QDialog.exec = QDialog.exec_  # type: ignore[attr-defined]
    if not hasattr(QMessageBox, "exec"):
        QMessageBox.exec = QMessageBox.exec_  # type: ignore[attr-defined]

THEMES: dict[str, dict[str, str]] = {
    "darkskin": {
        "bg_window": "#1B1B1D",
        "bg_panel": "#222226",
        "bg_elevated": "#2A2A2F",
        "bg_input": "#26262B",
        "text_primary": "#A8E6A1",
        "text_secondary": "#86C286",
        "text_disabled": "#5C7A5C",
        "accent": "#7ED67E",
        "accent_strong": "#55B855",
        "selection_bg": "rgba(126, 214, 126, 0.18)",
        "border": "#34343A",
        "border_focus": "#7ED67E",
        "scrollbar_track": "#26262B",
        "scrollbar_handle": "#55555C",
        "danger": "#D96A6A",
        "poster_placeholder_bg": "#242428",
        "overlay_fanart": "rgba(15, 15, 16, 0.72)",
    },
    "bluemoon": {
        "bg_window": "#0D1B2A",
        "bg_panel": "#12263A",
        "bg_elevated": "#173049",
        "bg_input": "#132C44",
        "text_primary": "#FFEC9E",
        "text_secondary": "#D9C77E",
        "text_disabled": "#8A8160",
        "accent": "#FFD75E",
        "accent_strong": "#F2C230",
        "selection_bg": "rgba(255, 215, 94, 0.16)",
        "border": "#1F3A57",
        "border_focus": "#FFD75E",
        "scrollbar_track": "#12263A",
        "scrollbar_handle": "#2C5480",
        "danger": "#E08080",
        "poster_placeholder_bg": "#10233A",
        "overlay_fanart": "rgba(6, 14, 24, 0.72)",
    },
    "creamy": {
        "bg_window": "#CBB78E",
        "bg_panel": "#DECCA8",
        "bg_elevated": "#EBDFC4",
        "bg_input": "#E6D8B6",
        "text_primary": "#1A1A1A",
        "text_secondary": "#6B5C42",
        "text_disabled": "#9A8C73",
        "accent": "#A9741F",
        "accent_strong": "#8F621A",
        "selection_bg": "rgba(169, 116, 31, 0.22)",
        "border": "#B7A67E",
        "border_focus": "#A9741F",
        "scrollbar_track": "#E6D8B6",
        "scrollbar_handle": "#B3A074",
        "danger": "#A83B32",
        "poster_placeholder_bg": "#E2D2AC",
        "overlay_fanart": "rgba(40, 32, 20, 0.72)",
    },
}
DEFAULT_THEME = "darkskin"

QSS_TEMPLATE = """/* Tokens werden per Ersetzung eingesetzt */
QMainWindow, QWidget { background-color: {{bg_window}}; color: {{text_primary}}; }
QDialog              { background-color: {{bg_elevated}}; }
QLabel               { color: {{text_primary}}; background: transparent; }
QLabel#secondary     { color: {{text_secondary}}; }
QPushButton {
  background-color: {{bg_elevated}}; color: {{text_primary}};
  border: 1px solid {{border}}; border-radius: 4px; padding: 6px 14px;
}
QPushButton:hover    { border: 1px solid {{border_focus}}; }
QPushButton:focus    { border: 2px solid {{border_focus}}; }
QPushButton:pressed  { background-color: {{bg_input}}; }
QPushButton:disabled { color: {{text_disabled}}; border: 1px solid {{border}}; }
QPushButton#primary  { background-color: {{accent_strong}}; color: {{bg_window}}; font-weight: bold; }
QPushButton#danger   { color: {{danger}}; border: 1px solid {{danger}}; }
QLineEdit, QComboBox, QSpinBox {
  background-color: {{bg_input}}; color: {{text_primary}};
  border: 1px solid {{border}}; border-radius: 4px; padding: 4px 8px;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid {{border_focus}}; }
QComboBox QAbstractItemView {
  background-color: {{bg_elevated}}; color: {{text_primary}};
  selection-background-color: {{selection_bg}};
}
QMenu { background-color: {{bg_elevated}}; border: 1px solid {{border}}; border-radius: 4px; }
QMenu::item           { padding: 6px 16px; color: {{text_primary}}; }
QMenu::item:selected  { background-color: {{selection_bg}}; }
QMenu::item:disabled  { color: {{text_disabled}}; }
QMenu::separator      { height: 1px; background: {{border}}; margin: 4px 8px; }
QScrollBar:vertical { background: {{scrollbar_track}}; border: 1px solid {{border}}; border-radius: 5px; width: 12px; }
QScrollBar::handle:vertical { background: {{text_primary}}; border-radius: 5px; min-height: 30px; }
QScrollBar::handle:vertical:hover { background: {{accent_strong}}; }
QScrollBar:horizontal { background: {{scrollbar_track}}; border: 1px solid {{border}}; border-radius: 5px; height: 12px; }
QScrollBar::handle:horizontal { background: {{text_primary}}; border-radius: 5px; min-width: 30px; }
QScrollBar::handle:horizontal:hover { background: {{accent_strong}}; }
QScrollBar::add-line, QScrollBar::sub-line { height: 0; width: 0; }
QProgressBar {
  background: {{bg_input}}; border: 1px solid {{border}}; border-radius: 4px;
  color: {{text_primary}}; text-align: center;
}
QProgressBar::chunk { background-color: {{accent_strong}}; border-radius: 3px; }
QListWidget, QTableWidget, QTreeWidget, QListView, QTreeView {
  background-color: {{bg_window}}; color: {{text_primary}};
  border: none; alternate-background-color: {{bg_panel}};
}
QListWidget::item, QListView::item { padding: 2px; }
QListWidget::item:selected, QListView::item:selected,
QTreeWidget::item:selected, QTableWidget::item:selected {
  background-color: {{selection_bg}}; color: {{text_primary}};
}
QListWidget::item:hover, QListView::item:hover { background-color: {{selection_bg}}; }
QHeaderView::section {
  background-color: {{bg_panel}}; color: {{text_secondary}};
  border: none; border-bottom: 1px solid {{border}}; padding: 4px 8px;
}
QTabWidget::pane { border: 1px solid {{border}}; border-radius: 4px; }
QTabBar::tab {
  background: {{bg_panel}}; color: {{text_secondary}};
  padding: 6px 14px; border: 1px solid {{border}};
  border-top-left-radius: 4px; border-top-right-radius: 4px;
}
QTabBar::tab:selected { color: {{text_primary}}; border-bottom: 2px solid {{accent}}; }
QToolTip { background-color: {{bg_elevated}}; color: {{text_primary}}; border: 1px solid {{border}}; }
"""


def get_theme(theme_name: str) -> dict[str, str]:
    return THEMES.get(theme_name, THEMES[DEFAULT_THEME])


def get_stylesheet(theme_name: str) -> str:
    tokens = get_theme(theme_name)
    qss = QSS_TEMPLATE
    for key, value in tokens.items():
        qss = qss.replace("{{" + key + "}}", value)
    return qss


# UI-Texte (deutsch)
class S:
    APP_TITLE = f"MediaCenter {APP_VERSION}"
    NAV_MOVIES, NAV_SHOWS, NAV_PLAYLISTS, NAV_SETTINGS = "Filme", "Serien", "Playlisten", "Einstellungen"
    NAV_THEME_BUTTON = "Theme: {}"
    STATUS_THEME_CHANGED = "Theme gewechselt: {}"
    SEARCH_PLACEHOLDER = "Suchen… (Titel, Genre, Jahr)"
    SORT_TITLE, SORT_YEAR = "Titel", "Jahr"
    FILTER_GENRE = "Genre"
    FILTER_GENRE_ALL = "Alle Genres"
    FILTER_GENRE_ENTRY = "{} ({})"
    FILTER_GENRE_LABEL = "Genre: {}"
    STATUS_GENRE_FILTER = "Genre-Filter: {}"
    VIEW_POSTERS, VIEW_LIST = "Posterwand", "Liste"
    RANDOM_PLAY = "Zufall"
    RANDOM_TOOLTIP = (
        "Spielt einen zufälligen Film bzw. (in der Serienansicht) eine zufällige "
        "Episode ab — bei aktivem Filter (Suche/Genre/Jahr) aus den gefilterten Einträgen."
    )
    STATUS_RANDOM_EMPTY = "Keine abspielbaren Einträge für die Zufallswiedergabe"
    MENU_PLAY = "Abspielen"
    MENU_TRAILER = "Trailer abspielen"
    MENU_DETAILS = "Details / Medieninfos…"
    MENU_ADD_TO_PLAYLIST = "Zur Playlist hinzufügen"
    MENU_NEW_PLAYLIST = "Neue Playlist…"
    MENU_OPEN_FOLDER = "Ordner öffnen"
    MENU_RESCAN = "Metadaten neu einlesen"
    MENU_REMOVE_FROM_PLAYLIST = "Aus Playlist entfernen"
    MENU_REMOVE_FROM_LIBRARY = "Aus Bibliothek entfernen"
    REMOVE_FROM_LIBRARY_CONFIRM = (
        "„{}“ aus der Bibliothek entfernen?\n\nDie Dateien auf der Festplatte bleiben unberührt."
    )
    STATUS_LIBRARY_REMOVED = "Aus Bibliothek entfernt: {}"
    STATUS_ORPHANS_REMOVED = "{} Einträge entfernt (Bibliothek nicht mehr konfiguriert)"
    STATUS_SHOW_ADDED_TO_PLAYLIST = "{} Episoden zur Playlist „{}“ hinzugefügt"
    STATUS_SHOW_NO_EPISODES = "Keine abspielbaren Episoden gefunden: {}"
    DETAILS_TITLE = "Medieninfos"
    DETAILS_YEAR, DETAILS_RATING, DETAILS_RUNTIME = "Jahr", "Bewertung", "Laufzeit"
    DETAILS_MINUTES = "{} min"
    DETAILS_GENRES, DETAILS_STUDIO, DETAILS_DIRECTOR, DETAILS_ACTORS = (
        "Genres",
        "Studio",
        "Regisseur",
        "Darsteller",
    )
    DETAILS_FILES, DETAILS_FILES_VIDEO = "Dateien", "Video"
    DETAILS_FILES_TRAILER, DETAILS_FILES_TRAILER_URL = "Trailer", "Trailer (Web)"
    DETAILS_FILES_NFO = "NFO"
    DETAILS_SEASONS_EPISODES = "Staffeln / Episoden"
    DETAILS_BUTTON_PLAY, DETAILS_BUTTON_TRAILER, DETAILS_BUTTON_CLOSE = "Abspielen", "Trailer", "Schließen"
    SERIES_BREADCRUMB_ROOT = "Serien"
    SERIES_SEASON_TILE = "Staffel {}"
    SERIES_EPISODES_TITLE = "Episoden"
    SERIES_NO_EPISODES = "Keine Episoden gefunden"
    SERIES_EPISODE_PREFIX = "S{:02d}E{:02d}"
    PLAYLIST_NEW, PLAYLIST_RENAME, PLAYLIST_DELETE = "Neu", "Umbenennen", "Löschen"
    PLAYLIST_DELETE_CONFIRM = "Playlist „{}“ wirklich löschen?"
    PLAYLIST_EMPTY_HINT = "Keine Playlist ausgewählt"
    PLAYLIST_PLAY = "Abspielen"
    PLAYLIST_EXPORT = "Exportieren"
    PLAYLIST_FORMAT_M3U8 = "M3U8 (*.m3u8)"
    PLAYLIST_FORMAT_M3U = "M3U (*.m3u)"
    PLAYLIST_FORMAT_XSPF = "XSPF (*.xspf)"
    PLAYLIST_IMPORT = "Importieren (M3U/M3U8)"
    PLAYLIST_NEW_NAME = "Name der neuen Playlist:"
    PLAYLIST_RENAME_TITLE = "Playlist umbenennen"
    SETTINGS_TITLE = "Einstellungen"
    SETTINGS_TAB_LIBRARIES = "Bibliotheken"
    SETTINGS_LIB_COUNT = "Anzahl"
    SETTINGS_SCAN_ON_START = "Bibliotheken bei jedem Start des MediaCenter einlesen"
    SETTINGS_TAB_PLAYERS = "Player"
    SETTINGS_TAB_PLAYBACK = "Wiedergabe"
    SETTINGS_TAB_VIEW = "Ansicht"
    SETTINGS_TAB_SHORTCUTS = "Tastenkürzel"
    SETTINGS_LIB_NAME, SETTINGS_LIB_TYPE, SETTINGS_LIB_PATH = "Name", "Typ", "Pfad"
    SETTINGS_LIB_TYPE_MOVIES, SETTINGS_LIB_TYPE_SHOWS = "Filme", "Serien"
    SETTINGS_LIB_TYPE_HINT = (
        "Typ: „Filme“ oder „Serien“ wählen — oder „Eigene Kategorie…“ wählen und "
        "einen Namen eingeben (z. B. Doku, Comedy). Für jede eigene Kategorie gibt "
        "es einen eigenen Eintrag in der Navigation."
    )
    SETTINGS_LIB_TYPE_CUSTOM = "Eigene Kategorie…"
    SETTINGS_LIB_TYPE_CUSTOM_TITLE = "Eigene Kategorie"
    SETTINGS_LIB_TYPE_CUSTOM_LABEL = "Bezeichnung des Typs eingeben (z. B. Doku, Comedy):"
    SETTINGS_LIB_TYPE_CUSTOM_DATA = "__custom__"
    SETTINGS_LIB_ADD, SETTINGS_LIB_REMOVE, SETTINGS_LIB_SCAN = "Hinzufügen", "Entfernen", "Neu scannen"
    SETTINGS_LIB_REMOVE_CONFIRM = (
        'Bibliothek "{}" entfernen?\n\n'
        "Ihre Einträge werden aus der Mediathek entfernt (beim Übernehmen oder"
        " nächsten Start). Die Dateien auf der Festplatte bleiben unberührt."
    )
    PLAYLIST_ITEM_REMOVE_CONFIRM = 'Eintrag "{}" aus der Playlist entfernen?'
    SETTINGS_LIB_PICK_TITLE = "Ordner für Bibliothek wählen"
    SETTINGS_PLAYER_NAME = "Name"
    SETTINGS_PLAYER_PATH = "Pfad/Befehl"
    SETTINGS_PLAYER_ARGS = "Argumente"
    SETTINGS_PLAYER_DEFAULT = "Standard"
    SETTINGS_PLAYER_ADD, SETTINGS_PLAYER_REMOVE = "Hinzufügen", "Entfernen"
    SETTINGS_PLAYER_AUTODETECT = "VLC/mpv automatisch finden"
    SETTINGS_PLAYER_AUTODETECT_DONE = "Player erkannt:\n{}"
    SETTINGS_PLAYER_AUTODETECT_NONE = "Kein Player automatisch gefunden.\nBitte Pfad manuell eintragen."
    SETTINGS_PLAYBACK_FULLSCREEN = "Filme und Serien im Vollbild öffnen"
    SETTINGS_PLAYBACK_YOUTUBE = "YouTube-Trailer öffnen in:"
    SETTINGS_YOUTUBE_BROWSER, SETTINGS_YOUTUBE_VLC = "Browser", "VLC"
    SETTINGS_VIEW_THEME = "Theme"
    SETTINGS_VIEW_POSTER_WIDTH = "Postergröße"
    #: 4 feste Stufen — die bisherige Größe (200) ist die größte.
    SETTINGS_POSTER_LEVELS = (("Klein", 120), ("Mittel", 150), ("Groß", 175), ("Sehr groß", 200))
    SETTINGS_VIEW_LABEL = "Titel unter dem Poster anzeigen"
    SETTINGS_SHORTCUT_ACTION, SETTINGS_SHORTCUT_KEY = "Aktion", "Taste"
    SETTINGS_SHORTCUT_HINT = "Doppelklick auf eine Zeile, um ein neues Tastenkürzel aufzunehmen."
    SETTINGS_SHORTCUT_RESET = "Alle Kürzel zurücksetzen"
    SETTINGS_SHORTCUT_CAPTURE_TITLE = "Neues Tastenkürzel"
    SETTINGS_SHORTCUT_CAPTURE_LABEL = "Neue Tastenkombination für „{}“:"
    SETTINGS_SHORTCUT_CONFLICT = (
        "Diese Taste ist bereits „{}“ zugewiesen.\nBitte eine andere Kombination wählen."
    )
    SETTINGS_OK, SETTINGS_CANCEL = "Übernehmen", "Abbrechen"
    SETTINGS_SAVED = "Einstellungen gespeichert"
    SCAN_RUNNING = "Scanne {}… ({} %)"
    SCAN_FINISHED = "Scan abgeschlossen: {} neu, {} unverändert, {} entfernt"
    COUNT_MOVIES = "{} Filme"
    COUNT_SHOWS = "{} Serien"
    COUNT_ITEMS = "{} Einträge"
    SCAN_CANCELLED = "Scan abgebrochen"
    SCAN_CANCEL = "Abbrechen"
    SCAN_ERROR_TITLE = "Scan-Fehler"
    SCAN_NO_LIBRARIES = (
        "Keine Bibliotheken konfiguriert.\nBitte zuerst in den Einstellungen einen Ordner hinzufügen."
    )
    SCAN_DIALOG_TITLE = "Bibliotheken scannen"
    STATUS_PLAYER_MISSING_TITLE = "Player nicht gefunden"
    STATUS_PLAYER_MISSING = "Der Player „{}“ wurde nicht gefunden.\nPfad prüfen oder anderen Player wählen."
    STATUS_FILE_MISSING_TITLE = "Datei fehlt"
    STATUS_FILE_MISSING = "Die Videodatei existiert nicht mehr:\n{}"
    STATUS_FOLDER_OPEN_FAILED = "Ordner konnte nicht geöffnet werden"
    STATUS_FOLDER_OPEN_HINT = (
        "Der Ordner konnte nicht geöffnet werden:\n{}\n\n"
        "Bitte einen Standard-Dateimanager festlegen oder xdg-utils installieren."
    )
    STATUS_FOLDER_MISSING = "Ordner nicht gefunden: {}"
    STATUS_ERROR = "Fehler: {}"
    PLACEHOLDER_NO_POSTER = "Kein Poster"

    @staticmethod
    def action_label(action_id: str) -> str:
        return {
            "play": "Abspielen (Enter)",
            "details": "Details / Medieninfos",
            "trailer": "Trailer abspielen",
            "search": "Suche fokussieren",
            "scan": "Bibliotheken scannen",
            "back": "Zurück / Dialog schließen",
            "playlist_remove": "Eintrag aus Playlist entfernen",
        }.get(action_id, action_id)


def shortcut_map(cfg: dict) -> dict[str, str]:
    stored = cfg.get("shortcuts", {}) or {}
    mapping = {}
    for action_id, default_key in SHORTCUT_ACTIONS.items():
        value = stored.get(action_id)
        mapping[action_id] = value if isinstance(value, str) and value.strip() else default_key
    return mapping


def find_conflict(mapping: dict[str, str], action_id: str, new_key: str) -> str | None:
    normalized = QKeySequence(new_key).toString().lower()
    if not normalized:
        return None
    for other_id, key in mapping.items():
        if other_id != action_id and QKeySequence(key).toString().lower() == normalized:
            return other_id
    return None


def register_shortcut(parent: QWidget, action_id: str, key_sequence: str, callback) -> QShortcut:
    shortcut = QShortcut(QKeySequence(key_sequence), parent)
    shortcut.activated.connect(callback)
    return shortcut


def _ffmpeg_binary() -> str | None:
    """ffmpeg finden (Fallback-Kette: PATH zuerst) — None = keine Video-Thumbnails."""
    found = shutil_which("ffmpeg")
    if found:
        return found
    for candidate in ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/snap/bin/ffmpeg"):
        if Path(candidate).is_file():
            return candidate
    return None


#: Zeitpunkte (Sekunden), an denen nacheinander ein Frame extrahiert wird —
#: viele Videos sind bei 0 s schwarz, daher spätere zuerst.
_FFMPEG_OFFSETS = (60, 15, 0)


def extract_video_frame(video: str | Path, width: int, height: int) -> Image.Image | None:
    """Einzelbild aus einem Video als Thumbnail generieren (ffmpeg, Hintergrund-Thread).

    Läuft NUR im Thumbnail-Worker, nie im GUI-Thread. Kein ffmpeg installiert
    oder fehlgeschlagen → None (die Ansicht fällt auf den Platzhalter zurück).
    """
    ffmpeg = _ffmpeg_binary()
    video_path = Path(video)
    if not ffmpeg or not video_path.is_file():
        return None
    for offset in _FFMPEG_OFFSETS:
        cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            str(offset),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-vf",
            f"scale='min({width},iw)':-2",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=25,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("ffmpeg-Thumbnail fehlgeschlagen: %s (%s)", video_path, exc)
            return None
        if proc.returncode == 0 and proc.stdout:
            try:
                frame = Image.open(BytesIO(proc.stdout))
                frame.load()
            except (OSError, ValueError):
                logger.warning("ffmpeg-Frame nicht dekodierbar: %s", video_path)
                return None
            # 2:3-Posterwand: Frame in voller Breite einpassen, oben/unten
            # Balken in Platzhalterfarbe (kein Beschnitt — nichts fehlt im Bild).
            return ImageOps.pad(frame.convert("RGB"), (width, height), method=Image.Resampling.BILINEAR)
    logger.info("Kein Frame extrahierbar (ggf. schwarzes Leervideo): %s", video_path)
    return None


def _font_pil(size: int):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split(" ")
        current = ""
        for word in words:
            attempt = f"{current} {word}".strip()
            if draw.textlength(attempt, font=font) <= max_width or not current:
                current = attempt
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines[:4]


def render_placeholder(title: str, width: int, height: int, tokens: dict[str, str]) -> Image.Image:
    bg = ImageColor.getrgb(tokens["poster_placeholder_bg"])
    fg = ImageColor.getrgb(tokens["text_primary"])
    dim = ImageColor.getrgb(tokens["text_disabled"])
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    font = _font_pil(max(12, width // 10))
    lines = _wrap_text(draw, title, font, width - 20)
    line_height = font.size + 6 if hasattr(font, "size") else width // 8
    y = max(16, (height - line_height * len(lines)) // 2 - 10)
    for line in lines:
        text_width = draw.textlength(line, font=font)
        draw.text(((width - text_width) / 2, y), line, font=font, fill=fg)
        y += line_height
    small = _font_pil(max(9, width // 16))
    hint_width = draw.textlength("Kein Poster", font=small)
    draw.text(((width - hint_width) / 2, height - small.size - 12), "Kein Poster", font=small, fill=dim)
    return image


def thumb_cache_key(
    prefix: str,
    ident: object,
    source: str | None,
    width: int,
    theme_name: str,
    title: str = "",
    missing: bool = False,
    video: str = "",
    height: int | None = None,
    fallback: str = "",
) -> str:
    """Cache-Key für Thumbnails — reine String-Bindung, KEIN Dateizugriff.

    Echte Bilder sind an Quell-Pfad+Kachelgröße gebunden — bewusst NICHT an das
    Theme, damit ein Skin-Wechsel kein Neu-Rendern der ganzen Posterwand auslöst
    (die eigentliche Ursache für den ~10-s-Lag). Bewusst OHNE mtime-Stat: Beim
    Start wird die Medienbibliothek dafür nicht mehr angefasst (DB + Thumbnail-
    Cache genügen); ein ausgetauschtes Posterbild erscheint, sobald ein Scan
    einen anderen Poster-Pfad in die DB schreibt. Ohne Poster, aber mit Video-
    Pfad: generiertes Video-Thumbnail am Video gebunden. Platzhalter hängen am
    Theme.
    """
    size = f"{width}x{height}" if height else str(width)
    if source:
        return f"{prefix}:{ident}:{size}:{source}:{int(missing)}"
    if video:
        key = f"{prefix}vid:{ident}:{size}:{video}:{int(missing)}"
        return f"{key}:{fallback}" if fallback else key
    return f"ph:{theme_name}:{width}:{title}:{int(missing)}"


class ThumbnailWorker(QThread):
    """Skaliert Poster/Thumbnails mit Pillow (up+down) im Hintergrund-Thread.

    Der Cache-Key wird HIER berechnet — inklusive Plattencache-Prüfung. Der
    GUI-Thread reicht nur DB-Werte durch (Quellpfad, Titel, Größe) und greift
    beim Anzeigen aus der Datenbank nicht auf Medienordner zu: Start ohne
    „Beim Start scannen" = reine DB-Ansicht ohne Bibliotheks-IO.
    """

    ready = Signal(object, str)  # token (GUI-Routingschlüssel), cache file path

    def __init__(
        self, cache: Path, theme_tokens: dict[str, str], theme_name: str, parent: QObject | None = None
    ):
        super().__init__(parent)
        self._cache_dir = Path(cache)
        self._tokens = theme_tokens
        self._theme_name = theme_name
        self._queue: queue.Queue = queue.Queue()
        self._stopped = False

    def submit(
        self,
        token: object,
        prefix: str,
        ident: object,
        source: str | None,
        title: str,
        width: int,
        missing: bool = False,
        video: str = "",
        height: int | None = None,
        fallback: str = "",
    ) -> None:
        self._queue.put((token, prefix, ident, source, title, width, missing, video, height, fallback))

    def stop(self) -> None:
        # Flag statt Gift-Pille am Queue-Ende: Der Worker bricht nach dem
        # laufenden Bild sofort ab, statt den Rest der alten Queue abzuarbeiten.
        self._stopped = True
        self._queue.put(None)

    def _cache_path(self, key: str) -> Path:
        digest = sha1(key.encode("utf-8", errors="replace")).hexdigest()
        return self._cache_dir / f"{digest}.jpg"

    def run(self) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        while True:
            item = self._queue.get()
            if item is None or self._stopped:
                break
            token, prefix, ident, source, title, width, missing, video, height, fallback = item
            if not height:
                height = int(width * 1.5)
            key = thumb_cache_key(
                prefix,
                ident,
                source,
                width,
                self._theme_name,
                title,
                missing,
                video=video,
                height=height,
                fallback=fallback,
            )
            cache_file = self._cache_path(key)
            try:
                if not cache_file.exists():
                    self._render(
                        source, title, width, height, missing, cache_file, video=video, fallback=fallback
                    )
                self.ready.emit(token, str(cache_file))
            except Exception:
                logger.exception("Thumbnail fehlgeschlagen: %s", source or video or title)
                self.ready.emit(token, "")
        logger.info("Thumbnail-Worker beendet")

    def _render(
        self, source, title, width, height, missing, target: Path, video: str = "", fallback: str = ""
    ) -> None:
        image: Image.Image | None = None
        if source:
            try:
                with Image.open(source) as opened:
                    # JPEG-Vorschau: Decoder liefert bereits verkleinerte Pixel
                    # (großzügig 2x Zielgröße) — riesige Scans dekodieren so
                    # Bruchteile statt Sekunden.
                    opened.draft("RGB", (width * 2, height * 2))
                    image = ImageOps.contain(opened.convert("RGB"), (width, height))
            except OSError as exc:
                logger.warning("Bild nicht lesbar: %s (%s)", source, exc)
                image = None
        if image is None and video:
            # Kein Poster: Thumbnail aus dem Video selbst generieren (ffmpeg).
            image = extract_video_frame(video, width, height)
        if image is None and fallback:
            # Weder Still noch Video-Frame verfügbar (z. B. kein ffmpeg):
            # Serien-Poster als Thumbnail für die Folge-Kachel.
            try:
                with Image.open(fallback) as opened:
                    opened.draft("RGB", (width * 2, height * 2))
                    image = ImageOps.contain(opened.convert("RGB"), (width, height))
            except OSError as exc:
                logger.warning("Fallback-Bild nicht lesbar: %s (%s)", fallback, exc)
                image = None
        if image is None:
            image = render_placeholder(title, width, height, self._tokens)
        elif missing:
            overlay = Image.new("RGB", image.size, ImageColor.getrgb(self._tokens["bg_window"]))
            image = Image.blend(image, overlay, 0.65)
        image.save(target, format="JPEG", quality=88)


class ScanWorker(QThread):
    progress = Signal(int, str)  # Prozent (0–100), aktueller Datei-/Ordnername
    finished_ok = Signal(dict)
    failed = Signal(str)

    def __init__(self, db_path: Path, libraries: list[dict], parent: QObject | None = None):
        super().__init__(parent)
        self._db_path = Path(db_path)
        self._libraries = libraries
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        totals = {"scanned": 0, "updated": 0, "unchanged": 0, "missing": 0, "warnings": []}
        try:
            conn = db_connect(self._db_path)
        except (sqlite3.Error, OSError) as exc:
            logger.error("Datenbank nicht öffnbar: %s", exc)
            self.failed.emit(str(exc))
            return
        try:
            n_libs = max(len(self._libraries), 1)
            for index, library in enumerate(self._libraries):
                if self._cancel_requested:
                    break
                logger.info("Scanne Bibliothek %s (%s)", library.get("name", "?"), library.get("path"))
                base = index * 100.0 / n_libs
                span = 100.0 / n_libs

                def report(done, total, label, base=base, span=span):
                    percent = int(base + (done / total) * span) if total else int(base)
                    self.progress.emit(min(percent, 100), label)

                if normalize_library_type(library.get("type", "movies")) == "shows":
                    scan = scan_series_library(
                        conn, library, progress=report, cancel_check=lambda: self._cancel_requested
                    )
                else:
                    scan = scan_movie_library(
                        conn, library, progress=report, cancel_check=lambda: self._cancel_requested
                    )
                totals["scanned"] += scan.scanned
                totals["updated"] += scan.updated
                totals["unchanged"] += scan.unchanged
                totals["missing"] += scan.missing
                totals["warnings"].extend(scan.warnings)
        except Exception as exc:
            logger.exception("Scan fehlgeschlagen")
            self.failed.emit(str(exc))
        finally:
            conn.close()
        self.finished_ok.emit(totals)


ROLE_REF = Qt.ItemDataRole.UserRole + 1
ROLE_TITLE = Qt.ItemDataRole.UserRole + 2
ROLE_YEAR = Qt.ItemDataRole.UserRole + 3
ROLE_ADDED = Qt.ItemDataRole.UserRole + 4
ROLE_GENRES = Qt.ItemDataRole.UserRole + 5

SORT_TITLE, SORT_YEAR = "title", "year"


@dataclass
class MediaRef:
    ref_type: str  # "movie" | "episode" | "show" | "season"
    ref_id: int  # bei "season": die Show-ID
    season: int | None = None  # nur bei "season": Staffelnummer

    def __hash__(self) -> int:
        return hash((self.ref_type, self.ref_id, self.season))


@dataclass
class PosterItem:
    ref: MediaRef
    title: str
    poster_path: str | None
    missing: bool = False
    year: int | None = None
    date_added: str | None = None
    genres: list[str] = field(default_factory=list)
    video_path: str = ""  # für generierte Video-Thumbnails (kein Poster vorhanden)


def _matches_year_query(query: str, year) -> bool:
    """Jahres-Suche: „2002“ (genau), „1977-2005“ (Zeitraum inklusive),
    „1977-“ (ab), „-2005“ (bis). Kein Jahresmuster → kein Jahresfilter."""
    if re.fullmatch(r"\d{4}", query):
        try:
            return int(year) == int(query)
        except (TypeError, ValueError):
            return False
    match = re.fullmatch(r"(\d{0,4})\s*-\s*(\d{0,4})", query)
    if not match:
        return False
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return False
    try:
        value = int(year)
    except (TypeError, ValueError):
        return False
    return not (start_text and value < int(start_text)) and not (end_text and value > int(end_text))


class PosterProxyModel(QSortFilterProxyModel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._search = ""
        self._genre = ""
        self._sort_mode = SORT_TITLE

    def set_search(self, text: str) -> None:
        self._search = text.strip().casefold()
        self.invalidate()

    def set_genre_filter(self, genre: str) -> None:
        self._genre = genre.strip().casefold()
        self.invalidate()

    def set_sort_mode(self, mode: str) -> None:
        self._sort_mode = mode
        # invalidate() ist Pflicht: sort() allein wäre ein No-op, solange Spalte
        # und Reihenfolge unverändert sind — dann hing der Wechsel Titel↔Jahr
        # nach dem ersten Umschalten und die Reihenfolge änderte sich nicht mehr.
        self.invalidate()
        self.sort(0, Qt.SortOrder.AscendingOrder)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._search and not self._genre:
            return True
        model = self.sourceModel()
        index = model.index(source_row, 0, source_parent)
        if self._genre:
            genres = [str(g).casefold() for g in (index.data(ROLE_GENRES) or [])]
            if self._genre not in genres:
                return False
        if not self._search:
            return True
        if self._search in str(index.data(ROLE_TITLE) or "").casefold():
            return True
        genre_blob = ", ".join(str(g) for g in (index.data(ROLE_GENRES) or [])).casefold()
        if genre_blob and self._search in genre_blob:
            return True
        return _matches_year_query(self._search, index.data(ROLE_YEAR))

    def lessThan(self, left: QModelIndex, right: QModelIndex) -> bool:
        if self._sort_mode == SORT_YEAR:
            return (left.data(ROLE_YEAR) or 0) < (right.data(ROLE_YEAR) or 0)
        return str(left.data(ROLE_TITLE) or "").casefold() < str(right.data(ROLE_TITLE) or "").casefold()


class PosterGrid(QListView):
    """Posterwand: Einzelklick spielt ab, Rechtsklick öffnet das Kontextmenü."""

    play_requested = Signal(object)
    context_requested = Signal(object, QPoint)
    selection_changed = Signal()

    def __init__(self, poster_width: int, label_under_poster: bool, parent: QWidget | None = None):
        super().__init__(parent)
        self._model = QStandardItemModel(self)
        self._items_by_ref: dict[MediaRef, QStandardItem] = {}
        self._proxy = PosterProxyModel(self)
        self._proxy.setSourceModel(self._model)
        self.setModel(self._proxy)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setWrapping(True)
        self.setWordWrap(True)
        self.setUniformItemSizes(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)
        self.clicked.connect(self._on_clicked)
        self.selectionModel().selectionChanged.connect(lambda *_: self.selection_changed.emit())
        self.set_poster_size(poster_width, label_under_poster)

    def set_poster_size(self, width: int, label_under_poster: bool) -> None:
        height = int(width * 1.5)
        label_height = 26 if label_under_poster else 0
        self.setIconSize(QSize(width, height))
        self.setGridSize(QSize(width + 18, height + label_height + 14))
        grid = self.gridSize()
        for row in range(self._model.rowCount()):
            self._model.item(row).setSizeHint(grid)

    def set_items(self, items: list[PosterItem]) -> None:
        self._model.removeRows(0, self._model.rowCount())
        self._items_by_ref.clear()
        for item in items:
            std = QStandardItem(item.title)
            std.setData(item.ref, ROLE_REF)
            std.setData(item.title, ROLE_TITLE)
            std.setData(item.year, ROLE_YEAR)
            std.setData(item.date_added or "", ROLE_ADDED)
            std.setData(list(item.genres), ROLE_GENRES)
            std.setEditable(False)
            std.setSizeHint(self.gridSize())
            if item.missing:
                std.setForeground(QBrush(QColor("#5C7A5C")))
            self._model.appendRow(std)
            self._items_by_ref[item.ref] = std
        self._proxy.invalidate()

    def set_icon(self, ref: MediaRef, icon) -> None:
        item = self._items_by_ref.get(ref)
        if item is not None:
            # PyQt5 verlangt QIcon (PySide6 akzeptiert auch QPixmap) — deshalb
            # QPixmap explizit verpacken.
            item.setIcon(icon if isinstance(icon, QIcon) else QIcon(icon))

    def set_icons(self, icons: dict[MediaRef, object]) -> None:
        """Mehrere Icons auf einmal setzen (Wiederverwendung ohne Worker-Runde)."""
        for ref, icon in icons.items():
            item = self._items_by_ref.get(ref)
            if item is not None:
                item.setIcon(icon if isinstance(icon, QIcon) else QIcon(icon))

    def set_search(self, text: str) -> None:
        self._proxy.set_search(text)

    def set_genre_filter(self, genre: str) -> None:
        self._proxy.set_genre_filter(genre)

    def set_sort_mode(self, mode: str) -> None:
        self._proxy.set_sort_mode(mode)

    def _ref_at(self, index) -> MediaRef | None:
        item = self._model.itemFromIndex(self._proxy.mapToSource(index))
        return item.data(ROLE_REF) if item else None

    def _on_clicked(self, index) -> None:
        ref = self._ref_at(index)
        if ref:
            self.play_requested.emit(ref)

    def _on_context(self, pos) -> None:
        index = self.indexAt(pos)
        if not index.isValid():
            return
        self.setCurrentIndex(index)
        ref = self._ref_at(index)
        if ref:
            self.context_requested.emit(ref, self.viewport().mapToGlobal(pos))

    def current_ref(self) -> MediaRef | None:
        return self._ref_at(self.currentIndex())


class MovieListView(QTreeWidget):
    play_requested = Signal(object)
    context_requested = Signal(object, QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels([S.SORT_TITLE, S.SORT_YEAR, S.DETAILS_RATING, S.DETAILS_RUNTIME])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)
        self.itemDoubleClicked.connect(lambda item, _col: self._emit_play(item))
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self._movies: list[Movie] = []
        self._search = ""
        self._genre = ""

    def _emit_play(self, item: QTreeWidgetItem) -> None:
        ref = item.data(0, ROLE_REF)
        if ref:
            self.play_requested.emit(ref)

    def set_movies(self, movies: list[Movie]) -> None:
        self._movies = list(movies)
        self._apply_filter()

    def set_search(self, text: str) -> None:
        self._search = text.strip().casefold()
        self._apply_filter()

    def set_genre_filter(self, genre: str) -> None:
        self._genre = genre.strip().casefold()
        self._apply_filter()

    def _accepts(self, movie: Movie) -> bool:
        if self._genre and self._genre not in (g.casefold() for g in movie.genres):
            return False
        if not self._search:
            return True
        if self._search in movie.title.casefold():
            return True
        genre_blob = ", ".join(movie.genres).casefold()
        if genre_blob and self._search in genre_blob:
            return True
        return _matches_year_query(self._search, movie.year)

    def _apply_filter(self) -> None:
        self.clear()
        for movie in self._movies:
            if not self._accepts(movie):
                continue
            item = QTreeWidgetItem(
                [
                    movie.title,
                    str(movie.year) if movie.year else "",
                    f"{movie.rating:.1f}" if movie.rating else "",
                    S.DETAILS_MINUTES.format(movie.runtime_min) if movie.runtime_min else "",
                ]
            )
            item.setData(0, ROLE_REF, MediaRef("movie", movie.id))
            if movie.missing:
                item.setDisabled(True)
            self.addTopLevelItem(item)

    def _on_context(self, pos) -> None:
        item = self.itemAt(pos)
        if not item:
            return
        self.setCurrentItem(item)
        ref = item.data(0, ROLE_REF)
        if ref:
            self.context_requested.emit(ref, self.viewport().mapToGlobal(pos))

    def current_ref(self) -> MediaRef | None:
        item = self.currentItem()
        return item.data(0, ROLE_REF) if item else None


LEVEL_SHOWS, LEVEL_SEASONS, LEVEL_EPISODES = 0, 1, 2


class SeriesListView(QTreeWidget):
    """Serien als Liste (Titel/Jahr/Bewertung) — Pendant zur Serien-Posterwand.

    Gefüllt wird sie mit den GLEICH gefilterten Serien wie die Posterwand
    (SeriesView.set_shows liest die sichtbaren Zeilen aus dem Grid-Proxy).
    """

    play_requested = Signal(object)
    context_requested = Signal(object, QPoint)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setColumnCount(3)
        self.setHeaderLabels([S.SORT_TITLE, S.SORT_YEAR, S.DETAILS_RATING])
        self.setRootIsDecorated(False)
        self.setAlternatingRowColors(True)
        self.setAllColumnsShowFocus(True)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)
        self.itemDoubleClicked.connect(lambda item, _col: self._emit_play(item))
        self.setSortingEnabled(True)
        self.sortByColumn(0, Qt.SortOrder.AscendingOrder)

    def _emit_play(self, item: QTreeWidgetItem) -> None:
        ref = item.data(0, ROLE_REF)
        if ref:
            self.play_requested.emit(ref)

    def set_shows(self, shows: list[Show]) -> None:
        self.clear()
        for show in shows:
            item = QTreeWidgetItem(
                [
                    show.title,
                    str(show.year) if show.year else "",
                    f"{show.rating:.1f}" if show.rating else "",
                ]
            )
            item.setData(0, ROLE_REF, MediaRef("show", show.id))
            if show.missing:
                item.setDisabled(True)
            self.addTopLevelItem(item)

    def _on_context(self, pos) -> None:
        item = self.itemAt(pos)
        if not item:
            return
        self.setCurrentItem(item)
        ref = item.data(0, ROLE_REF)
        if ref:
            self.context_requested.emit(ref, self.viewport().mapToGlobal(pos))

    def current_ref(self) -> MediaRef | None:
        item = self.currentItem()
        return item.data(0, ROLE_REF) if item else None


class SeriesView(QWidget):
    play_requested = Signal(object)
    context_requested = Signal(object, QPoint)

    def __init__(
        self,
        conn,
        theme_tokens: dict[str, str],
        theme_name: str,
        parent: QWidget | None = None,
        poster_width: int = 200,
        label_under_poster: bool = True,
    ):
        super().__init__(parent)
        self._conn = conn
        self._theme = theme_tokens
        self._theme_name = theme_name
        self._current_show: Show | None = None
        self._current_season: int | None = None
        self._thumb_worker: ThumbnailWorker | None = None
        self._thumb_callbacks: dict[int, object] = {}
        self._thumb_memory_keys: dict[int, tuple] = {}
        self._icon_memory: dict[tuple, QPixmap] = {}
        self._next_token = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self._breadcrumb = QLabel()
        self._breadcrumb.setTextFormat(Qt.TextFormat.RichText)
        self._breadcrumb.linkActivated.connect(self._on_breadcrumb)
        self._breadcrumb.setContentsMargins(12, 6, 12, 0)
        layout.addWidget(self._breadcrumb)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack, stretch=1)

        shows_page = QWidget()
        shows_layout = QVBoxLayout(shows_page)
        shows_layout.setContentsMargins(8, 0, 8, 0)
        self._shows_grid = PosterGrid(poster_width, label_under_poster, self)
        self._shows_grid.play_requested.connect(self._show_clicked)
        self._shows_grid.context_requested.connect(self.context_requested)
        self._shows_list = SeriesListView()
        self._shows_list.play_requested.connect(self._show_clicked)
        self._shows_list.context_requested.connect(self.context_requested)
        self._shows_list.hide()
        shows_layout.addWidget(self._shows_grid)
        shows_layout.addWidget(self._shows_list)
        self._stack.addWidget(shows_page)

        seasons_page = QWidget()
        seasons_layout = QHBoxLayout(seasons_page)
        seasons_layout.setContentsMargins(12, 12, 12, 12)
        seasons_layout.setSpacing(20)
        left = QVBoxLayout()
        self._show_poster = QLabel()
        self._show_poster.setFixedWidth(180)
        self._show_poster.setAlignment(Qt.AlignmentFlag.AlignTop)
        left.addWidget(self._show_poster)
        self._show_plot = QLabel()
        self._show_plot.setWordWrap(True)
        self._show_plot.setObjectName("secondary")
        left.addWidget(self._show_plot)
        left.addStretch(1)
        seasons_layout.addLayout(left)
        self._season_list = QListWidget()
        self._season_list.setViewMode(QListWidget.ViewMode.IconMode)
        self._season_list.setIconSize(QSize(140, 210))
        self._season_list.setMovement(QListWidget.Movement.Static)
        self._season_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self._season_list.setWordWrap(True)
        self._season_list.itemClicked.connect(self._season_clicked)
        self._season_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._season_list.customContextMenuRequested.connect(self._on_episode_context)
        seasons_layout.addWidget(self._season_list, stretch=1)
        self._stack.addWidget(seasons_page)

        self._episode_list = QListWidget()
        self._episode_list.setViewMode(QListWidget.ViewMode.ListMode)
        self._episode_list.setIconSize(QSize(96, 54))
        self._episode_list.setAlternatingRowColors(True)
        self._episode_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._episode_list.customContextMenuRequested.connect(self._on_episode_context)
        self._episode_list.itemClicked.connect(self._episode_clicked)
        self._stack.addWidget(self._episode_list)

        self._update_breadcrumb()

    def _request_thumb(
        self,
        prefix: str,
        ident: object,
        source: str | None,
        title: str,
        width: int,
        missing: bool,
        callback,
        video: str = "",
        height: int | None = None,
        fallback: str = "",
    ) -> None:
        """Poster/Platzhalter im Worker-Thread rendern (keine GUI-Blockade).

        Der Cache-Key (inkl. Vergleich für den Speicher-Cache) wird ohne
        Dateizugriff aus DB-Werten gebildet; der Worker berechnet den
        Cache-Dateinamen selbst und prüft den Plattencache im Hintergrund.
        Ohne Poster/Still wird aus `video` (DB-Pfad) ein Frame-Thumbnail
        generiert — ffmpeg läuft im Worker, nicht im GUI-Thread. Liefert auch
        das kein Bild (z. B. ohne ffmpeg), wird `fallback` als Thumbnail
        verwendet.
        """
        memory_key = (prefix, ident, source, width, height, missing, video, fallback, "" if source else title)
        cached = self._icon_memory.get(memory_key)
        if cached is not None:
            callback(cached)
            return
        if self._thumb_worker is None or not self._thumb_worker.isRunning():
            worker = ThumbnailWorker(cache_dir(), self._theme, self._theme_name, self)
            worker.ready.connect(self._on_thumb_ready)
            worker.start()
            self._thumb_worker = worker
        token = self._next_token
        self._next_token += 1
        self._thumb_callbacks[token] = callback
        self._thumb_memory_keys[token] = memory_key
        self._thumb_worker.submit(
            token, prefix, ident, source, title, width, missing, video, height, fallback
        )

    def _on_thumb_ready(self, token: object, path: str) -> None:
        callback = self._thumb_callbacks.pop(token, None)
        memory_key = self._thumb_memory_keys.pop(token, None)
        if callback is None or not path:
            return
        pixmap = QPixmap(path)
        if not pixmap.isNull():
            if memory_key is not None:
                self._icon_memory[memory_key] = pixmap
            callback(pixmap)

    def set_search(self, text: str) -> None:
        self._shows_grid.set_search(text)
        self._sync_shows_list()

    def set_genre_filter(self, genre: str) -> None:
        self._shows_grid.set_genre_filter(genre)
        self._sync_shows_list()

    def set_sort_mode(self, mode: str) -> None:
        self._shows_grid.set_sort_mode(mode)
        self._sync_shows_list()

    def _sync_shows_list(self) -> None:
        """Serien-Liste an die GEFILTERTEN/sortierten Zeilen der Posterwand anpassen."""
        shows_by_id = {show.id: show for show in getattr(self, "_shows_source", [])}
        visible_shows: list[Show] = []
        for row in range(self._shows_grid._proxy.rowCount()):
            ref = self._shows_grid._proxy.index(row, 0).data(ROLE_REF)
            if ref is not None and ref.ref_id in shows_by_id:
                visible_shows.append(shows_by_id[ref.ref_id])
        self._shows_list.set_shows(visible_shows)

    def _poster_for_display(self, show: Show) -> str | None:
        """Anzeigepfad des Serienposters — direkt aus der DB, ohne Ordnerzugriff.

        URL-Thumbs aus gescrapten tvshow.nfo werden ignoriert (Platzhalter statt
        leerer Kachel). Die Bereinigung fehlerhafter DB-Einträge passiert beim
        SCAN (build_show übernimmt nur noch lokale Dateien), nicht bei jedem
        Fensteraufbau — deshalb wird hier weder stat() noch der Serienordner
        gelesen. Nachzumischen ist teuer und gehört zum Scan (F5).
        """
        poster = show.poster_path
        if poster and poster.startswith("http"):
            return None
        return poster

    def set_shows(self, shows: list[Show]) -> None:
        # Erstes Video je Serie (DB-Werte) — Fallback für Serien ohne Poster:
        # der Worker generiert daraus ein Frame-Thumbnail (ffmpeg, Hintergrund).
        first_videos: dict[int, str] = {}
        for row in self._conn.execute(
            "SELECT show_id, video_path FROM episodes WHERE missing = 0 ORDER BY show_id, season, episode"
        ):
            if row["show_id"] not in first_videos and row["video_path"]:
                first_videos[row["show_id"]] = row["video_path"]
        items = []
        for show in shows:
            show.poster_path = self._poster_for_display(show)
            items.append(
                PosterItem(
                    ref=MediaRef("show", show.id),
                    title=show.title,
                    poster_path=show.poster_path,
                    missing=show.missing,
                    year=show.year,
                    genres=show.genres,
                )
            )
        self._shows_grid.set_items(items)
        # Listenansicht mit den GLEICH gefilterten Serien füllen (Grid-Proxy);
        # bei Suche/Genre/Sort wird _sync_shows_list erneut aufgerufen.
        self._shows_source = list(shows)
        self._sync_shows_list()
        icon_size = self._shows_grid.iconSize()
        for show in shows:
            if show.id is None:
                continue
            ref = MediaRef("show", show.id)
            self._request_thumb(
                "show",
                show.id,
                show.poster_path,
                show.title,
                icon_size.width(),
                show.missing,
                lambda pixmap, r=ref: self._shows_grid.set_icon(
                    r,
                    pixmap.scaled(
                        icon_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    ),
                ),
                video=first_videos.get(show.id, ""),
            )

    def _show_clicked(self, ref: MediaRef) -> None:
        if ref.ref_type != "show":
            return
        row = self._conn.execute("SELECT * FROM shows WHERE id = ?", (ref.ref_id,)).fetchone()
        if not row:
            return
        self._current_show = db_get_show_by_path(self._conn, row["path"])
        self._current_season = None
        self._fill_season_page()
        self._stack.setCurrentIndex(LEVEL_SEASONS)
        self._update_breadcrumb()

    def _fill_season_page(self) -> None:
        show = self._current_show
        if show is None:
            return
        self._season_list.clear()
        self._show_poster.setText("")
        # Nicht das Original in voller Auflösung laden: Das 180-px-Vorschaubild
        # kommt aus dem Thumbnail-Worker (Cache) — riesige Poster bleiben unkritisch.
        # Kein is_file()-Check: Der Worker rendert aus der DB-Quelle oder einen
        # Platzhalter, der GUI-Thread greift nicht auf die Datei zu.
        if show.poster_path:
            self._request_thumb(
                "showposter",
                show.id,
                show.poster_path,
                show.title,
                180,
                show.missing,
                lambda pixmap: self._show_poster.setPixmap(
                    pixmap.scaledToWidth(180, Qt.TransformationMode.SmoothTransformation)
                ),
            )
        else:
            first_video = self._conn.execute(
                "SELECT video_path FROM episodes WHERE show_id = ? AND missing = 0 "
                "ORDER BY season, episode LIMIT 1",
                (show.id,),
            ).fetchone()
            self._request_thumb(
                "showposter",
                show.id,
                None,
                show.title,
                180,
                show.missing,
                lambda pixmap: self._show_poster.setPixmap(
                    pixmap.scaledToWidth(180, Qt.TransformationMode.SmoothTransformation)
                ),
                video=first_video["video_path"] if first_video else "",
            )
        self._show_plot.setText(show.plot or "")
        seasons = db_list_seasons(self._conn, show.id)
        if not seasons:
            episodes = db_list_episodes(self._conn, show.id)
            if episodes:
                # Keine Staffel-Zeilen (z. B. alter DB-Stand): eine Kachel je Staffelnummer.
                seasons = [Season(show_id=show.id, number=n) for n in sorted({e.season for e in episodes})]
        icon_size = self._season_list.iconSize()
        for season in seasons:
            item = QListWidgetItem(S.SERIES_SEASON_TILE.format(season.number))
            item.setData(Qt.ItemDataRole.UserRole, MediaRef("season", show.id, season.number))
            self._season_list.addItem(item)
            self._request_thumb(
                "season",
                f"{show.id}:{season.number}",
                season.poster_path,
                S.SERIES_SEASON_TILE.format(season.number),
                icon_size.width(),
                False,
                lambda pixmap, it=item: it.setIcon(
                    QIcon(
                        pixmap.scaled(
                            icon_size,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation,
                        )
                    )
                ),
            )

    def _season_clicked(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, MediaRef):
            self._current_season = data.season  # MediaRef("season", show_id, Staffelnummer)
        else:
            self._current_season = data  # alter Stand: Staffelnummer als int
        self._fill_episode_list()
        self._stack.setCurrentIndex(LEVEL_EPISODES)
        self._update_breadcrumb()

    def _fill_episode_list(self) -> None:
        show, season = self._current_show, self._current_season
        if show is None or season is None:
            return
        self._episode_list.clear()
        episodes = [ep for ep in db_list_episodes(self._conn, show.id) if ep.season == season]
        if not episodes:
            empty = QListWidgetItem(S.SERIES_NO_EPISODES)
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self._episode_list.addItem(empty)
            return
        icon_size = self._episode_list.iconSize()
        for episode in episodes:
            prefix = S.SERIES_EPISODE_PREFIX.format(episode.season, episode.episode)
            item = QListWidgetItem(f"{prefix} · {episode.title}")
            item.setData(Qt.ItemDataRole.UserRole, MediaRef("episode", episode.id))
            if episode.missing:
                item.setForeground(QBrush(QColor(self._theme["text_disabled"])))
            self._episode_list.addItem(item)
            if episode.still_path or episode.video_path:
                self._request_thumb(
                    "still",
                    episode.id,
                    episode.still_path,
                    episode.title,
                    96,
                    episode.missing,
                    lambda pixmap, it=item: it.setIcon(
                        QIcon(
                            pixmap.scaled(
                                icon_size,
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                    ),
                    video=episode.video_path or "",
                    height=icon_size.height(),
                    # Kein Still: Serien-Poster als Thumbnail-Fallback (z. B. wenn
                    # kein ffmpeg für Video-Frames installiert ist).
                    fallback=self._current_show.poster_path or "",
                )

    def _episode_clicked(self, item: QListWidgetItem) -> None:
        ref = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(ref, MediaRef):
            self.play_requested.emit(ref)

    def _on_episode_context(self, pos) -> None:
        item = (
            self._episode_list.itemAt(pos)
            if self._stack.currentIndex() == LEVEL_EPISODES
            else self._season_list.itemAt(pos)
        )
        if not item:
            return
        ref = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(ref, MediaRef):
            self.context_requested.emit(ref, item.listWidget().viewport().mapToGlobal(pos))

    def _on_breadcrumb(self, link: str) -> None:
        if link == str(LEVEL_SHOWS):
            self._stack.setCurrentIndex(LEVEL_SHOWS)
            self._current_show = None
        elif link == str(LEVEL_SEASONS) and self._current_show:
            self._stack.setCurrentIndex(LEVEL_SEASONS)
        self._update_breadcrumb()

    def go_back(self) -> bool:
        current = self._stack.currentIndex()
        if current == LEVEL_EPISODES:
            self._stack.setCurrentIndex(LEVEL_SEASONS)
        elif current == LEVEL_SEASONS:
            self._stack.setCurrentIndex(LEVEL_SHOWS)
            self._current_show = None
        else:
            return False
        self._update_breadcrumb()
        return True

    def _update_breadcrumb(self) -> None:
        sec, primary = self._theme["text_secondary"], self._theme["text_primary"]
        show = self._current_show
        if show is None:
            self._breadcrumb.setText(
                f"<span style='color:{primary};font-weight:bold'>{S.SERIES_BREADCRUMB_ROOT}</span>"
            )
            return
        root_link = f"<a href='{LEVEL_SHOWS}' style='color:{sec}'>{S.SERIES_BREADCRUMB_ROOT}</a>"
        show_name = _esc_html(show.title)
        if self._stack.currentIndex() == LEVEL_SEASONS:
            self._breadcrumb.setText(
                f"{root_link} <span style='color:{sec}'>›</span> "
                f"<span style='color:{primary};font-weight:bold'>{show_name}</span>"
            )
        else:
            season_name = S.SERIES_SEASON_TILE.format(self._current_season or 0)
            self._breadcrumb.setText(
                f"{root_link} <span style='color:{sec}'>›</span> "
                f"<a href='{LEVEL_SEASONS}' style='color:{sec}'>{show_name}</a> "
                f"<span style='color:{sec}'>›</span> "
                f"<span style='color:{primary};font-weight:bold'>{_esc_html(season_name)}</span>"
            )

    def toggle_shows_view(self) -> bool:
        """Serienansicht: Posterwand ↔ Liste umschalten.

        Rückgabe: True, wenn jetzt die Liste sichtbar ist.
        """
        grid_hidden = self._shows_grid.isHidden()
        self._shows_grid.setVisible(grid_hidden)
        self._shows_list.setVisible(not grid_hidden)
        return not grid_hidden

    def current_episode_ref(self) -> MediaRef | None:
        if self._stack.currentIndex() != LEVEL_EPISODES:
            return None
        item = self._episode_list.currentItem()
        ref = item.data(Qt.ItemDataRole.UserRole) if item else None
        return ref if isinstance(ref, MediaRef) else None

    def current_season_ref(self) -> MediaRef | None:
        """Aktuell markierte Staffel-Kachel (für Enter-/Details-Taste)."""
        if self._stack.currentIndex() != LEVEL_SEASONS:
            return None
        item = self._season_list.currentItem()
        ref = item.data(Qt.ItemDataRole.UserRole) if item else None
        return ref if isinstance(ref, MediaRef) and ref.ref_type == "season" else None

    def current_show_ref(self) -> MediaRef | None:
        """Aktuell markierte Serie — Posterwand ODER Liste (für Details-Taste)."""
        if self._stack.currentIndex() != LEVEL_SHOWS:
            return None
        if not self._shows_grid.isHidden():
            return self._shows_grid.current_ref()
        return self._shows_list.current_ref()

    def random_episode_ref(self) -> MediaRef | None:
        """Zufällige Episode: in der Serien-Übersicht aus den GEFILTERTEN Serien,
        innerhalb einer Serie/eines Staffels aus der aktuellen Auswahl."""
        if self._stack.currentIndex() == LEVEL_SHOWS:
            show_ids = [
                ref.ref_id
                for row in range(self._shows_grid._proxy.rowCount())
                if (ref := self._shows_grid._proxy.index(row, 0).data(ROLE_REF)) is not None
            ]
            if not show_ids:
                return None
            show_id = random.choice(show_ids)
            episodes = [episode for episode in db_list_episodes(self._conn, show_id) if not episode.missing]
        else:
            if self._current_show is None or self._current_show.id is None:
                return None
            episodes = [
                episode
                for episode in db_list_episodes(self._conn, self._current_show.id)
                if not episode.missing
            ]
            if self._current_season is not None:
                episodes = [episode for episode in episodes if episode.season == self._current_season]
        if not episodes:
            return None
        return MediaRef("episode", random.choice(episodes).id)

    def shutdown(self) -> None:
        if self._thumb_worker is not None:
            self._thumb_worker.stop()
            self._thumb_worker.wait(2000)


def _esc_html(text: str) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _mono_html(text: str) -> str:
    return f"<code style='color:#9db89d'>{_esc_html(text)}</code>"


class DetailDialog(QDialog):
    play_requested = Signal(object, object)  # MediaRef, subtitle | None
    trailer_requested = Signal(object)

    def __init__(
        self,
        parent,
        ref,
        title,
        poster_path,
        fanart_path,
        meta_rows,
        plot,
        video_path,
        subtitle_paths,
        trailer_path,
        theme_name,
        file_label=None,
        nfo_path=None,
    ):
        super().__init__(parent)
        self.ref = ref
        self._fanart: QPixmap | None = None
        if fanart_path and Path(fanart_path).exists():
            self._fanart = QPixmap(fanart_path)
        self._theme = get_theme(theme_name)
        self.setWindowTitle(S.DETAILS_TITLE)
        self.resize(860, 560)

        root = QHBoxLayout(self)
        root.setContentsMargins(24, 24, 24, 16)
        root.setSpacing(20)

        poster = QLabel()
        poster.setFixedWidth(200)
        poster.setAlignment(Qt.AlignmentFlag.AlignTop)
        if poster_path and Path(poster_path).exists():
            poster.setPixmap(
                QPixmap(poster_path).scaledToWidth(200, Qt.TransformationMode.SmoothTransformation)
            )
        root.addWidget(poster)

        right = QVBoxLayout()
        right.setSpacing(8)
        title_label = QLabel(f"<h2>{_esc_html(title)}</h2>")
        title_label.setTextFormat(Qt.TextFormat.RichText)
        right.addWidget(title_label)

        for label, value in meta_rows:
            if not value:
                continue
            row = QLabel(
                f"<b style='color:{self._theme['text_secondary']}'>{_esc_html(label)}:</b> {_esc_html(value)}"
            )
            row.setTextFormat(Qt.TextFormat.RichText)
            row.setWordWrap(True)
            right.addWidget(row)

        if plot:
            plot_area = QScrollArea()
            plot_area.setWidgetResizable(True)
            plot_area.setFrameShape(QScrollArea.Shape.NoFrame)
            plot_body = QLabel(_esc_html(plot))
            plot_body.setWordWrap(True)
            plot_body.setAlignment(Qt.AlignmentFlag.AlignTop)
            plot_area.setWidget(plot_body)
            plot_area.setMinimumHeight(120)
            right.addWidget(plot_area, stretch=1)

        files = []
        if video_path:
            files.append(f"<b>{S.DETAILS_FILES_VIDEO}:</b> {_mono_html(video_path)}")
        if file_label:
            files.append(f"<b>{S.DETAILS_FILES}:</b> {_esc_html(file_label)}")
        if nfo_path:
            files.append(f"<b>{S.DETAILS_FILES_NFO}:</b> {_mono_html(nfo_path)}")
        if trailer_path:
            label = (
                S.DETAILS_FILES_TRAILER_URL if trailer_path.startswith("http") else S.DETAILS_FILES_TRAILER
            )
            files.append(f"<b>{label}:</b> {_mono_html(trailer_path)}")
        for index, sub in enumerate(subtitle_paths):
            files.append(
                f"<a href='sub:{index}' style='color:{self._theme['accent']}'>{_esc_html(sub.name)}</a>"
            )
        files_label = QLabel("<br>".join(files))
        files_label.setTextFormat(Qt.TextFormat.RichText)
        files_label.setWordWrap(True)
        files_label.linkActivated.connect(self._on_link)
        right.addWidget(files_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        play = QPushButton(S.DETAILS_BUTTON_PLAY)
        play.setObjectName("primary")
        play.clicked.connect(lambda: self.play_requested.emit(self.ref, None))
        play.setVisible(bool(video_path))  # Serie: kein direktes Video zum Abspielen
        trailer = QPushButton(S.DETAILS_BUTTON_TRAILER)
        trailer.setEnabled(bool(trailer_path))
        trailer.clicked.connect(lambda: self.trailer_requested.emit(self.ref))
        close = QPushButton(S.DETAILS_BUTTON_CLOSE)
        close.clicked.connect(self.reject)
        buttons.addWidget(play)
        buttons.addWidget(trailer)
        buttons.addWidget(close)
        right.addLayout(buttons)
        root.addLayout(right, stretch=1)
        self._subtitle_paths = subtitle_paths

    def _on_link(self, link: str) -> None:
        if link.startswith("sub:"):
            index = int(link.split(":", 1)[1])
            if 0 <= index < len(self._subtitle_paths):
                self.play_requested.emit(self.ref, self._subtitle_paths[index])

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        if self._fanart and not self._fanart.isNull():
            scaled = self._fanart.scaled(
                self.size(),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            painter.drawPixmap(0, 0, scaled)
            painter.fillRect(self.rect(), QColor(0, 0, 0, int(255 * 0.72)))
        else:
            painter.fillRect(self.rect(), QColor(self._theme["bg_window"]))
        super().paintEvent(event)


def meta_rows_for_movie(movie: Movie) -> list[tuple[str, str]]:
    return [
        (S.DETAILS_YEAR, str(movie.year) if movie.year else ""),
        (S.DETAILS_RATING, f"{movie.rating:.1f} / 10" if movie.rating else ""),
        (S.DETAILS_RUNTIME, S.DETAILS_MINUTES.format(movie.runtime_min) if movie.runtime_min else ""),
        (S.DETAILS_GENRES, ", ".join(movie.genres)),
        (S.DETAILS_STUDIO, movie.studio),
        (S.DETAILS_DIRECTOR, movie.director),
    ]


def meta_rows_for_episode(episode: Episode, show: Show | None) -> list[tuple[str, str]]:
    prefix = S.SERIES_EPISODE_PREFIX.format(episode.season, episode.episode)
    rows = []
    if show:
        rows.append((S.NAV_SHOWS, show.title))
    rows.append((S.SERIES_EPISODES_TITLE, f"{prefix} — {episode.title}"))
    if episode.rating:
        rows.append((S.DETAILS_RATING, f"{episode.rating:.1f} / 10"))
    if show and show.year:
        rows.append((S.DETAILS_YEAR, str(show.year)))
    return rows


def meta_rows_for_show(show: Show, season_count: int, episode_count: int) -> list[tuple[str, str]]:
    """Serien-Ebene: Metadaten aus tvshow.nfo (Plot, Jahr, Bewertung, Genres, Studio)
    plus Staffel-/Episodenzähler aus der DB."""
    return [
        (S.DETAILS_YEAR, str(show.year) if show.year else ""),
        (S.DETAILS_RATING, f"{show.rating:.1f} / 10" if show.rating else ""),
        (S.DETAILS_GENRES, ", ".join(show.genres)),
        (S.DETAILS_STUDIO, show.studio),
        (S.DETAILS_SEASONS_EPISODES, f"{season_count} / {episode_count}"),
    ]


ROLE_ITEM_ID = Qt.ItemDataRole.UserRole


class _FlowLayout(QLayout):
    """Sub-Layout, dessen Einträge bei schmaler Breite in die nächste Zeile
    umbrechen (nach dem offiziellen Qt-FlowLayout-Beispiel). Hält das
    Fensterminimum klein — Button-Reihen erzwingen sonst als Summe ihrer
    Textbreiten eine hohe Mindestbreite des Hauptfensters."""

    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self.setContentsMargins(0, 0, 0, 0)
        self._spacing = spacing
        self._items: list[QLayoutItem] = []

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientations:
        return Qt.Orientations(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        x = rect.x()
        y = rect.y()
        line_height = 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._spacing
            if next_x - self._spacing > rect.right() + 1 and line_height > 0:
                x = rect.x()
                y = y + line_height + self._spacing
                next_x = x + hint.width() + self._spacing
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


class PlaylistView(QWidget):
    play_requested = Signal(object)  # Playlist
    play_ref_requested = Signal(object)  # MediaRef (Film oder Episode)
    playlists_changed = Signal()

    def __init__(self, conn, parent: QWidget | None = None):
        super().__init__(parent)
        self._conn = conn
        self._current: Playlist | None = None

        splitter = QSplitter(self)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(splitter)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self._playlist_list = QListWidget()
        self._playlist_list.currentItemChanged.connect(self._on_select_playlist)
        # Doppelklick/Rechtsklick auf eine Playlist startet sie.
        self._playlist_list.itemDoubleClicked.connect(self._play_playlist_item)
        self._playlist_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._playlist_list.customContextMenuRequested.connect(self._on_playlist_context)
        left_layout.addWidget(self._playlist_list)
        buttons = _FlowLayout()
        for text, handler in (
            (S.PLAYLIST_NEW, self._new_playlist),
            (S.PLAYLIST_RENAME, self._rename_playlist),
            (S.PLAYLIST_DELETE, self._delete_playlist),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            if text == S.PLAYLIST_DELETE:
                button.setObjectName("danger")
            buttons.addWidget(button)
        left_layout.addLayout(buttons)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self._item_list = QListWidget()
        self._item_list.setIconSize(QSize(60, 90))
        self._item_list.setAlternatingRowColors(True)
        self._item_list.itemDoubleClicked.connect(self._play_item)
        right_layout.addWidget(self._item_list)
        right_buttons = _FlowLayout()
        for text, handler in (
            ("↑", lambda: self._move(-1)),
            ("↓", lambda: self._move(1)),
        ):
            button = QPushButton(text)
            button.clicked.connect(handler)
            right_buttons.addWidget(button)
        remove = QPushButton(S.MENU_REMOVE_FROM_PLAYLIST)
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_item)
        right_buttons.addWidget(remove)
        export = QPushButton(S.PLAYLIST_EXPORT)
        export.clicked.connect(self._export)
        right_buttons.addWidget(export)
        play = QPushButton(S.PLAYLIST_PLAY)
        play.setObjectName("primary")
        play.clicked.connect(self._play_all)
        right_buttons.addWidget(play)
        right_layout.addLayout(right_buttons)
        splitter.addWidget(right)
        splitter.setSizes([260, 620])

    def refresh(self) -> None:
        selected_id = self._current.id if self._current else None
        self._playlist_list.clear()
        for playlist in db_list_playlists(self._conn):
            item = QListWidgetItem(playlist.name)
            item.setData(ROLE_ITEM_ID, playlist.id)
            self._playlist_list.addItem(item)
            if playlist.id == selected_id:
                self._playlist_list.setCurrentItem(item)
        if not self._playlist_list.currentItem() and self._playlist_list.count():
            self._playlist_list.setCurrentRow(0)
        self._fill_items()

    def _selected_playlist(self) -> Playlist | None:
        item = self._playlist_list.currentItem()
        if not item:
            return None
        playlist_id = item.data(ROLE_ITEM_ID)
        for playlist in db_list_playlists(self._conn):
            if playlist.id == playlist_id:
                return playlist
        return None

    def _on_select_playlist(self, *_args) -> None:
        self._current = self._selected_playlist()
        self._fill_items()

    def _new_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, S.PLAYLIST_NEW, S.PLAYLIST_NEW_NAME)
        if ok and name.strip():
            db_create_playlist(self._conn, name.strip())
            self.refresh()
            self.playlists_changed.emit()

    def _rename_playlist(self) -> None:
        playlist = self._selected_playlist()
        if not playlist:
            return
        name, ok = QInputDialog.getText(
            self, S.PLAYLIST_RENAME_TITLE, S.PLAYLIST_NEW_NAME, text=playlist.name
        )
        if ok and name.strip():
            db_rename_playlist(self._conn, playlist.id, name.strip())
            self.refresh()
            self.playlists_changed.emit()

    def _delete_playlist(self) -> None:
        playlist = self._selected_playlist()
        if not playlist:
            return
        answer = QMessageBox.question(
            self,
            S.PLAYLIST_DELETE,
            S.PLAYLIST_DELETE_CONFIRM.format(playlist.name),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            db_delete_playlist(self._conn, playlist.id)
            self._current = None
            self.refresh()
            self.playlists_changed.emit()

    def _fill_items(self) -> None:
        self._item_list.clear()
        playlist = self._current
        if not playlist:
            return
        for item_row in db_list_playlist_items(self._conn, playlist.id):
            video_path, title = pl_resolve_item(self._conn, item_row)
            entry = QListWidgetItem(title or Path(video_path).name)
            poster = self._poster_for(item_row)
            if poster:
                pixmap = QPixmap(poster)
                if not pixmap.isNull():
                    entry.setIcon(
                        QIcon(
                            pixmap.scaled(
                                self._item_list.iconSize(),
                                Qt.AspectRatioMode.KeepAspectRatio,
                                Qt.TransformationMode.SmoothTransformation,
                            )
                        )
                    )
            entry.setData(ROLE_ITEM_ID, item_row.id)
            if video_path and not Path(video_path).exists():
                entry.setForeground(self.palette().brush(self.palette().ColorRole.Mid))
            self._item_list.addItem(entry)

    def _poster_for(self, item_row: PlaylistItem) -> str | None:
        if item_row.ref_type == "movie" and item_row.ref_id is not None:
            row = self._conn.execute(
                "SELECT poster_path FROM movies WHERE id = ?", (item_row.ref_id,)
            ).fetchone()
            return row["poster_path"] if row else None
        if item_row.ref_type == "episode" and item_row.ref_id is not None:
            row = self._conn.execute(
                "SELECT still_path FROM episodes WHERE id = ?", (item_row.ref_id,)
            ).fetchone()
            return row["still_path"] if row else None
        return None

    def _selected_item_id(self) -> int | None:
        item = self._item_list.currentItem()
        return item.data(ROLE_ITEM_ID) if item else None

    def _play_playlist_item(self, item: QListWidgetItem) -> None:
        playlist_id = item.data(ROLE_ITEM_ID)
        for playlist in db_list_playlists(self._conn):
            if playlist.id == playlist_id:
                self.play_requested.emit(playlist)
                return

    def _on_playlist_context(self, pos) -> None:
        item = self._playlist_list.itemAt(pos)
        if not item:
            return
        self._playlist_list.setCurrentItem(item)
        playlist = self._selected_playlist()
        if not playlist:
            return
        menu = QMenu(self)
        menu.addAction(S.PLAYLIST_PLAY, lambda: self.play_requested.emit(playlist))
        menu.addAction(S.PLAYLIST_RENAME, self._rename_playlist)
        menu.addAction(S.PLAYLIST_EXPORT, self._export)
        menu.addAction(S.PLAYLIST_DELETE, self._delete_playlist)
        menu.exec(self._playlist_list.viewport().mapToGlobal(pos))

    def _move(self, direction: int) -> None:
        item_id = self._selected_item_id()
        playlist = self._current
        if item_id is None or not playlist:
            return
        for item_row in db_list_playlist_items(self._conn, playlist.id):
            if item_row.id == item_id:
                if pl_move_item(self._conn, item_row, direction):
                    self._fill_items()
                return

    def _remove_item(self) -> None:
        item_id = self._selected_item_id()
        if item_id is None:
            return
        current = self._item_list.currentItem()
        title = current.text() if current else ""
        answer = QMessageBox.question(
            self,
            S.MENU_REMOVE_FROM_PLAYLIST,
            S.PLAYLIST_ITEM_REMOVE_CONFIRM.format(title),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        db_remove_playlist_item(self._conn, item_id)
        self._fill_items()
        self.playlists_changed.emit()

    def _play_item(self, item: QListWidgetItem) -> None:
        item_id = item.data(ROLE_ITEM_ID)
        playlist = self._current
        if item_id is None or not playlist:
            return
        for item_row in db_list_playlist_items(self._conn, playlist.id):
            if item_row.id == item_id and item_row.ref_type in ("movie", "episode") and item_row.ref_id:
                self.play_ref_requested.emit(MediaRef(item_row.ref_type, item_row.ref_id))
                return

    def _play_all(self) -> None:
        playlist = self._selected_playlist()
        if playlist:
            self.play_requested.emit(playlist)

    def _export(self) -> None:
        playlist = self._selected_playlist()
        if not playlist:
            return
        formats = [
            (S.PLAYLIST_FORMAT_M3U8, ".m3u8", pl_export_m3u8),
            (S.PLAYLIST_FORMAT_M3U, ".m3u", pl_export_m3u),
            (S.PLAYLIST_FORMAT_XSPF, ".xspf", pl_export_xspf),
        ]
        menu = QMenu(self)
        for index, (label, _ext, _exporter) in enumerate(formats):
            action = menu.addAction(label)
            action.setData(index)
        chosen = menu.exec(QCursor.pos())
        if chosen is None:
            return
        _label, ext, exporter = formats[chosen.data()]
        self._export_as(playlist, ext, exporter)

    def _export_as(self, playlist: Playlist, ext: str, exporter) -> None:
        target, _filter = QFileDialog.getSaveFileName(
            self, S.PLAYLIST_EXPORT, f"{playlist.name}{ext}", f"*{ext}"
        )
        if not target:
            return
        path = Path(target)
        if path.suffix.lower() != ext:
            path = path.with_suffix(ext)
        exporter(self._conn, playlist, path)

    def import_file(self, source: Path) -> None:
        name, ok = QInputDialog.getText(self, S.PLAYLIST_IMPORT, S.PLAYLIST_NEW_NAME, text=source.stem)
        if ok and name.strip():
            pl_import_m3u8(self._conn, name.strip(), source)
            self.refresh()
            self.playlists_changed.emit()


ROLE_ACTION_ID = Qt.ItemDataRole.UserRole


class _ShortcutCaptureDialog(QDialog):
    def __init__(self, parent, action_id: str, current_key: str):
        super().__init__(parent)
        self.setWindowTitle(S.SETTINGS_SHORTCUT_CAPTURE_TITLE)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(S.SETTINGS_SHORTCUT_CAPTURE_LABEL.format(S.action_label(action_id))))
        self._edit = QKeySequenceEdit(QKeySequence(current_key))
        layout.addWidget(self._edit)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton(S.SETTINGS_OK)
        ok.clicked.connect(self.accept)
        cancel = QPushButton(S.SETTINGS_CANCEL)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def key_sequence(self) -> str:
        return self._edit.keySequence().toString()


class SettingsDialog(QDialog):
    def __init__(self, parent, cfg: dict, conn=None, scan_requested=None):
        super().__init__(parent)
        self.setWindowTitle(S.SETTINGS_TITLE)
        self.resize(720, 540)
        self._cfg = cfg
        self._conn = conn
        self._scan_requested = scan_requested
        self._shortcut_map = shortcut_map(cfg)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs, stretch=1)
        tabs.addTab(self._build_libraries(), S.SETTINGS_TAB_LIBRARIES)
        tabs.addTab(self._build_players(), S.SETTINGS_TAB_PLAYERS)
        tabs.addTab(self._build_playback(), S.SETTINGS_TAB_PLAYBACK)
        tabs.addTab(self._build_view(), S.SETTINGS_TAB_VIEW)
        tabs.addTab(self._build_shortcuts(), S.SETTINGS_TAB_SHORTCUTS)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        ok = QPushButton(S.SETTINGS_OK)
        ok.setObjectName("primary")
        ok.clicked.connect(self._on_accept)
        cancel = QPushButton(S.SETTINGS_CANCEL)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(ok)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    # -- Bibliotheken
    def _build_libraries(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._lib_table = QTableWidget(0, 4)
        self._lib_table.setHorizontalHeaderLabels(
            [S.SETTINGS_LIB_NAME, S.SETTINGS_LIB_TYPE, S.SETTINGS_LIB_PATH, S.SETTINGS_LIB_COUNT]
        )
        self._lib_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for lib in self._cfg.get("libraries", []):
            self._add_library_row(lib["name"], lib["type"], lib["path"])
        self._refresh_counts()
        layout.addWidget(self._lib_table, stretch=1)
        hint = QLabel(S.SETTINGS_LIB_TYPE_HINT)
        hint.setWordWrap(True)
        hint.setObjectName("secondary")
        layout.addWidget(hint)
        self._scan_on_start = QCheckBox(S.SETTINGS_SCAN_ON_START)
        self._scan_on_start.setChecked(bool(self._cfg.get("scan", {}).get("scan_on_start", True)))
        layout.addWidget(self._scan_on_start)
        buttons = QHBoxLayout()
        add = QPushButton(S.SETTINGS_LIB_ADD)
        add.clicked.connect(self._add_library)
        remove = QPushButton(S.SETTINGS_LIB_REMOVE)
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_library)
        scan = QPushButton(S.SETTINGS_LIB_SCAN)
        scan.clicked.connect(self._scan_selected)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(scan)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _add_library_row(self, name: str, lib_type: str, path: str) -> None:
        row = self._lib_table.rowCount()
        self._lib_table.insertRow(row)
        self._lib_table.setItem(row, 0, QTableWidgetItem(name))
        # Typ als Auswahlliste: Filme/Serien fest; „Eigene Kategorie…“ öffnet
        # einen Eingabedialog und legt die getippte Kategorie als weiteren
        # Eintrag an (z. B. „Doku“, „Comedy“).
        type_combo = QComboBox()
        type_combo.addItem(S.SETTINGS_LIB_TYPE_MOVIES, "movies")
        type_combo.addItem(S.SETTINGS_LIB_TYPE_SHOWS, "shows")
        normalized = normalize_library_type(lib_type)
        if normalized not in ("movies", "shows"):
            type_combo.addItem(normalized, normalized)
        type_combo.addItem(S.SETTINGS_LIB_TYPE_CUSTOM, S.SETTINGS_LIB_TYPE_CUSTOM_DATA)
        type_combo.setProperty("last_type", normalized)
        type_combo.setCurrentIndex(max(type_combo.findData(normalized), 0))
        type_combo.currentIndexChanged.connect(self._on_type_changed)
        self._lib_table.setCellWidget(row, 1, type_combo)
        self._lib_table.setItem(row, 2, QTableWidgetItem(path))
        self._lib_table.setItem(row, 3, QTableWidgetItem("—"))

    def _on_type_changed(self, _index: int = 0) -> None:
        combo = self.sender()
        row = self._lib_table.indexAt(combo.pos()).row() if combo is not None else -1
        if combo is None or row < 0:
            return
        if combo.currentData() != S.SETTINGS_LIB_TYPE_CUSTOM_DATA:
            combo.setProperty("last_type", combo.currentData() or normalize_library_type(combo.currentText()))
            self._refresh_counts()
            return
        # „Eigene Kategorie…“ gewählt: Namen abfragen und als Auswahl eintragen.
        name, ok = QInputDialog.getText(
            self, S.SETTINGS_LIB_TYPE_CUSTOM_TITLE, S.SETTINGS_LIB_TYPE_CUSTOM_LABEL
        )
        category = normalize_library_type(name) if ok and name.strip() else ""
        if not category or category in ("movies", "shows"):
            previous = combo.findData(combo.property("last_type") or "movies")
            combo.blockSignals(True)
            combo.setCurrentIndex(max(previous, 0))
            combo.blockSignals(False)
            return
        existing = combo.findData(category)
        if existing < 0:
            combo.blockSignals(True)
            combo.insertItem(combo.count() - 1, category, category)
            combo.blockSignals(False)
            existing = combo.findData(category)
        combo.blockSignals(True)
        combo.setCurrentIndex(existing)
        combo.blockSignals(False)
        combo.setProperty("last_type", category)
        self._refresh_counts()

    def _library_type(self, row: int) -> str:
        combo = self._lib_table.cellWidget(row, 1)
        if combo is None:
            return "movies"
        data = combo.currentData()
        if data == S.SETTINGS_LIB_TYPE_CUSTOM_DATA:
            return "movies"  # Dialog offen/abgebrochen — nichts persistieren
        return normalize_library_type(combo.currentText())

    def _add_library(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, S.SETTINGS_LIB_PICK_TITLE)
        if not folder:
            return
        name, ok = QInputDialog.getText(self, S.SETTINGS_LIB_ADD, S.SETTINGS_LIB_NAME)
        if ok and name.strip():
            self._add_library_row(name.strip(), "movies", folder)
        self._refresh_counts()

    def _remove_library(self) -> None:
        row = self._lib_table.currentRow()
        if row < 0:
            return
        name_item = self._lib_table.item(row, 0)
        name = name_item.text() if name_item else ""
        answer = QMessageBox.question(
            self,
            S.SETTINGS_LIB_REMOVE,
            S.SETTINGS_LIB_REMOVE_CONFIRM.format(name or "?"),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._lib_table.removeRow(row)
        self._refresh_counts()

    def _scan_selected(self) -> None:
        if not self._scan_requested:
            return
        row = self._lib_table.currentRow()
        self._write_back_libraries()
        path = self._lib_table.item(row, 2).text() if row >= 0 else ""
        self._scan_requested(path, self._refresh_counts_if_visible)
        self._refresh_counts()

    def _refresh_counts_if_visible(self) -> None:
        """Zähler nach abgeschlossenem Scan auffrischen (Dialog evtl. schon zu)."""
        if self.isVisible():
            self._refresh_counts()

    def _refresh_counts(self) -> None:
        """Anzahl je Bibliothek — passend zum Bibliothekstyp (Filme → Filme,
        Serien → Serien, eigene Kategorie → Einträge des Typs)."""
        if self._conn is None:
            return
        for row in range(self._lib_table.rowCount()):
            path_item = self._lib_table.item(row, 2)
            count_item = self._lib_table.item(row, 3)
            if not path_item or not count_item:
                continue
            lib_type = self._library_type(row)
            movies, shows = db_count_library_items(self._conn, Path(path_item.text()))
            if lib_type == "shows":
                count_item.setText(S.COUNT_SHOWS.format(shows))
            elif lib_type == "movies":
                count_item.setText(S.COUNT_MOVIES.format(movies))
            else:
                count_item.setText(S.COUNT_ITEMS.format(movies))

    def _write_back_libraries(self) -> None:
        libraries = []
        for row in range(self._lib_table.rowCount()):
            name_item = self._lib_table.item(row, 0)
            path_item = self._lib_table.item(row, 2)
            name = name_item.text().strip() if name_item else ""
            lib_type = self._library_type(row)
            path = path_item.text().strip() if path_item else ""
            if name and path:
                libraries.append({"name": name, "type": lib_type, "path": path})
        self._cfg["libraries"] = libraries

    # -- Player
    def _build_players(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._player_table = QTableWidget(0, 4)
        self._player_table.setHorizontalHeaderLabels(
            [
                S.SETTINGS_PLAYER_NAME,
                S.SETTINGS_PLAYER_PATH,
                S.SETTINGS_PLAYER_ARGS,
                S.SETTINGS_PLAYER_DEFAULT,
            ]
        )
        self._player_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for player in self._cfg.get("players", []):
            self._add_player_row(player)
        layout.addWidget(self._player_table, stretch=1)
        buttons = QHBoxLayout()
        add = QPushButton(S.SETTINGS_PLAYER_ADD)
        add.clicked.connect(
            lambda: self._add_player_row({"name": "Player", "path": "", "args": ["{file}"], "default": False})
        )
        remove = QPushButton(S.SETTINGS_PLAYER_REMOVE)
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_player)
        autodetect = QPushButton(S.SETTINGS_PLAYER_AUTODETECT)
        autodetect.clicked.connect(self._autodetect_players)
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addWidget(autodetect)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return page

    def _add_player_row(self, player: dict) -> None:
        row = self._player_table.rowCount()
        self._player_table.insertRow(row)
        self._player_table.setItem(row, 0, QTableWidgetItem(player.get("name", "")))
        self._player_table.setItem(row, 1, QTableWidgetItem(player.get("path", "")))
        self._player_table.setItem(row, 2, QTableWidgetItem(" ".join(player.get("args", ["{file}"]))))
        default = QTableWidgetItem()
        default.setCheckState(Qt.CheckState.Checked if player.get("default") else Qt.CheckState.Unchecked)
        default.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        self._player_table.setItem(row, 3, default)

    def _remove_player(self) -> None:
        row = self._player_table.currentRow()
        if row >= 0:
            self._player_table.removeRow(row)

    def _table_paths(self) -> list[str]:
        return [
            self._player_table.item(row, 1).text()
            for row in range(self._player_table.rowCount())
            if self._player_table.item(row, 1)
        ]

    def _autodetect_players(self) -> None:
        found = []
        for detector in (_detect_vlc, _detect_mpv):
            player = detector()
            if not player:
                continue
            if player["path"] in self._table_paths():
                continue
            self._add_player_row(player)
            found.append(f"{player['name']} ({player['path']})")
        if found:
            QMessageBox.information(
                self, S.SETTINGS_PLAYER_AUTODETECT, S.SETTINGS_PLAYER_AUTODETECT_DONE.format("\n".join(found))
            )
        else:
            QMessageBox.information(self, S.SETTINGS_PLAYER_AUTODETECT, S.SETTINGS_PLAYER_AUTODETECT_NONE)

    def _write_back_players(self) -> None:
        players = []
        seen_default = False
        for row in range(self._player_table.rowCount()):
            name_item = self._player_table.item(row, 0)
            path_item = self._player_table.item(row, 1)
            args_item = self._player_table.item(row, 2)
            default_item = self._player_table.item(row, 3)
            name = name_item.text().strip() if name_item else ""
            path = path_item.text().strip() if path_item else ""
            args_text = args_item.text() if args_item else "{file}"
            is_default = default_item.checkState() == Qt.CheckState.Checked if default_item else False
            if name and path:
                if is_default and seen_default:
                    is_default = False
                elif is_default:
                    seen_default = True
                args = [arg for arg in args_text.split() if arg] or ["{file}"]
                players.append({"name": name, "path": path, "args": args, "default": is_default})
        if players and not seen_default:
            players[0]["default"] = True
        self._cfg["players"] = players

    # -- Wiedergabe
    def _build_playback(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        playback = self._cfg.get("playback", {})
        self._fullscreen = QCheckBox(S.SETTINGS_PLAYBACK_FULLSCREEN)
        self._fullscreen.setChecked(bool(playback.get("fullscreen", False)))
        layout.addWidget(self._fullscreen)
        row = QHBoxLayout()
        row.addWidget(QLabel(S.SETTINGS_PLAYBACK_YOUTUBE))
        self._youtube = QComboBox()
        self._youtube.addItem(S.SETTINGS_YOUTUBE_BROWSER, "browser")
        self._youtube.addItem(S.SETTINGS_YOUTUBE_VLC, "vlc")
        self._youtube.setCurrentIndex(1 if playback.get("youtube_trailer") == "vlc" else 0)
        row.addWidget(self._youtube)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    # -- Ansicht
    def _build_view(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        ui = self._cfg.get("ui", {})
        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel(S.SETTINGS_VIEW_THEME))
        self._theme_combo = QComboBox()
        for theme in VALID_THEMES:
            self._theme_combo.addItem(theme, theme)
        current = ui.get("theme", "darkskin")
        self._theme_combo.setCurrentIndex(
            VALID_THEMES.index(current if current in VALID_THEMES else "darkskin")
        )
        theme_row.addWidget(self._theme_combo)
        theme_row.addStretch(1)
        layout.addLayout(theme_row)
        width_row = QHBoxLayout()
        width_row.addWidget(QLabel(S.SETTINGS_VIEW_POSTER_WIDTH))
        self._poster_width_combo = QComboBox()
        for label, width in S.SETTINGS_POSTER_LEVELS:
            self._poster_width_combo.addItem(label, width)
        current_width = int(ui.get("poster_width", 200))
        # Alte Freiwerte auf die nächste Stufe schnappen (Reihenfolge wie Levels).
        levels = [w for _label, w in S.SETTINGS_POSTER_LEVELS]
        nearest = min(levels, key=lambda w: (abs(w - current_width), -w))
        self._poster_width_combo.setCurrentIndex(self._poster_width_combo.findData(nearest))
        width_row.addWidget(self._poster_width_combo)
        width_row.addStretch(1)
        layout.addLayout(width_row)
        self._label_check = QCheckBox(S.SETTINGS_VIEW_LABEL)
        self._label_check.setChecked(bool(ui.get("label_under_poster", True)))
        layout.addWidget(self._label_check)
        layout.addStretch(1)
        return page

    # -- Tastenkürzel
    def _build_shortcuts(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        hint = QLabel(S.SETTINGS_SHORTCUT_HINT)
        hint.setObjectName("secondary")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._shortcut_tree = QTreeWidget()
        self._shortcut_tree.setColumnCount(2)
        self._shortcut_tree.setHeaderLabels([S.SETTINGS_SHORTCUT_ACTION, S.SETTINGS_SHORTCUT_KEY])
        self._shortcut_tree.setRootIsDecorated(False)
        self._shortcut_tree.setAlternatingRowColors(True)
        self._shortcut_tree.itemDoubleClicked.connect(self._edit_shortcut)
        layout.addWidget(self._shortcut_tree, stretch=1)
        self._fill_shortcuts()
        reset = QPushButton(S.SETTINGS_SHORTCUT_RESET)
        reset.clicked.connect(self._reset_shortcuts)
        layout.addWidget(reset)
        return page

    def _fill_shortcuts(self) -> None:
        self._shortcut_tree.clear()
        for action_id, default_key in SHORTCUT_ACTIONS.items():
            item = QTreeWidgetItem(
                [S.action_label(action_id), self._shortcut_map.get(action_id, default_key)]
            )
            item.setData(0, ROLE_ACTION_ID, action_id)
            self._shortcut_tree.addTopLevelItem(item)

    def _edit_shortcut(self, item: QTreeWidgetItem, _column: int) -> None:
        action_id = item.data(0, ROLE_ACTION_ID)
        dialog = _ShortcutCaptureDialog(self, action_id, self._shortcut_map.get(action_id, ""))
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.key_sequence():
            return
        new_key = dialog.key_sequence()
        conflict = find_conflict(self._shortcut_map, action_id, new_key)
        if conflict:
            QMessageBox.warning(
                self,
                S.SETTINGS_SHORTCUT_CONFLICT.split("\n")[0],
                S.SETTINGS_SHORTCUT_CONFLICT.format(S.action_label(conflict)),
            )
            return
        self._shortcut_map[action_id] = str(QKeySequence(new_key).toString())
        self._fill_shortcuts()

    def _reset_shortcuts(self) -> None:
        self._shortcut_map = dict(SHORTCUT_ACTIONS)
        self._fill_shortcuts()

    # -- Speichern
    def _on_accept(self) -> None:
        self._write_back_libraries()
        self._write_back_players()
        self._cfg["playback"] = {
            "fullscreen": self._fullscreen.isChecked(),
            "youtube_trailer": self._youtube.currentData() or "browser",
        }
        self._cfg["ui"] = {
            "theme": self._theme_combo.currentData() or "darkskin",
            "poster_width": int(self._poster_width_combo.currentData() or 200),
            "label_under_poster": self._label_check.isChecked(),
        }
        self._cfg["shortcuts"] = dict(self._shortcut_map)
        self._cfg["scan"] = {"scan_on_start": self._scan_on_start.isChecked()}
        save_settings(self._cfg)
        self.accept()


PAGE_MOVIES, PAGE_SHOWS, PAGE_PLAYLISTS = 0, 1, 2


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_settings()
        self.conn = db_connect(data_dir() / "library.db")
        self._scan_worker: ScanWorker | None = None
        self._scan_dialog: QProgressDialog | None = None
        self._scan_finished_hook = None
        self._thumb_worker: ThumbnailWorker | None = None
        self._pending_thumbs: dict[object, tuple] = {}
        self._thumb_icons: dict[MediaRef, tuple[tuple, QIcon]] = {}
        self._thumb_grid_for: dict[MediaRef, object] = {}
        self._thumb_size: tuple[int, str] | None = None  # (Breite, Theme) des laufenden Workers
        self._genre_filter = ""
        # Genre-Zähler je Ansicht, EINMAL aus der DB geladen (Scan/Refresh);
        # Seitenwechsel bauen das Menü daraus ohne weiteren DB-Zugriff.
        self._genre_cache: dict[tuple[str, str | None], list[tuple[str, int]]] = {}

        self.setWindowTitle(S.APP_TITLE)
        if not self._restore_window_geometry():
            self.resize(1280, 800)
        self._build_ui()
        self._apply_theme()
        self._register_shortcuts()
        self._prune_orphans()
        self.refresh_all()
        if self.cfg.get("scan", {}).get("scan_on_start", True):
            self._start_scan()

    def _prune_orphans(self) -> None:
        """Einträge ohne konfigurierte Bibliothek aus der DB nehmen
        (z. B. nach „Bibliothek entfernen" in den Einstellungen) — nur DB."""
        removed = db_prune_outside_libraries(self.conn, self.cfg.get("libraries", []))
        if removed:
            logger.info("%d Einträge außerhalb konfigurierter Bibliotheken entfernt", removed)
            self.statusBar().showMessage(S.STATUS_ORPHANS_REMOVED.format(removed), 8000)

    # -- UI
    def _theme_name(self) -> str:
        return self.cfg.get("ui", {}).get("theme", "darkskin")

    def _poster_width(self) -> int:
        return int(self.cfg.get("ui", {}).get("poster_width", 200))

    def _label_under_poster(self) -> bool:
        return bool(self.cfg.get("ui", {}).get("label_under_poster", True))

    def _build_ui(self) -> None:
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.setCentralWidget(central)

        self._nav = QListWidget()
        self._nav.setFixedWidth(200)
        self._category_pages: dict[str, dict] = {}
        self._nav_entries: list[tuple[str, object]] = []
        self._nav.currentRowChanged.connect(self._on_nav)
        side = QWidget()
        side_layout = QVBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 8)
        side_layout.addWidget(self._nav, stretch=1)
        self._theme_button = QPushButton()
        self._theme_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._theme_button.clicked.connect(self._cycle_theme)
        side_layout.addWidget(self._theme_button)
        root.addWidget(side)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)
        root.addWidget(content, stretch=1)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(12, 8, 12, 8)
        self._search = QLineEdit()
        self._search.setPlaceholderText(S.SEARCH_PLACEHOLDER)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search_changed)
        self._search.setMaximumWidth(280)
        self._genre_btn = QPushButton(S.FILTER_GENRE)
        self._genre_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._genre_menu = QMenu(self._genre_btn)
        self._genre_btn.setMenu(self._genre_menu)
        header_layout.addStretch(1)
        header_layout.addWidget(self._search)
        header_layout.addWidget(self._genre_btn)
        self._sort = QComboBox()
        for label, mode in ((S.SORT_TITLE, SORT_TITLE), (S.SORT_YEAR, SORT_YEAR)):
            self._sort.addItem(label, mode)
        self._sort.currentIndexChanged.connect(self._on_sort)
        header_layout.addWidget(self._sort)
        self._view_toggle = QPushButton(S.VIEW_LIST)
        self._view_toggle.clicked.connect(self._toggle_view)
        header_layout.addWidget(self._view_toggle)
        self._random_button = QPushButton(S.RANDOM_PLAY)
        self._random_button.setToolTip(S.RANDOM_TOOLTIP)
        self._random_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._random_button.clicked.connect(self._random_play)
        header_layout.addWidget(self._random_button)
        content_layout.addWidget(header)

        self._stack = QStackedWidget()
        content_layout.addWidget(self._stack, stretch=1)

        movies_page = QWidget()
        movies_layout = QVBoxLayout(movies_page)
        movies_layout.setContentsMargins(8, 0, 8, 0)
        self._movie_grid = PosterGrid(self._poster_width(), self._label_under_poster())
        self._movie_grid.play_requested.connect(self.play_ref)
        self._movie_grid.context_requested.connect(self._show_context_menu)
        self._movie_list = MovieListView()
        self._movie_list.play_requested.connect(self.play_ref)
        self._movie_list.context_requested.connect(self._show_context_menu)
        self._movie_list.hide()
        movies_layout.addWidget(self._movie_grid)
        movies_layout.addWidget(self._movie_list)
        self._stack.addWidget(movies_page)

        self._series_view = SeriesView(
            self.conn,
            get_theme(self._theme_name()),
            self._theme_name(),
            poster_width=self._poster_width(),
            label_under_poster=self._label_under_poster(),
        )
        self._series_view.play_requested.connect(self.play_ref)
        self._series_view.context_requested.connect(self._show_context_menu)
        self._stack.addWidget(self._series_view)

        self._playlist_view = PlaylistView(self.conn)
        self._playlist_view.play_requested.connect(self._play_playlist)
        self._playlist_view.play_ref_requested.connect(self.play_ref)
        self._stack.addWidget(self._playlist_view)

        # Eigene Kategorien (z. B. „Doku“): je eine Posterwand-Seite + Nav-Eintrag.
        self._rebuild_category_pages()

        self.statusBar().showMessage("")
        # Zähleranzeige rechts in der Kopfzeile, passend zur aktuellen Ansicht.
        self._counts_label = QLabel("")
        self._counts_label.setObjectName("secondary")
        header_layout.addWidget(self._counts_label)
        self._nav.setCurrentRow(0)

    def _update_theme_button(self) -> None:
        self._theme_button.setText(S.NAV_THEME_BUTTON.format(self._theme_name().capitalize()))

    def _cycle_theme(self) -> None:
        names = list(THEMES)
        next_name = names[(names.index(self._theme_name()) + 1) % len(names)]
        self.cfg.setdefault("ui", {})["theme"] = next_name
        save_settings(self.cfg)
        self._apply_theme()
        self.refresh_all()
        self.statusBar().showMessage(S.STATUS_THEME_CHANGED.format(next_name.capitalize()), 5000)
        logger.info("Theme gewechselt: %s", next_name)

    def _apply_theme(self) -> None:
        QApplication.instance().setStyleSheet(get_stylesheet(self._theme_name()))
        self._update_theme_button()

    def _register_shortcuts(self) -> None:
        mapping = shortcut_map(self.cfg)
        self._shortcuts = {
            "play": register_shortcut(self, "play", mapping["play"], self._on_play_key),
            "details": register_shortcut(self, "details", mapping["details"], self._on_details_key),
            "trailer": register_shortcut(self, "trailer", mapping["trailer"], self._on_trailer_key),
            "search": register_shortcut(self, "search", mapping["search"], self._on_search_key),
            "scan": register_shortcut(self, "scan", mapping["scan"], self._start_scan),
            "back": register_shortcut(self, "back", mapping["back"], self._on_back_key),
            "playlist_remove": register_shortcut(
                self, "playlist_remove", mapping["playlist_remove"], self._on_playlist_remove_key
            ),
        }

    # -- Navigation
    def _custom_categories(self) -> list[str]:
        """Eigene Bibliothekskategorien aus den Einstellungen (Reihenfolge wie konfiguriert)."""
        categories: list[str] = []
        seen: set[str] = set()
        for lib in self.cfg.get("libraries", []):
            lib_type = normalize_library_type(lib.get("type", "movies"))
            if lib_type in ("movies", "shows"):
                continue
            key = lib_type.casefold()
            if key not in seen:
                seen.add(key)
                categories.append(lib_type)
        return categories

    def _rebuild_category_pages(self) -> None:
        """Pro eigener Kategorie eine Posterwand-Seite erzeugen (oder entfernen),
        danach die Navigation neu aufbauen. Nach Einstellungs-Änderungen aufrufen."""
        for page in getattr(self, "_category_pages", {}).values():
            widget = self._stack.widget(page["page"])
            if widget is not None:
                self._stack.removeWidget(widget)
                widget.deleteLater()
        self._category_pages = {}
        for name in self._custom_categories():
            grid = PosterGrid(self._poster_width(), self._label_under_poster())
            grid.play_requested.connect(self.play_ref)
            grid.context_requested.connect(self._show_context_menu)
            mlist = MovieListView()
            mlist.play_requested.connect(self.play_ref)
            mlist.context_requested.connect(self._show_context_menu)
            mlist.hide()
            page_widget = QWidget()
            page_layout = QVBoxLayout(page_widget)
            page_layout.setContentsMargins(8, 0, 8, 0)
            page_layout.addWidget(grid)
            page_layout.addWidget(mlist)
            self._category_pages[name] = {
                "grid": grid,
                "list": mlist,
                "page": self._stack.addWidget(page_widget),
            }
        self._refresh_genre_cache()
        self._rebuild_nav()

    def _rebuild_nav(self) -> None:
        self._nav.blockSignals(True)
        self._nav.clear()
        self._nav_entries = [("page", PAGE_MOVIES), ("page", PAGE_SHOWS)]
        self._nav.addItem(QListWidgetItem(S.NAV_MOVIES))
        self._nav.addItem(QListWidgetItem(S.NAV_SHOWS))
        for name, page in self._category_pages.items():
            self._nav.addItem(QListWidgetItem(name))
            self._nav_entries.append(("page", page["page"]))
        self._nav.addItem(QListWidgetItem(S.NAV_PLAYLISTS))
        self._nav_entries.append(("page", PAGE_PLAYLISTS))
        self._nav.addItem(QListWidgetItem(S.NAV_SETTINGS))
        self._nav_entries.append(("settings", None))
        self._nav.blockSignals(False)

    def _nav_row_for_page(self, page_index: int) -> int:
        for row, (kind, page) in enumerate(getattr(self, "_nav_entries", [])):
            if kind == "page" and page == page_index:
                return row
        return 0

    def _current_grid_and_list(self) -> tuple[PosterGrid | None, MovieListView | None]:
        """Posterwand+Liste der aktuellen Seite (Filme oder eigene Kategorie)."""
        page = self._stack.currentIndex()
        if page == PAGE_MOVIES:
            return self._movie_grid, self._movie_list
        for page_info in getattr(self, "_category_pages", {}).values():
            if page_info["page"] == page:
                return page_info["grid"], page_info["list"]
        return None, None

    def _on_nav(self, row: int) -> None:
        entries = getattr(self, "_nav_entries", [])
        if not 0 <= row < len(entries):
            return
        kind, page = entries[row]
        if kind == "settings":
            # Auswahl auf die aktuelle Seite zurücksetzen und den Dialog öffnen.
            self._nav.setCurrentRow(self._nav_row_for_page(self._stack.currentIndex()))
            self._open_settings()
            return
        self._stack.setCurrentIndex(page)
        self._update_counts()
        self._rebuild_genre_menu()  # aus dem Cache — kein DB-Zugriff pro Wechsel

    def _on_search_changed(self, text: str) -> None:
        self._movie_grid.set_search(text)
        self._movie_list.set_search(text)
        self._series_view.set_search(text)
        for page in self._category_pages.values():
            page["grid"].set_search(text)
            page["list"].set_search(text)

    def _on_sort(self) -> None:
        mode = self._sort.currentData()
        self._movie_grid.set_sort_mode(mode)
        self._series_view.set_sort_mode(mode)
        for page in self._category_pages.values():
            page["grid"].set_sort_mode(mode)
        if mode == SORT_YEAR:
            self._movie_list.sortByColumn(1, Qt.SortOrder.AscendingOrder)
            for page in self._category_pages.values():
                page["list"].sortByColumn(1, Qt.SortOrder.AscendingOrder)
        else:
            self._movie_list.sortByColumn(0, Qt.SortOrder.AscendingOrder)
            for page in self._category_pages.values():
                page["list"].sortByColumn(0, Qt.SortOrder.AscendingOrder)

    # -- Genre-Filter
    def _genre_scope(self) -> tuple[str, str | None]:
        """Welche Genres die aktuelle Seite zeigt: Filme der Filme-Seite (Kategorie
        „movies“), Filme einer eigenen Kategorie oder Serien; sonst alles."""
        page = self._stack.currentIndex()
        if page == PAGE_SHOWS:
            return "shows", None
        if page == PAGE_MOVIES:
            return "movies", "movies"
        for name, info in getattr(self, "_category_pages", {}).items():
            if info["page"] == page:
                return "movies", name
        return "all", None

    def _refresh_genre_cache(self) -> None:
        """Genre-Zähler je Ansicht EINMAL aus der DB holen — beim Scan/Refresh,
        nicht beim Seitenwechsel (der baut das Menü nur aus diesem Cache)."""
        cache: dict[tuple[str, str | None], list[tuple[str, int]]] = {
            ("movies", "movies"): db_genres_with_counts(self.conn, "movies", "movies"),
            ("shows", None): db_genres_with_counts(self.conn, "shows"),
            ("all", None): db_genres_with_counts(self.conn),
        }
        for name in getattr(self, "_category_pages", {}):
            cache[("movies", name)] = db_genres_with_counts(self.conn, "movies", name)
        self._genre_cache = cache

    def _available_genres(self) -> list[str]:
        """Alle Genres aus den gescannten NFO-Metadaten (Filme + Serien)."""
        return [name for name, _count in db_genres_with_counts(self.conn)]

    def _rebuild_genre_menu(self) -> None:
        menu = self._genre_menu
        menu.clear()
        current = self._genre_filter.casefold()
        act_all = menu.addAction(S.FILTER_GENRE_ALL)
        act_all.setCheckable(True)
        act_all.setChecked(not current)
        act_all.triggered.connect(lambda _checked=False: self._set_genre_filter(""))
        menu.addSeparator()
        kind, category = self._genre_scope()
        for genre, count in self._genre_cache.get((kind, category), []):
            act = menu.addAction(S.FILTER_GENRE_ENTRY.format(genre, count))
            act.setCheckable(True)
            act.setChecked(genre.casefold() == current)
            act.triggered.connect(lambda _checked=False, g=genre: self._set_genre_filter(g))
        self._genre_btn.setText(
            S.FILTER_GENRE_LABEL.format(self._genre_filter) if self._genre_filter else S.FILTER_GENRE
        )

    def _set_genre_filter(self, genre: str) -> None:
        self._genre_filter = genre
        self._movie_grid.set_genre_filter(genre)
        self._movie_list.set_genre_filter(genre)
        self._series_view.set_genre_filter(genre)
        for page in self._category_pages.values():
            page["grid"].set_genre_filter(genre)
            page["list"].set_genre_filter(genre)
        self._rebuild_genre_menu()
        if genre:
            self.statusBar().showMessage(S.STATUS_GENRE_FILTER.format(genre), 5000)

    def _toggle_view(self) -> None:
        if self._stack.currentIndex() == PAGE_SHOWS:
            # Serienansicht: Posterwand ↔ Liste (innerhalb der Serienansicht).
            list_visible = self._series_view.toggle_shows_view()
            self._view_toggle.setText(S.VIEW_POSTERS if list_visible else S.VIEW_LIST)
            return
        grid, mlist = self._current_grid_and_list()
        if grid is None:
            return
        # isHidden() statt isVisible(): expliziter Hide-Zustand, unabhängig davon,
        # ob das Fenster gerade sichtbar ist (funktioniert auch offscreen).
        grid_hidden = grid.isHidden()
        grid.setVisible(grid_hidden)
        mlist.setVisible(not grid_hidden)
        self._view_toggle.setText(S.VIEW_LIST if grid_hidden else S.VIEW_POSTERS)

    def _random_play(self) -> None:
        """Zufällige Wiedergabe: Film/Kategorie aus den GEFILTERTEN Einträgen der
        aktuellen Seite; Serienansicht: zufällige Episode (aktuelle Serie/Staffel
        bzw. aus den gefilterten Serien)."""
        grid, _mlist = self._current_grid_and_list()
        if grid is not None:
            refs = [
                ref
                for row in range(grid._proxy.rowCount())
                if (ref := grid._proxy.index(row, 0).data(ROLE_REF)) is not None
            ]
            random.shuffle(refs)
            for ref in refs:
                movie, _episode, _show = self._load_ref(ref)
                if movie is not None and not movie.missing:
                    self.play_ref(ref)
                    return
            self.statusBar().showMessage(S.STATUS_RANDOM_EMPTY, 5000)
            return
        if self._stack.currentIndex() == PAGE_SHOWS:
            ref = self._series_view.random_episode_ref()
            if ref is not None:
                self.play_ref(ref)
            else:
                self.statusBar().showMessage(S.STATUS_RANDOM_EMPTY, 5000)

    # -- Daten
    def refresh_all(self) -> None:
        self.refresh_movies()
        self._series_view.set_shows(db_list_shows(self.conn))
        self._playlist_view.refresh()
        self._refresh_categories()
        self._update_counts()
        self._rebuild_genre_menu()
        self._prune_thumb_caches()

    def _prune_thumb_caches(self) -> None:
        """Thumbnail-Caches auf die aktuell existierenden Film-Refs begrenzen
        (Speicher); läuft am Ende von refresh_all, NACH allen Seiten-Updates."""
        valid: set[MediaRef] = {MediaRef("movie", m.id) for m in db_list_movies(self.conn)}
        self._thumb_icons = {r: v for r, v in self._thumb_icons.items() if r in valid}
        self._thumb_grid_for = {r: g for r, g in self._thumb_grid_for.items() if r in valid}
        self._pending_thumbs = {r: m for r, m in self._pending_thumbs.items() if r in valid}

    def _refresh_categories(self) -> None:
        """Posterwände der eigenen Kategorien aus der DB füllen."""
        for name, page in self._category_pages.items():
            movies = db_list_movies(self.conn, category=name)
            items = self._movies_to_items(movies)
            page["grid"].set_items(items)
            page["list"].set_movies(movies)
            self._queue_thumbnails(items, page["grid"])

    @staticmethod
    def _movies_to_items(movies) -> list[PosterItem]:
        return [
            PosterItem(
                ref=MediaRef("movie", movie.id),
                title=movie.title,
                poster_path=movie.poster_path,
                missing=movie.missing,
                year=movie.year,
                date_added=movie.date_added,
                genres=movie.genres,
                video_path=movie.video_path,
            )
            for movie in movies
        ]

    def _update_counts(self) -> None:
        """Kopfzeile rechts: Anzahl passend zur aktuellen Ansicht.

        Filme-Ansicht → Anzahl vorhandener Filme; Serien-Ansicht → Anzahl
        vorhandener Serien(-Ordner); Kategorie-Ansicht → Anzahl Einträge.
        """
        page = self._stack.currentIndex()
        if page == PAGE_MOVIES:
            # Haupt-Filme-Seite zeigt nur den Typ „movies“ — eigene Kategorien
            # (z. B. „Doku“) haben ihre eigene Seite, keine Doppelanzeige.
            self._counts_label.setText(S.COUNT_MOVIES.format(db_count_movies(self.conn, category="movies")))
        elif page == PAGE_SHOWS:
            self._counts_label.setText(S.COUNT_SHOWS.format(db_count_shows(self.conn)))
        else:
            for name, page_info in self._category_pages.items():
                if page_info["page"] == page:
                    count = len(db_list_movies(self.conn, category=name))
                    self._counts_label.setText(S.COUNT_ITEMS.format(count))
                    break
            else:
                self._counts_label.setText("")

    def refresh_movies(self) -> None:
        # Haupt-Filme-Seite = nur Kategorie „movies“; eigene Kategorien füllen
        # ihre eigenen Seiten (_refresh_categories) — keine Doppelanzeige.
        movies = db_list_movies(self.conn, category="movies")
        items = self._movies_to_items(movies)
        self._movie_grid.set_items(items)
        self._movie_list.set_movies(movies)
        self._queue_thumbnails(items, self._movie_grid)
        self._refresh_genre_cache()
        self._rebuild_genre_menu()

    def _queue_thumbnails(self, items: list[PosterItem], grid) -> None:
        width, theme = self._poster_width(), self._theme_name()
        # Bereits gerenderte Icons wiederverwenden: Verglichen wird über reine
        # DB-Werte (Quellpfad, Breite, missing, Titel/Theme nur für Platzhalter)
        # — ohne Dateizugriff. Ein Skin-Wechsel ändert die Marker echter Poster
        # nicht → keine Worker-Runde, kein Neu laden.
        for item in items:
            self._thumb_grid_for[item.ref] = grid
        reusable: dict[MediaRef, QIcon] = {}
        to_submit: list[PosterItem] = []
        for item in items:
            marker = self._thumb_marker(item, width, theme)
            cached = self._thumb_icons.get(item.ref)
            if cached is not None and cached[0] == marker:
                reusable[item.ref] = cached[1]
            else:
                to_submit.append(item)
        # Kein Prune hier: refresh_all stößt diese Methode mehrfach an (Filme +
        # jede Kategorie) — ein Prune pro Aufruf würde die Icons/Zuordnung der
        # anderen Seiten wegwerfen. Aufgeräumt wird gesammelt in _prune_thumb_caches.
        grid.set_icons(reusable)
        if not to_submit:
            return
        # Worker WIEDERVERWENDEN, solange er läuft und Größe/Theme unverändert
        # sind: refresh_all stößt _queue_thumbnails mehrfach an (Filme-Seite +
        # jede Kategorie-Seite). Stop+Neustart hier hätte die noch wartenden
        # Poster der vorherigen Seiten verworfen — die Poster verschwanden nach
        # jedem Scan (F5) und kamen erst nach „Metadaten neu einlesen" zurück.
        if (
            self._thumb_worker is not None
            and self._thumb_worker.isRunning()
            and self._thumb_size == (width, theme)
        ):
            worker = self._thumb_worker
        else:
            if self._thumb_worker is not None:
                self._thumb_worker.stop()
                self._thumb_worker.wait(300)
            worker = ThumbnailWorker(cache_dir(), get_theme(theme), theme, self)
            worker.ready.connect(self._on_thumb_ready)
            worker.start()
            self._thumb_worker = worker
            self._thumb_size = (width, theme)
        # _pending_thumbs NICHT leeren: späte Ergebnisse vorheriger Seiten müssen
        # noch ansetzen dürfen (Zuordnung kommt je PostItem hier).
        for item in to_submit:
            self._pending_thumbs[item.ref] = self._thumb_marker(item, width, theme)
            worker.submit(
                item.ref,
                "movie",
                item.ref.ref_id,
                item.poster_path,
                item.title,
                width,
                item.missing,
                video=item.video_path,
            )

    @staticmethod
    def _thumb_marker(item: PosterItem, width: int, theme: str) -> tuple:
        """Vergleichsmarker für den Icon-Speicher-Cache — nur DB-Werte, kein IO."""
        if item.poster_path:
            return (item.poster_path, width, item.missing)
        if item.video_path:
            return ("vid", item.video_path, width, item.missing)
        return ("", item.title, width, item.missing, theme)

    def _on_thumb_ready(self, ref: object, path: str) -> None:
        marker = self._pending_thumbs.pop(ref, None)
        if marker is None or not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            return
        target = self._movie_grid.iconSize()
        scaled = (
            pixmap
            if pixmap.size() == target
            else pixmap.scaled(
                target,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        icon = QIcon(scaled)
        self._thumb_icons[ref] = (marker, icon)
        target_grid = self._thumb_grid_for.get(ref, self._movie_grid)
        target_grid.set_icon(ref, icon)

    # -- Scan
    def _start_scan(self) -> None:
        self._run_scan(self.cfg.get("libraries", []))

    def _run_scan(self, libraries: list[dict], on_finished=None) -> None:
        """Scan mit Fortschrittsdialog — auch aus dem Einstellungen-Dialog heraus."""
        if not libraries:
            self.statusBar().showMessage(S.SCAN_NO_LIBRARIES, 8000)
            return
        if self._scan_worker is not None and self._scan_worker.isRunning():
            return
        worker = ScanWorker(data_dir() / "library.db", libraries, self)
        worker.progress.connect(self._on_scan_progress)
        worker.finished_ok.connect(self._on_scan_finished)
        worker.failed.connect(self._on_scan_failed)
        self._scan_finished_hook = on_finished
        worker.start()
        self._scan_worker = worker
        dialog = QProgressDialog(S.SCAN_DIALOG_TITLE, S.SCAN_CANCEL, 0, 100, self)
        dialog.setWindowModality(Qt.WindowModality.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setValue(0)
        dialog.canceled.connect(worker.cancel)
        dialog.show()
        self._scan_dialog = dialog

    def _on_scan_progress(self, percent: int, label: str) -> None:
        dialog = self._scan_dialog
        if dialog is not None:
            dialog.setValue(max(percent, 1))
            dialog.setLabelText(S.SCAN_RUNNING.format(label, percent))
            self.statusBar().showMessage(S.SCAN_RUNNING.format(label, percent))

    def _on_scan_finished(self, totals: dict) -> None:
        # Erst None setzen: Späte Progress-Signale (Queued) müssen ignorierbar sein.
        dialog = self._scan_dialog
        self._scan_dialog = None
        if dialog is not None:
            dialog.reset()
        self.refresh_all()
        hook = self._scan_finished_hook
        self._scan_finished_hook = None
        if hook is not None:
            try:
                hook()
            except Exception:
                logger.exception("Nachlauf nach Scan fehlgeschlagen")
        message = S.SCAN_FINISHED.format(
            totals.get("updated", 0), totals.get("unchanged", 0), totals.get("missing", 0)
        )
        self.statusBar().showMessage(message, 10000)
        logger.info(message)
        for warning in totals.get("warnings", []):
            logger.warning(warning)

    def _on_scan_failed(self, error: str) -> None:
        if self._scan_dialog:
            self._scan_dialog.reset()
            self._scan_dialog = None
        self.statusBar().showMessage(S.STATUS_ERROR.format(error), 10000)
        QMessageBox.critical(self, S.SCAN_ERROR_TITLE, error)

    # -- Kontextmenü (Reihenfolge wie SKILL.md F4; „Abspielen mit ▸“ auf
    # ausdrücklichen Nutzerwunsch entfernt — es gibt nur den Standard-Player;
    # „Untertitel ▸“ ebenfalls entfernt — Untertitel wählt man im Player)
    def _show_context_menu(self, ref: MediaRef, global_pos) -> None:
        # Filme, Episoden und Serien erhalten dasselbe Kontextmenü; bei einer
        # Serie bedeuten „Abspielen“/„Zur Playlist hinzufügen“ die GANZE Serie
        # (alle Episoden in Staffel-/Episoden-Reihenfolge).
        menu = QMenu(self)
        movie, episode, show = self._load_ref(ref)

        menu.addAction(S.MENU_PLAY, lambda: self._play_ref(ref))

        trailer = movie.trailer_path if movie else None
        trailer_action = menu.addAction(S.MENU_TRAILER, lambda: self._play_trailer(ref, trailer))
        trailer_action.setEnabled(bool(trailer))

        menu.addAction(S.MENU_DETAILS, lambda: self._open_details(ref))

        add_menu = menu.addMenu(S.MENU_ADD_TO_PLAYLIST)
        for playlist in db_list_playlists(self.conn):
            add_menu.addAction(playlist.name, lambda p=playlist, r=ref: self._add_to_playlist(p, r))
        add_menu.addAction(S.MENU_NEW_PLAYLIST, lambda r=ref: self._add_to_new_playlist(r))

        menu.addAction(S.MENU_OPEN_FOLDER, lambda: self._open_folder(movie, episode, show))
        menu.addAction(S.MENU_RESCAN, lambda: self._rescan_single(ref))
        if ref.ref_type != "season":
            # Bei einer Staffel nicht anbieten: „Entfernen" würde die GANZE Serie löschen.
            title = movie.title if movie else (episode.title if episode else (show.title if show else ""))
            menu.addAction(
                S.MENU_REMOVE_FROM_LIBRARY,
                lambda r=ref, t=title: self._remove_from_library(r, t),
            )
        menu.exec(global_pos)

    def _load_ref(self, ref: MediaRef) -> tuple[Movie | None, Episode | None, Show | None]:
        if ref.ref_type == "movie":
            row = self.conn.execute("SELECT * FROM movies WHERE id = ?", (ref.ref_id,)).fetchone()
            return (_row_to_movie(row) if row else None), None, None
        if ref.ref_type == "episode":
            episode = db_get_episode(self.conn, ref.ref_id)
            show = None
            if episode:
                row = self.conn.execute("SELECT * FROM shows WHERE id = ?", (episode.show_id,)).fetchone()
                show = _row_to_show(row) if row else None
            return None, episode, show
        if ref.ref_type == "show" or ref.ref_type == "season":
            row = self.conn.execute("SELECT * FROM shows WHERE id = ?", (ref.ref_id,)).fetchone()
            return None, None, (_row_to_show(row) if row else None)
        return None, None, None

    def _default_player(self) -> dict | None:
        for player in self.cfg.get("players", []):
            if player.get("default"):
                return player
        return (self.cfg.get("players") or [None])[0]

    def _add_to_playlist(self, playlist: Playlist, ref: MediaRef) -> None:
        if ref.ref_type == "movie":
            db_add_playlist_item(self.conn, playlist.id, "movie", ref.ref_id, None)
        elif ref.ref_type == "episode":
            db_add_playlist_item(self.conn, playlist.id, "episode", ref.ref_id, None)
        elif ref.ref_type == "show":
            # Ganze Serie: alle Episoden in Staffel-/Episoden-Reihenfolge anhängen.
            episodes = db_list_episodes(self.conn, ref.ref_id)
            db_add_episodes_to_playlist(self.conn, playlist.id, [e.id for e in episodes])
            if episodes:
                self.statusBar().showMessage(
                    S.STATUS_SHOW_ADDED_TO_PLAYLIST.format(len(episodes), playlist.name), 8000
                )
                logger.info("Serie zur Playlist hinzugefügt: %s Episoden → %s", len(episodes), playlist.name)
        elif ref.ref_type == "season":
            # Einzelne Staffel: nur die Episoden dieser Staffel, in Episoden-Reihenfolge.
            episodes = [e for e in db_list_episodes(self.conn, ref.ref_id) if e.season == ref.season]
            db_add_episodes_to_playlist(self.conn, playlist.id, [e.id for e in episodes])
            if episodes:
                self.statusBar().showMessage(
                    S.STATUS_SHOW_ADDED_TO_PLAYLIST.format(len(episodes), playlist.name), 8000
                )
                logger.info(
                    "Staffel zur Playlist hinzugefügt: %s Episoden (S%02d) → %s",
                    len(episodes),
                    ref.season or 0,
                    playlist.name,
                )
        self._playlist_view.refresh()

    def _add_to_new_playlist(self, ref: MediaRef) -> None:
        name, ok = QInputDialog.getText(self, S.PLAYLIST_NEW, S.PLAYLIST_NEW_NAME)
        if ok and name.strip():
            playlist = db_create_playlist(self.conn, name.strip())
            self._add_to_playlist(playlist, ref)

    def _open_folder(self, movie: Movie | None, episode: Episode | None, show: Show | None = None) -> None:
        folder = ""
        if show is not None:
            folder = show.path or ""
        elif movie is not None:
            folder = movie.folder_path or ""
            # Alter DB-Stand kann einen leeren/ungültigen Ordnerpfad haben —
            # dann auf den Ordner der Videodatei ausweichen (nie still scheitern).
            if not folder or not Path(folder).is_dir():
                folder = str(Path(movie.video_path).parent)
        elif episode is not None:
            folder = str(Path(episode.video_path).parent)
        if not folder:
            return
        if not Path(folder).is_dir():
            logger.warning("Ordner existiert nicht: %s", folder)
            self.statusBar().showMessage(S.STATUS_FOLDER_MISSING.format(folder), 8000)
            return
        ok, message = system_open(folder, folder_fallbacks=True)
        if not ok:
            logger.error("Ordner öffnen fehlgeschlagen: %s (%s)", folder, message)
            QMessageBox.warning(self, S.STATUS_FOLDER_OPEN_FAILED, S.STATUS_FOLDER_OPEN_HINT.format(message))

    def _remove_from_library(self, ref: MediaRef, title: str) -> None:
        """Eintrag nur aus der Bibliothek (Datenbank) entfernen — niemals Dateien
        von der Festplatte löschen (dazu ist der Datei-Explorer da)."""
        answer = QMessageBox.question(
            self,
            S.MENU_REMOVE_FROM_LIBRARY,
            S.REMOVE_FROM_LIBRARY_CONFIRM.format(title),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if ref.ref_type == "movie":
            db_delete_movie(self.conn, ref.ref_id)
        elif ref.ref_type == "show":
            db_delete_show(self.conn, ref.ref_id)
        elif ref.ref_type == "episode":
            db_delete_episode(self.conn, ref.ref_id)
        self.refresh_all()
        self.statusBar().showMessage(S.STATUS_LIBRARY_REMOVED.format(title), 8000)
        logger.info("Aus Bibliothek entfernt (%s): %s", ref.ref_type, title)

    def _rescan_single(self, ref: MediaRef) -> None:
        movie, _episode, show = self._load_ref(ref)
        if movie:
            rescan_single_movie(self.conn, movie)
            self.refresh_movies()
            self.statusBar().showMessage(S.SETTINGS_SAVED, 5000)
        elif ref.ref_type in ("show", "season") and show:
            rescan_single_show(self.conn, show)
            self.refresh_all()
            self.statusBar().showMessage(S.SETTINGS_SAVED, 5000)

    # -- Wiedergabe
    def _play_ref(self, ref: MediaRef, player: dict | None = None, subtitle=None) -> None:
        """Wiedergabe-Dispatcher: Film/Episode direkt, Serie/Staffel = Episodenliste."""
        if ref.ref_type == "show":
            _movie, _episode, show = self._load_ref(ref)
            if show:
                self._play_show(show, player)
            return
        if ref.ref_type == "season":
            _movie, _episode, show = self._load_ref(ref)
            if show:
                self._play_show(show, player, season=ref.season)
            return
        self.play_ref(ref, player, subtitle)

    def _play_show(self, show: Show, player: dict | None = None, season: int | None = None) -> None:
        """Serie (oder einzelne Staffel) abspielen: alle vorhandenen Episoden in
        Staffel-/Episoden-Reihenfolge als temporäre Wiedergabeliste (wie Playlist-Abspielen)."""
        episodes = [
            e
            for e in db_list_episodes(self.conn, show.id)
            if e.video_path and Path(e.video_path).exists() and (season is None or e.season == season)
        ]
        if not episodes:
            logger.warning("Keine abspielbaren Episoden für Serie: %s", show.title)
            self.statusBar().showMessage(S.STATUS_SHOW_NO_EPISODES.format(show.title), 8000)
            return
        lines = ["#EXTM3U"]  # einfache Wiedergabeliste, kein HLS (siehe pl_export_m3u8)
        for episode in episodes:
            lines.append(f"#EXTINF:0,{show.title} S{episode.season:02d}E{episode.episode:02d}")
            lines.append(episode.video_path)
        name = show.title or "Serie"
        if season is not None:
            name = f"{name} S{season:02d}"
        safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in name)
        target = data_dir() / "tmp" / f"{safe_name or 'Serie'}.m3u8"
        _write_playlist_file(target, "\n".join(lines) + "\n")
        logger.info(
            "%s wird abgespielt: %s (%d Episoden)",
            "Staffel" if season is not None else "Serie",
            name,
            len(episodes),
        )
        self._launch(str(target), player)

    def play_ref(self, ref: MediaRef, player: dict | None = None, subtitle=None) -> None:
        movie, episode, _show = self._load_ref(ref)
        video = movie.video_path if movie else (episode.video_path if episode else None)
        if not video:
            return
        if not Path(video).exists():
            QMessageBox.warning(self, S.STATUS_FILE_MISSING_TITLE, S.STATUS_FILE_MISSING.format(video))
            return
        self._launch(video, player, subtitle)

    def _launch(self, video: str, player: dict | None = None, subtitle=None) -> None:
        player = player or self._default_player()
        if not player:
            QMessageBox.warning(self, S.STATUS_PLAYER_MISSING_TITLE, S.STATUS_PLAYER_MISSING.format("-"))
            return
        fullscreen = bool(self.cfg.get("playback", {}).get("fullscreen", False))
        try:
            launch_player(player, video, subtitle, fullscreen)
        except PlayerNotFoundError:
            self._handle_player_missing(player, video, subtitle)

    def _handle_player_missing(self, player: dict, video: str, subtitle) -> None:
        message = QMessageBox(self)
        message.setIcon(QMessageBox.Icon.Warning)
        message.setWindowTitle(S.STATUS_PLAYER_MISSING_TITLE)
        message.setText(S.STATUS_PLAYER_MISSING.format(player.get("name", "")))
        path_input = QLineEdit(player.get("path", ""))
        message.layout().addWidget(path_input)
        save_button = message.addButton(S.SETTINGS_OK, QMessageBox.ButtonRole.AcceptRole)
        message.addButton(S.SETTINGS_CANCEL, QMessageBox.ButtonRole.RejectRole)
        message.exec()
        if message.clickedButton() is save_button and path_input.text().strip():
            player["path"] = path_input.text().strip()
            save_settings(self.cfg)
            self._launch(video, player, subtitle)

    def _play_trailer(self, ref: MediaRef, trailer: str | None) -> None:
        if not trailer:
            return
        if trailer.startswith("http"):
            if self.cfg.get("playback", {}).get("youtube_trailer") == "vlc":
                self._launch(trailer)
            else:
                ok, message = system_open(trailer)
                if not ok:
                    logger.error("Browser öffnen fehlgeschlagen: %s (%s)", trailer, message)
                    self.statusBar().showMessage(S.STATUS_FOLDER_OPEN_FAILED, 8000)
            return
        self._launch(trailer)

    def _play_playlist(self, playlist: Playlist) -> None:
        target = pl_write_temp_playlist(self.conn, playlist, data_dir() / "tmp")
        if target:
            self._launch(str(target))

    # -- Detail-Dialog
    def _open_details(self, ref: MediaRef) -> None:
        movie, episode, show = self._load_ref(ref)
        if movie:
            video = Path(movie.video_path)
            dialog = DetailDialog(
                self,
                ref,
                movie.title,
                movie.poster_path,
                movie.fanart_path,
                meta_rows_for_movie(movie),
                movie.plot,
                movie.video_path,
                find_subtitles(video.parent, video.stem),
                movie.trailer_path,
                self._theme_name(),
                nfo_path=movie.nfo_path,
            )
        elif episode:
            video = Path(episode.video_path)
            title = f"{show.title}: {episode.title}" if show else episode.title
            dialog = DetailDialog(
                self,
                ref,
                title,
                episode.still_path,
                show.fanart_path if show else None,
                meta_rows_for_episode(episode, show),
                episode.plot,
                episode.video_path,
                find_subtitles(video.parent, video.stem),
                None,
                self._theme_name(),
                file_label=f"S{episode.season:02d}E{episode.episode:02d}",
                nfo_path=episode.nfo_path,
            )
        elif show and ref.ref_type in ("show", "season"):
            seasons = db_list_seasons(self.conn, show.id)
            episodes = db_list_episodes(self.conn, show.id)
            title = show.title
            if ref.ref_type == "season" and ref.season is not None:
                title = f"{show.title} — {S.SERIES_SEASON_TILE.format(ref.season)}"
            dialog = DetailDialog(
                self,
                ref,
                title,
                show.poster_path,
                show.fanart_path,
                meta_rows_for_show(show, len(seasons), len(episodes)),
                show.plot,
                None,  # Serie selbst hat kein Video — Episoden werden in der Ansicht abgespielt
                [],
                None,
                self._theme_name(),
                nfo_path=show.nfo_path,
            )
        else:
            return
        dialog.play_requested.connect(self.play_ref)
        dialog.trailer_requested.connect(lambda r: self._play_trailer(r, self._trailer_of(r)))
        dialog.exec()

    def _trailer_of(self, ref: MediaRef) -> str | None:
        """Lokale Trailerdatei immer vor NFO-URL (YouTube nur als Fallback)."""
        movie, _episode, _show = self._load_ref(ref)
        if not movie:
            return None
        video = Path(movie.video_path)
        local = find_trailer(video.parent, video.stem)
        if local:
            return str(local)
        return movie.trailer_path

    # -- Tastenkürzel-Aktionen
    def _current_ref(self) -> MediaRef | None:
        grid, mlist = self._current_grid_and_list()
        if grid is not None:
            if grid.isVisible():
                return grid.current_ref()
            return mlist.current_ref()
        if self._stack.currentIndex() == PAGE_SHOWS:
            return (
                self._series_view.current_episode_ref()
                or self._series_view.current_season_ref()
                or self._series_view.current_show_ref()
            )
        return None

    def _on_play_key(self) -> None:
        ref = self._current_ref()
        if ref:
            self._play_ref(ref)

    def _on_details_key(self) -> None:
        ref = self._current_ref()
        if ref:
            self._open_details(ref)

    def _on_trailer_key(self) -> None:
        ref = self._current_ref()
        if ref:
            self._play_trailer(ref, self._trailer_of(ref))

    def _on_search_key(self) -> None:
        self._nav.setCurrentRow(self._nav_row_for_page(PAGE_MOVIES))
        self._search.setFocus()

    def _on_back_key(self) -> None:
        if self._stack.currentIndex() == PAGE_SHOWS and self._series_view.go_back():
            return
        self._nav.setCurrentRow(self._nav_row_for_page(PAGE_MOVIES))

    def _on_playlist_remove_key(self) -> None:
        if self._stack.currentIndex() == PAGE_PLAYLISTS:
            self._playlist_view._remove_item()

    # -- Einstellungen
    def _open_settings(self) -> None:
        dialog = SettingsDialog(self, self.cfg, conn=self.conn, scan_requested=self._scan_library)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.cfg = load_settings()
            self._prune_orphans()
            self._apply_theme()
            self._series_view._theme = get_theme(self._theme_name())
            self._series_view._shows_grid.set_poster_size(self._poster_width(), self._label_under_poster())
            self._movie_grid.set_poster_size(self._poster_width(), self._label_under_poster())
            for page in self._category_pages.values():
                page["grid"].set_poster_size(self._poster_width(), self._label_under_poster())
            # Eigene Kategorien können geändert/entfernt worden sein → Seiten + Nav neu.
            self._rebuild_category_pages()
            for shortcut in self._shortcuts.values():
                shortcut.setParent(None)
                shortcut.deleteLater()
            self._register_shortcuts()
            self.refresh_all()
            self.statusBar().showMessage(S.SETTINGS_SAVED, 5000)

    def _scan_library(self, path: str, on_finished=None) -> None:
        """Scan aus dem Einstellungen-Dialog: gewählte Bibliothek oder alle."""
        if path:
            libraries = [lib for lib in self.cfg.get("libraries", []) if lib.get("path") == path]
        else:
            libraries = list(self.cfg.get("libraries", []))
        self._run_scan(libraries, on_finished)

    def _restore_window_geometry(self) -> bool:
        """Fenstergröße/Position aus settings.json (ui.window_geometry) wiederherstellen."""
        geom = self.cfg.get("ui", {}).get("window_geometry")
        if isinstance(geom, str) and geom:
            try:
                return self.restoreGeometry(QByteArray.fromBase64(geom.encode("ascii")))
            except Exception:
                logger.warning("Gespeicherte Fenstergeometrie unlesbar — nutze Default", exc_info=True)
        return False

    def _save_window_geometry(self) -> None:
        """Fenstergröße/Position in settings.json sichern (inkl. maximiert-Zustand)."""
        try:
            b64 = bytes(self.saveGeometry().toBase64()).decode("ascii")
            ui = self.cfg.setdefault("ui", {})
            ui["window_geometry"] = b64
            save_settings(self.cfg)
        except OSError:
            logger.warning("Fenstergeometrie konnte nicht gespeichert werden", exc_info=True)

    def closeEvent(self, event) -> None:
        self._save_window_geometry()
        self._series_view.shutdown()
        if self._thumb_worker is not None:
            self._thumb_worker.stop()
            self._thumb_worker.wait(2000)
        if self._scan_worker is not None and self._scan_worker.isRunning():
            self._scan_worker.cancel()
            self._scan_worker.wait(5000)
        self.conn.close()
        super().closeEvent(event)


# ===========================================================================
# 8. Bootstrap
# ===========================================================================


def _setup_frozen_qt() -> None:
    """Als ausführbare Datei (PyInstaller): Qt-Plugin-Verzeichnis explizit setzen,
    damit Plattform-Plugins (xcb/wayland/offscreen) und JPEG-Codecs gefunden werden."""
    if not getattr(sys, "frozen", False):
        return
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return
    for plugin_dir in (Path(base) / "PySide6" / "Qt" / "plugins", Path(base) / "PySide6" / "plugins"):
        if plugin_dir.is_dir():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plugin_dir))
            break


def setup_logging() -> None:
    log_dir = logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "mediacenter.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    console = logging.StreamHandler(sys.stderr)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler.setFormatter(formatter)
    console.setFormatter(formatter)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(console)


def main() -> int:
    if _IS_QT5:
        # Qt 5 (Windows 7/8): High-DPI-Skalierung explizit einschalten —
        # muss VOR der QApplication-Instanz gesetzt werden; Qt 6 macht das automatisch.
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    _setup_frozen_qt()
    setup_logging()
    logger.info("MediaCenter startet (Einzeldatei-Version)")
    app = QApplication(sys.argv)
    app.setApplicationName("MediaCenter")
    icon_path = app_icon_path()
    if icon_path is not None:
        # Fenster-Icon (Titelleiste, Taskleiste, Alt-Tab) — gilt für alle Fenster.
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        logger.warning("App-Icon icon/mediacenter.png nicht gefunden")
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
