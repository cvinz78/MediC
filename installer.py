#!/usr/bin/env python3
"""MediaCenter Installer — installiert die Schnellstart-Variante (onedir).

Zwei Modi:
- Installieren: Standardpfad C:\\Program Files\\MediaCenter (änderbar)
- Portable entpacken: beliebiger Ordner (z. B. USB-Stick), keine Installation

Das Paket (mediacenter-fast.exe + _internal/) wird beim Bau des Installers
per --add-data eingebettet (sys._MEIPASS/mediacenter-fast).

Kopfloser Testmodus:  installer.py --to <zielordner> [--mode install|portable] [--integrate]
    (--integrate: Startmenü + „Programme und Features"-Eintrag + uninstall.cmd, nur install)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "MediaCenter"
APP_VERSION = "1.6"
PAYLOAD_DIRNAME = "MediaCenterx64"
EXE_NAME = "MediaCenterx64.exe"
UNINSTALL_FILENAME = "uninstall.cmd"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MediaCenter"


def payload_root() -> Path:
    """Eingebettetes Paket finden (frozen) bzw. im Projektordner (Entwicklung)."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / PAYLOAD_DIRNAME
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent / "dist" / PAYLOAD_DIRNAME


def default_install_dir() -> Path:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    return Path(program_files) / APP_NAME


def default_portable_dir() -> Path:
    desktop = Path.home() / "Desktop"
    return desktop / APP_NAME


def install_payload(target: Path) -> tuple[int, int]:
    """Paket nach target kopieren. Rückgabe: (Dateien, Bytes)."""
    source = payload_root()
    if not (source / EXE_NAME).is_file():
        raise FileNotFoundError(f"Installationspaket unvollständig: {source}")
    target.mkdir(parents=True, exist_ok=True)
    files = 0
    total_bytes = 0
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        shutil.copy2(item, destination)
        files += 1
        total_bytes += item.stat().st_size
    return files, total_bytes


def run_installer(target: Path) -> tuple[int, int]:
    files, total = install_payload(target)
    if not (target / EXE_NAME).is_file():
        raise RuntimeError("Installation unvollständig — EXE fehlt")
    return files, total


def start_menu_link() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / f"{APP_NAME}.lnk"


def make_start_menu_shortcut(target: Path) -> Path | None:
    """Startmenü-Verknüpfung anlegen (per-user, keine Adminrechte nötig)."""
    link = start_menu_link()
    command = (
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        f"$s.TargetPath='{target / EXE_NAME}';"
        f"$s.WorkingDirectory='{target}';"
        f"$s.IconLocation='{target / EXE_NAME},0';"
        "$s.Save()"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except OSError:
        return None
    return link if proc.returncode == 0 and link.exists() else None


def register_installation(target: Path) -> None:
    """Eintrag unter „Programme und Features" (HKCU — ohne Adminrechte)."""
    import winreg

    uninstall_script = write_uninstall_script(target)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, APP_NAME)
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(target / EXE_NAME))
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'cmd.exe /c "{uninstall_script}"')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)


def write_uninstall_script(target: Path) -> Path:
    """Deinstallations-Skript in den Installationsordner legen (nur ASCII-Zeichen)."""
    script = target / UNINSTALL_FILENAME
    script.write_text(
        "@echo off\r\n"
        "title MediaCenter Uninstaller\r\n"
        'choice /c JN /n /m "MediaCenter deinstallieren? (J/N) "\r\n'
        "if errorlevel 2 goto end\r\n"
        f'reg delete "HKCU\\{UNINSTALL_KEY}" /f >nul 2>&1\r\n'
        f'del "{start_menu_link()}" >nul 2>&1\r\n'
        'pushd "%TEMP%"\r\n'
        'rmdir /s /q "%~dp0."\r\n'
        "popd\r\n"
        ":end\r\n",
        encoding="ascii",
        errors="replace",
    )
    return script


def integrate(target: Path) -> str:
    """Installations-Integration: Startmenü + „Programme und Features".

    Rückgabe: Kurzfassung für die Statusanzeige (was angelegt wurde).
    """
    link = make_start_menu_shortcut(target)
    register_installation(target)
    parts = ["Startmenü-Verknüpfung" if link else "Startmenü-Verknüpfung fehlgeschlagen"]
    parts.append('Eintrag unter „Programme und Features"')
    return " · ".join(parts)


