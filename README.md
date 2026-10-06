<div align="center">

<img src="screenshots/banner.png" alt="MediC Banner" width="100%">

# MediC

**Mediathek-GUI für Windows &amp; Linux** — Posterwand, NFO-Unterstützung,
Playlisten und Wiedergabe über externe Player wie VLC oder mpv.

**[⬇ Download (aktuelle Version)](https://github.com/cvinz78/MediC/releases/latest)** · [Lizenz: GPL-3.0](LICENSE)

</div>

---

**MediC** (von *MediaCenter*) liest lokale Medienordner ein, zeigt Filme und
Serien als Posterwand und übergibt die Wiedergabe an den externen Player deiner Wahl
(Standard: VLC). Vorhandene Metadaten- und Bilddateien — `movie.nfo`, `tvshow.nfo`,
Poster, Fanart, Trailer, Untertitel — werden ohne Konvertierung genutzt. Es werden **niemals Dateien
deiner Mediathek verändert oder gelöscht**, die App schreibt ausschließlich in ihren
eigenen Datenordner.

---

## Screenshots — die drei Skins

| Darkskin (Standard) | Bluemoon | Creamy |
|---|---|---|
| ![Darkskin](screenshots/darkskin.png) | ![Bluemoon](screenshots/bluemoon.png) | ![Creamy](screenshots/creamy.png) |

Der Skin lässt sich jederzeit umschalten — per **Theme-Button unten links** (klickt durch
alle drei) oder unter **Einstellungen → Ansicht**. Der Wechsel wirkt sofort, ohne Neustart.
*(Screenshots zeigen eine Beispieldatenbank.)*

---

## Funktionen

### Bibliotheken & Scan
- **Mehrere Bibliotheken** konfigurierbar (Name, Typ, Pfad) — beliebig viele Unterordner rekursiv
- **Eigene Kategorien** neben „Filme“ und „Serien“ (z. B. „Doku“, „Comedy“) — jede Kategorie bekommt einen eigenen Eintrag in der Navigation
- **Auto-Klassifikation**: Ordner mit Serienstruktur (`tvshow.nfo`, `SxxEyy`) landen automatisch in der Serien-Ansicht — auch innerhalb von Film-/Kategorie-Bibliotheken
- **Inkrementeller Scan**: unveränderte Einträge (mtime + Pfad) werden nicht neu eingelesen
- Scan läuft im **Hintergrund-Thread** mit Fortschritt in Prozent, aktueller Datei und **Abbrechen-Button**
- **Netzwerk-Mounts** (NFS/SMB): langsame Scans sind kein Problem, IO-Fehler (Mount weg) werden als Warnung behandelt statt den Scan abzubrechen
- Verschwundene Dateien räumt der Scan aus der Mediathek auf — **nur aus der Datenbank, nie von der Festplatte**

### NFO-Metadaten & Datei-Erkennung
- NFO-Parsing: `movie.nfo`, `tvshow.nfo`, Episoden-NFOs — inkl. UTF-8 mit BOM, Fallback latin-1, XML ab beliebiger Zeile, doppelte `<movie>`-Blöcke, modernes `<ratings>`-Format; unbekannte Tags werden ignoriert
- Erkannt werden: Titel, Sortier-Titel, Plot, Jahr, Bewertung, Laufzeit, Genres, Studio, Regisseur, Darsteller, Poster, Fanart, Trailer, Staffel/Episode/Ausstrahlungsdatum
- Poster/Fanart/Banner/Staffel-Poster/Trailer/Untertitel nach dem gängigen NFO-Schema (Tabelle unten)

### Posterwand & Ansichten
- **Klick auf ein Poster = Film startet** im Standard-Player
- **Rechtsklick = Kontextmenü**: Abspielen, Trailer abspielen, Details/Medieninfos, Zur Playlist hinzufügen, Ordner öffnen, Metadaten neu einlesen, Aus Bibliothek entfernen
- **Posterwand ↔ Liste** umschaltbar (Filme, Serien und eigene Kategorien; Serien-Liste mit Titel/Jahr/Bewertung)
- **4 Postergrößen** (Klein/Mittel/Groß/Sehr groß), Titel unter dem Poster an/aus
- **Live-Suche** über Titel, Genre und Jahr — inkl. Jahresbereichen (`1977-2005`, `1977-`, `-2005`)
- **Genre-Filter mit Zählern** — getrennt je Ansicht (Filme-Seite zeigt nur Film-Genres usw.)
- **Sortierung** nach Titel oder Jahr
- **„Zufall“-Button**: spielt einen zufälligen Film bzw. eine zufällige Episode — bei aktivem Filter aus den gefilterten Einträgen, in der Staffel-/Episoden-Ansicht aus der aktuell geöffneten Serie/Staffel

### Serien-Ansicht (3 Ebenen)
- Ebene 1: Posterwand aller Serien → Ebene 2: Staffeln + Serieninfos (Fanart-Hintergrund) → Ebene 3: Episodenliste mit Thumbnails
- **Breadcrumb-Navigation** (Serien › Show › Staffel), `Esc` geht eine Ebene zurück
- Ganze Serien oder einzelne Staffeln abspielen (temporäre M3U8 an den Player) oder in Playlisten aufnehmen

### Detail-Dialog (Medieninfos)
- Großes Poster, abgedunkeltes Fanart als Hintergrund, Sternebewertung
- Jahr, Bewertung, Laufzeit, Genres, Studio, Regisseur, Darsteller, vollständiger Plot
- Abschnitt „Dateien“: Videopfad, gefundene Untertitel (**anklickbar** → Wiedergabe mit diesem Untertitel), Trailer-Quelle

### Wiedergabe & Player
- **Standard-Player: VLC** — automatische Erkennung unter Linux (`$PATH` → `/usr/bin` → `/usr/local/bin` → `/snap/bin` → Flatpak) und Windows (`$PATH` → Registry → Programmordner)
- Weitere Player frei konfigurierbar (mpv, SMPlayer, Celluloid …) mit Argument-Vorlagen `{file}` und `{subtitle}`
- **Vollbild** optional (Einstellung, Standard: aus)
- **Untertitel** werden per `--sub-file` an VLC/mpv übergeben
- **Trailer**: lokale Trailerdatei wird bevorzugt; YouTube-URL öffnet den Browser (oder VLC, einstellbar)
- Player nicht gefunden → Dialog mit Pfadeingabe statt stillem Abbruch
- Der Player startet als eigener Prozess — die App bleibt bedienbar

### Playlisten
- Eigene Ansicht: Playlisten anlegen/umbenennen/löschen, Einträge mit **↑/↓** sortieren
- Einträge: **Filme, Episoden, ganze Serien und Staffeln** (Duplikate erlaubt)
- **Export als M3U8 / M3U / XSPF**, Import von M3U/M3U8
- Wiedergabe über temporäre M3U8 an den Player

### Tastenkürzel
Alle Hauptaktionen sind **editierbar** (Einstellungen → Tastenkürzel, mit Kollisionsprüfung
und „Alle zurücksetzen“). Standard-Belegung:

| Aktion | Taste |
|---|---|
| Abspielen | `Enter` |
| Details/Medieninfos | `i` |
| Trailer abspielen | `t` |
| Suche fokussieren | `Strg+F` |
| Bibliotheken scannen | `F5` |
| Zurück (Dialog/Ebene) | `Esc` |
| Eintrag aus Playlist entfernen | `Entf` |

### Thumbnails ohne NFO/Poster
- Ohne NFO/Poster dient ein **Frame aus dem Video selbst** als Poster (braucht `ffmpeg`, wird automatisch erkannt — optional)
- Der **Dateiname wird zum Titel** (inkl. Jahreszahl-Erkennung)
- Ohne ffmpeg: gerenderter Platzhalter im Skin-Farbdesign
- Thumbnail-Cache im Datenordner — auch sehr große Bibliotheken (1000+) bleiben flott, Ansicht lädt lazy

### Weitere Details
- **3 Skins**: Darkskin, Bluemoon, Creamy (siehe Screenshots oben)
- **Fenstergröße/Position/Maximierung wird gemerkt**
- „Ordner öffnen“ via `xdg-open` (Linux) bzw. Windows-Explorer
- **Scrollbar-Farben je Skin**: Griff immer sichtbar in der Textfarbe des Skins
- Fehlende Videodateien werden angezeigt/markiert; Berechtigungsfehler (NTFS/externe Platten) brechen den Scan nie ab
- Große/extreme Bilddateien werden beim Rendern runterskaliert, abgeschnittene Bilder soweit möglich angezeigt

---

## Download & Installation

Fertige Builds gibt es in den
**[Releases](https://github.com/cvinz78/MediC/releases/latest)**:

| Datei | Plattform | Was es ist |
|---|---|---|
| `MediC-windows-x64.exe` | Windows 10/11 (x64) | Installer — bietet **Installieren** (Startmenü + „Programme und Features“) oder **Portable entpacken** (z. B. USB-Stick). Die App-EXE heißt danach `MediaCenterx64.exe` |
| `MediC-linux-x64` | Linux (x64) | Eine einzelne ausführbare Datei — kein Python/pip nötig, nur einen Desktop mit VLC/mpv |

**SHA-256 (Version 1.5):**

```
e3f65af3df60df69c618cae01a0c39bca7774622f53ce14c093c8e741faef9e5  MediC-windows-x64.exe
d9ca54c1675c2882a4a623983cfcf099ce2bb7954af93b50bae52362124f83c3  MediC-linux-x64
```

**Hinweise:**
- **Windows**: Da die EXE unsigniert ist, meldet SmartScreen beim ersten Start „Weitere
  Informationen → Trotzdem ausführen“ — das ist bei selbst gebauten PyInstaller-EXEs normal.
- **Linux**: `chmod +x MediC-linux-x64` und starten. Die Anwendung entpackt sich **einmalig**
  nach `/tmp/MediaCenter/<Version>` (Smart-Cache) und startet danach sofort von dort; nichts
  wird beim Beenden gelöscht, neu entpackt wird nur bei geleertem Ordner oder älterer Version.
- **Windows 7 SP1/8.1**: Qt 6 läuft dort nicht — eine Win7-Variante (PyQt5) ist in diesem
  Release nicht enthalten.

---

## Erste Schritte

1. Starten → **Einstellungen → Bibliotheken → Hinzufügen**: Ordner wählen (z. B. `~/Filme`,
   `/mnt/nas/Serien`), Name und Typ setzen (Filme/Serien/**eigene Kategorie**).
2. Der Scan läuft im Hintergrund (Fortschritt + Abbrechen) — danach erscheint die **Posterwand**.
3. **Klick** = Abspielen, **Rechtsklick** = Kontextmenü, **F5** = neu scannen.
4. Ohne NFO/Poster zeigt MediC automatisch Video-Frame-Thumbnails (mit ffmpeg) und
   Dateinamen-Titel — jede erkannte Videodatei erscheint in der Ansicht.

---

## Erkennungsregeln

| Typ | Gesucht wird (Reihenfolge = Priorität) |
|---|---|
| Video | `.mkv .mp4 .avi .m2ts .iso .wmv .mov .webm .ts .mpg .mpeg .flv` u. a. (Groß/Klein egal) |
| Film-NFO | `movie.nfo`, `<videoname>.nfo` im selben Ordner |
| Serien-NFO | `tvshow.nfo` im Serienordner; Episoden: `s01e02.nfo`/`<name>.nfo` |
| Poster | `poster.jpg` › `folder.jpg` › `cover.jpg` › `<name>.jpg` › `<ordnername>.jpg` |
| Staffel-Poster | `season01-poster.jpg`, `Season 01/poster.jpg`, `staffel1-poster.*` … |
| Fanart/Banner | `fanart.jpg`, `banner.jpg` |
| Trailer | `trailer.mp4/mkv/mov` › `<name>-trailer.*` › Ordner `trailer(s)/` › `<trailer>`-URL aus dem NFO |
| Untertitel | `<name>.srt/.ass/.sub/.vtt` (auch im Ordner `subs/`) |
| Episoden | `Serie/Season 01/S01E02.mkv` (auch `1x02`, Fallback: Reihenfolge) |

---

## Datenordner

Alle Einstellungen, die Datenbank und Logs liegen außerhalb der Mediathek:

| Plattform | Ordner |
|---|---|
| Linux | `~/.medi/` (`config/settings.json`, `data/library.db`, `data/cache/`, `logs/`) |
| Windows | `%USERPROFILE%\.medi\` |

Mit der Umgebungsvariable `MEDIACENTER_DATA_DIR` lässt sich der Ordner beliebig verlagern
(z. B. portabel auf dem Medienlaufwerk). Log bei Problemen:
`~/.medi/logs/mediacenter.log` bzw. `%USERPROFILE%\.medi\logs\mediacenter.log`.

---

## Lizenz

Copyright © 2026 cvinz78

Dieses Programm ist freie Software: Sie können es unter den Bedingungen der
**GNU General Public License, Version 3** weitergeben und/oder modifizieren —
veröffentlicht von der Free Software Foundation. Details siehe [LICENSE](LICENSE).

Dieses Programm wird in der Hoffnung verbreitet, dass es nützlich ist, aber **ohne
jede Garantie** — auch ohne implizite Garantie für Marktgängigkeit oder Eignung für
einen bestimmten Zweck. Siehe die GNU GPL Version 3 für Einzelheiten.
