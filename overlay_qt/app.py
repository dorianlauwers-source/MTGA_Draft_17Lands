"""
overlay_qt/app.py
Qt6 overlay for MTG Arena drafts, running on the upstream engine.

What this adds over the upstream Tk front end:
  * a native Wayland window instead of Tk under XWayland
  * a --daemon mode that shows itself when Arena starts and hides when it exits
  * a systemd user service so it follows the session with no manual launch

Everything below the UI (log scanning, datasets, advisor) is upstream code,
imported unmodified.
"""

import argparse
import logging
import os
import signal
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                             QMenu, QVBoxLayout, QWidget)

from src import constants
from src.configuration import read_configuration
from src.file_extractor import search_arena_log_locations
from src.limited_sets import LimitedSets
from src.log_scanner import ArenaScanner

from overlay_qt.bridge import DraftBridge
from overlay_qt.views.pack_view import PackView

logger = logging.getLogger(__name__)

ARENA_PROCESS = "MTGA.exe"
ARENA_POLL_MS = 5000


def arena_is_running() -> bool:
    """
    True when the Arena client is running, whatever the launcher.

    Reads /proc directly rather than shelling out to pgrep: no subprocess every
    five seconds, and it works under Steam, Flatpak, Lutris and Bottles alike.
    """
    own_pid = str(os.getpid())
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or entry == own_pid:
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                cmdline = handle.read()
        except OSError:
            continue
        if ARENA_PROCESS.encode() in cmdline and b"overlay_qt" not in cmdline:
            return True
    return False


class OverlayWindow(QMainWindow):
    def __init__(self, scanner, configuration, daemon_mode=False):
        super().__init__()
        self.daemon_mode = daemon_mode

        self.setWindowTitle("MTGA Draft Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.resize(620, 700)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        self.title_label = QLabel("En attente d'un draft...")
        self.title_label.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.event_label = QLabel("")
        self.event_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.title_label, 1)
        header_layout.addWidget(self.event_label, 1)
        header.setStyleSheet(
            "background: #2c3e50; color: #ecf0f1; border-top-left-radius: 5px;"
            " border-top-right-radius: 5px;"
        )
        layout.addWidget(header)

        self.pack_view = PackView()
        layout.addWidget(self.pack_view, 1)

        self.status_label = QLabel("Démarrage...")
        self.status_label.setStyleSheet(
            "background: rgba(18,18,18,235); color: #95a5a6; padding: 4px; font-size: 12px;"
        )
        layout.addWidget(self.status_label)

        central.setStyleSheet(
            "QWidget { background: rgba(18,18,18,242); color: #ecf0f1; }"
            "QTreeView { background: transparent; border: none; font-size: 13px; }"
            "QTreeView::item { padding: 3px; }"
            "QHeaderView::section { background: #34495e; color: #ecf0f1;"
            " padding: 4px; border: none; font-weight: bold; }"
        )

        self.bridge = DraftBridge(scanner, configuration, parent=self)
        self.bridge.snapshot_ready.connect(self.on_snapshot)
        self.bridge.status_changed.connect(self.status_label.setText)
        self.bridge.error.connect(lambda msg: self.status_label.setText(f"Erreur : {msg}"))
        self.bridge.start()

        self._drag_position = None

        self.arena_timer = QTimer(self)
        if daemon_mode:
            self.arena_timer.timeout.connect(self.check_arena)
            self.arena_timer.start(ARENA_POLL_MS)
            QTimer.singleShot(0, self.check_arena)

    # --- data -----------------------------------------------------------

    def on_snapshot(self, snapshot):
        self.title_label.setText(snapshot.status_text)
        if snapshot.event_set or snapshot.event_type:
            self.event_label.setText(
                f"{snapshot.event_set} {snapshot.event_type}".strip()
            )
        self.pack_view.update_pack(snapshot)

        taken = len(snapshot.taken_cards or [])
        colours = snapshot.active_filter
        self.status_label.setText(
            f"{taken} carte(s) prise(s)  |  couleurs : {colours}"
        )
        if self.daemon_mode and snapshot.is_drafting and not self.isVisible():
            self.show()

    # --- daemon ---------------------------------------------------------

    def check_arena(self):
        running = arena_is_running()
        if running and not self.isVisible():
            self.show()
        elif not running and self.isVisible():
            self.hide()

    # --- window chrome --------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_position = event.globalPosition().toPoint()

    def mouseMoveEvent(self, event):
        if self._drag_position and event.buttons() == Qt.MouseButton.LeftButton:
            current = event.globalPosition().toPoint()
            self.move(self.pos() + current - self._drag_position)
            self._drag_position = current

    def mouseReleaseEvent(self, event):
        self._drag_position = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.addAction("Rescan complet du log").triggered.connect(self.bridge.full_rescan)
        menu.addSeparator()
        menu.addAction("Fermer").triggered.connect(self.close)
        menu.exec(event.globalPos())

    def closeEvent(self, event):
        self.bridge.stop()
        event.accept()


def build_scanner(configuration, log_path=None):
    """Minimal equivalent of main.load_data(), without the splash screen."""
    path = search_arena_log_locations(
        arg_location=log_path, config_location=configuration.settings.arena_log_location
    )
    if not path:
        raise SystemExit(
            "Player.log introuvable. Lancez Arena une fois, ou passez --file <chemin>."
        )
    logger.info("Player.log : %s", path)

    limited_sets = LimitedSets().retrieve_limited_sets()
    scanner = ArenaScanner(path, limited_sets.data)
    configuration.settings.arena_log_location = path
    return scanner


def main(argv=None):
    parser = argparse.ArgumentParser(description="Overlay Qt6 pour les drafts MTG Arena")
    parser.add_argument("-f", "--file", help="Chemin vers Player.log")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Masque la fenêtre tant qu'Arena n'est pas lancé",
    )
    parser.add_argument("--debug", action="store_true", help="Journalisation détaillée")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    configuration, _ = read_configuration()
    scanner = build_scanner(configuration, args.file)

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(not args.daemon)
    window = OverlayWindow(scanner, configuration, daemon_mode=args.daemon)
    if not args.daemon:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