def installed_location() -> Path | None:
    """Installationspfad einer bestehenden Installation (Registry), falls vorhanden."""
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            location, _type = winreg.QueryValueEx(key, "InstallLocation")
    except OSError:
        return None
    location_path = Path(location)
    if (location_path / EXE_NAME).is_file():
        return location_path
    return None


def uninstall(location: Path) -> tuple[bool, str]:
    """Bestehende Installation entfernen: Ordner, Startmenü, Registry."""
    problems: list[str] = []
    try:
        shutil.rmtree(location)
    except OSError as exc:
        problems.append(
            f"Programmordner konnte nicht entfernt werden ({exc}). "
            "Läuft MediaCenter noch? Dateien ggf. manuell löschen."
        )
    try:
        start_menu_link().unlink()
    except OSError as exc:
        problems.append(f"Startmenü-Verknüpfung konnte nicht entfernt werden ({exc}).")
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
    except OSError as exc:
        problems.append(f"Registry-Eintrag konnte nicht entfernt werden ({exc}).")
    return (not problems), (" · ".join(problems) if problems else "")


def main_cli() -> int:
    args = sys.argv[1:]
    if "--to" not in args:
        return 1
    target = Path(args[args.index("--to") + 1])
    mode = "install"
    if "--mode" in args:
        mode = args[args.index("--mode") + 1]
    files, total = run_installer(target)
    integration = ""
    if mode == "install" and "--integrate" in args:
        integration = " · " + integrate(target)
    print(f"OK: {files} Dateien ({total / 1e6:.1f} MB) nach {target}{integration}")
    return 0


def main_gui() -> int:
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QFileDialog,
        QGridLayout,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QRadioButton,
        QVBoxLayout,
        QWidget,
    )

    app = QApplication(sys.argv)
    app.setStyleSheet(
        "QWidget { background-color: #1b1b1f; color: #e8e8ea; font-size: 10pt; }"
        "QPushButton { background-color: #2d2d33; padding: 6px 14px; border: 1px solid #4a4a52;"
        "             border-radius: 4px; }"
        "QPushButton:hover { background-color: #3a3a42; }"
        "QPushButton#primary { background-color: #2f6f3f; border-color: #3f8f55; font-weight: bold; }"
        "QPushButton#danger { background-color: #6f2f2f; border-color: #8f3f3f; }"
        "QLineEdit { background-color: #232328; border: 1px solid #4a4a52; border-radius: 4px; padding: 4px; }"
        "QLabel#status { color: #9aa0a6; }"
    )

    window = QWidget()
    window.setWindowTitle(f"{APP_NAME} Installer")
    window.setFixedWidth(640)
    layout = QVBoxLayout(window)
    layout.setSpacing(10)

    title = QLabel(f"{APP_NAME} installieren")
    title.setStyleSheet("font-size: 15pt; font-weight: bold;")
    layout.addWidget(title)
    hint = QLabel(
        "Installiert die Schnellstart-Variante (ohne Entpacken beim Start).\n"
        "Die Dateien liegen danach fest im Programmordner."
    )
    hint.setObjectName("status")
    layout.addWidget(hint)

    group = QButtonGroup(window)
    row_install = QRadioButton("Installieren (Standardpfad von Windows):")
    row_install.setChecked(True)
    group.addButton(row_install)
    layout.addWidget(row_install)
    grid = QGridLayout()
    install_edit = QLineEdit(str(default_install_dir()))
    install_browse = QPushButton("Auswählen…")
    grid.addWidget(install_edit, 0, 0)
    grid.addWidget(install_browse, 0, 1)
    layout.addLayout(grid)

    row_portable = QRadioButton("Portable entpacken (keine Installation, eigener Ordner):")
    group.addButton(row_portable)
    layout.addWidget(row_portable)
    grid2 = QGridLayout()
    portable_edit = QLineEdit(str(default_portable_dir()))
    portable_browse = QPushButton("Auswählen…")
    grid2.addWidget(portable_edit, 0, 0)
    grid2.addWidget(portable_browse, 0, 1)
    layout.addLayout(grid2)

    status = QLabel("")
    status.setObjectName("status")
    status.setWordWrap(True)
    layout.addWidget(status)

    # Bereits installiert? -> Deinstallation anbieten (installiert ist installiert).
    existing = installed_location()
    if existing is not None:
        hint.setText(
            hint.text() + f"\n\nMediaCenter ist bereits installiert unter: {existing}\n"
            "Eine erneute Installation überschreibt diese Version."
        )

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    uninstall_button: QPushButton | None = None
    if existing is not None:
        uninstall_button = QPushButton("Deinstallieren")
        uninstall_button.setObjectName("danger")
        buttons.addWidget(uninstall_button)
    quit_button = QPushButton("Beenden")
    install_button = QPushButton("Installieren")
    install_button.setObjectName("primary")
    buttons.addWidget(quit_button)
    buttons.addWidget(install_button)
    layout.addLayout(buttons)

    def browse(edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(
            window, "Zielordner wählen", edit.text() or str(Path.home())
        )
        if folder:
            edit.setText(folder)

    install_browse.clicked.connect(lambda: browse(install_edit))
    portable_browse.clicked.connect(lambda: browse(portable_edit))

    def chosen_target() -> Path | None:
        if row_install.isChecked():
            text = install_edit.text().strip()
            return Path(text) if text else None
        if row_portable.isChecked():
            text = portable_edit.text().strip()
            return Path(text) if text else None
        return None

    def on_install() -> None:
        target = chosen_target()
        if target is None:
            status.setText("Bitte einen Zielordner angeben.")
            return
        install_button.setEnabled(False)
        is_install_mode = row_install.isChecked()
        status.setText(f"Kopiere nach {target} …")
        app.processEvents()
        try:
            files, total = run_installer(target)
            integration = ""
            if is_install_mode:
                status.setText("Kopieren fertig — lege Startmenü und Programmeintrag an …")
                app.processEvents()
                integration = integrate(target)
        except PermissionError:
            status.setText(
                "Keine Schreibrechte im Zielordner.\n"
                "Als Administrator starten oder einen Benutzer-Ordner wählen "
                "(z. B. Portable entpacken)."
            )
            install_button.setEnabled(True)
            return
        except Exception as exc:  # noqa: BLE001 — Installer-GUI: Fehler sichtbar machen
            status.setText(f"Fehler: {exc}")
            install_button.setEnabled(True)
            return
        summary = f"Fertig: {files} Dateien ({total / 1e6:.1f} MB) nach {target} kopiert."
        if is_install_mode:
            summary += f"\n{integration}\nDeinstallation später über Programme und Features."
        else:
            summary += "\nPortable Version — Start: " + str(target / EXE_NAME)
        status.setText(summary)
        # Installiert ist installiert: nach Erfolg schließt dieser Button den Installer.
        install_button.setText("Beenden")
        install_button.clicked.disconnect(on_install)
        install_button.clicked.connect(app.quit)
        install_button.setEnabled(True)
        # Nur EIN „Beenden": den zweiten Button ausblenden.
        quit_button.setVisible(False)

    def on_quit() -> None:
        app.quit()

    install_button.clicked.connect(on_install)
    quit_button.clicked.connect(on_quit)

    if existing is not None and uninstall_button is not None:

        def on_uninstall() -> None:
            from PySide6.QtWidgets import QMessageBox

            answer = QMessageBox.question(
                window,
                "MediaCenter deinstallieren",
                f"MediaCenter unter {existing}\nwirklich deinstallieren?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            uninstall_button.setEnabled(False)
            ok, problems = uninstall(existing)
            if ok:
                status.setText("MediaCenter wurde deinstalliert.")
            else:
                status.setText(f"Deinstallation mit Hinweisen abgeschlossen:\n{problems}")
            uninstall_button.deleteLater()

        uninstall_button.clicked.connect(on_uninstall)

    window.show()
    return app.exec()


def main() -> int:
    if "--to" in sys.argv:
        return main_cli()
    return main_gui()


if __name__ == "__main__":
    import os

    raise SystemExit(main())
