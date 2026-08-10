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
from PyQt6.QtGui import QCursor, QFont
from PyQt6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMainWindow,
                             QMenu, QPushButton, QSizeGrip, QTabWidget,
                             QVBoxLayout, QWidget)

from src import constants
from src.configuration import read_configuration
from src.file_extractor import search_arena_log_locations
from src.limited_sets import LimitedSets
from src.log_scanner import ArenaScanner

from overlay_qt.bridge import DraftBridge
from overlay_qt.state import rebuild_draft
from overlay_qt.views.advisor_view import AdvisorPanel
from overlay_qt.views.card_preview import CardPreview
from overlay_qt.views.info_strip import InfoStrip
from overlay_qt.views.pack_view import PackView
from overlay_qt.views.pool_view import PoolView

logger = logging.getLogger(__name__)

ARENA_PROCESS = "MTGA.exe"
ARENA_POLL_MS = 5000

OVERLAY_WIDTH = 400
OVERLAY_HEIGHT = 720


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


class DragHandle(QWidget):
    """
    Title bar of a frameless window.

    QWidget.move() does nothing for a top-level window under Wayland: the
    compositor owns placement, so an app cannot reposition itself. The only
    way to drag is to ask the compositor to take over via startSystemMove(),
    which also works on X11. Without it the overlay is pinned wherever the
    compositor first put it.
    """

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        handle = self.window().windowHandle()
        if handle is not None and handle.startSystemMove():
            event.accept()
            return
        # X11 fallback for the rare case the compositor refuses.
        self._press_position = event.globalPosition().toPoint()
        event.accept()

    def mouseMoveEvent(self, event):
        origin = getattr(self, "_press_position", None)
        if origin is None or event.buttons() != Qt.MouseButton.LeftButton:
            return super().mouseMoveEvent(event)
        window = self.window()
        current = event.globalPosition().toPoint()
        window.move(window.pos() + current - origin)
        self._press_position = current

    def mouseReleaseEvent(self, event):
        self._press_position = None
        super().mouseReleaseEvent(event)


class OverlayWindow(QMainWindow):
    def __init__(self, scanner, configuration, daemon_mode=False):
        super().__init__()
        self.daemon_mode = daemon_mode

        self.setWindowTitle("MTGA Draft Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Narrow enough to live beside Arena permanently. A wide window is
        # what forces alt-tabbing, which defeats the point of an overlay.
        self.resize(OVERLAY_WIDTH, OVERLAY_HEIGHT)
        self.setMinimumWidth(320)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        header = DragHandle()
        header.setCursor(Qt.CursorShape.SizeAllCursor)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(8, 3, 4, 3)
        header_layout.setSpacing(4)
        self.title_label = QLabel("En attente d'un draft...")
        self.title_label.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.event_label = QLabel("")
        self.event_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.title_label, 1)
        header_layout.addWidget(self.event_label, 1)

        self.reload_button = self._header_button(
            "⟳",
            "Relire le fichier log depuis le début\n"
            "Reconstruit un draft déjà en cours",
            self.reload_log,
        )
        header_layout.addWidget(self.reload_button)
        header_layout.addWidget(
            self._header_button("✕", "Fermer l'overlay", self.close)
        )

        # Style the handle and its labels together: the central stylesheet
        # below would otherwise paint each label with the body background and
        # leave visible boxes in the title bar.
        header.setStyleSheet(
            "DragHandle { background: #2c3e50; border-top-left-radius: 5px;"
            " border-top-right-radius: 5px; }"
            "DragHandle QLabel { background: transparent; color: #ecf0f1; }"
        )
        layout.addWidget(header)

        # The advisor is the answer to "what do I take", so it sits at the top,
        # not off to one side where it needs looking for.
        self.advisor = AdvisorPanel()
        self.advisor.setStyleSheet("background: rgba(255,255,255,0.04);")
        layout.addWidget(self.advisor)

        # Lane, curve, pool and wheel on one line that never needs scrolling.
        self.info_strip = InfoStrip()
        layout.addWidget(self.info_strip)

        self.tabs = QTabWidget()
        self.pack_view = PackView()
        self.pool_view = PoolView()
        self.tabs.addTab(self.pack_view, "Booster")
        self.tabs.addTab(self.pool_view, "Pool")
        layout.addWidget(self.tabs, 1)

        # Child of the central widget, not a top-level window: Wayland refuses
        # to position a top-level surface, so a floating popup never lands
        # where it is asked to.
        self.preview = CardPreview(central)
        self._cards_by_name = {}
        self.pack_view.card_hovered.connect(self._on_card_hovered)
        self.pack_view.card_left.connect(self.preview.hide_card)
        self.pool_view.card_hovered.connect(self._on_card_hovered)
        self.pool_view.card_left.connect(self.preview.hide_card)
        self.advisor.card_hovered.connect(self._on_name_hovered)
        self.advisor.card_left.connect(self.preview.hide_card)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(0)
        self.status_label = QLabel("Démarrage...")
        self.status_label.setStyleSheet(
            "background: transparent; color: #95a5a6; padding: 4px; font-size: 12px;"
        )
        footer_layout.addWidget(self.status_label, 1)
        # A frameless window has no border to grab; without this the overlay
        # cannot be resized at all under Wayland.
        footer_layout.addWidget(QSizeGrip(footer), 0, Qt.AlignmentFlag.AlignBottom)
        footer.setStyleSheet("background: rgba(18,18,18,235);")
        layout.addWidget(footer)

        central.setStyleSheet(
            "QWidget { background: rgba(18,18,18,242); color: #ecf0f1; }"
            "QTreeView { background: transparent; border: none; font-size: 13px; }"
            "QTreeView::item { padding: 3px; }"
            "QHeaderView::section { background: #34495e; color: #ecf0f1;"
            " padding: 4px; border: none; font-weight: bold; }"
            "QTabWidget::pane { border: none; }"
            "QTabBar::tab { background: #2c3e50; color: #bdc3c7; padding: 5px 14px; }"
            "QTabBar::tab:selected { background: #34495e; color: #ecf0f1;"
            " font-weight: bold; }"
        )

        self.bridge = DraftBridge(scanner, configuration, parent=self)
        self.bridge.snapshot_ready.connect(self.on_snapshot)
        self.bridge.status_changed.connect(self.status_label.setText)
        self.bridge.error.connect(lambda msg: self.status_label.setText(f"Erreur : {msg}"))
        self.bridge.start()

        self._drag_position = None
        self._mode = None

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
        self._cards_by_name = {
            card.get(constants.DATA_FIELD_NAME): card
            for card in list(snapshot.pack_cards or []) + list(snapshot.taken_cards or [])
        }
        self.advisor.update_recommendations(snapshot.recommendations)
        self.info_strip.update_from(snapshot)
        self.pack_view.update_pack(snapshot)
        self.pool_view.update_pool(snapshot)
        self.tabs.setTabText(1, f"Pool ({len(snapshot.taken_cards or [])})")
        self._apply_mode(snapshot)

        taken = len(snapshot.taken_cards or [])
        colours = snapshot.active_filter
        plural = "s" if taken > 1 else ""
        self.status_label.setText(
            f"{taken} carte{plural} prise{plural}  |  archétype : {colours}"
        )
        self.reload_button.setEnabled(True)
        if self.daemon_mode and snapshot.is_drafting and not self.isVisible():
            self.show()

    @staticmethod
    def _header_button(glyph, tooltip, slot):
        button = QPushButton(glyph)
        button.setToolTip(tooltip)
        button.setFixedSize(24, 22)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QPushButton { background: transparent; color: #ecf0f1; border: none;"
            " font-size: 14px; }"
            "QPushButton:hover { background: rgba(255,255,255,0.15);"
            " border-radius: 3px; }"
        )
        button.clicked.connect(slot)
        return button

    def reload_log(self):
        """
        Re-read the whole Player.log from the start.

        clear_draft(True) resets every read offset to zero, so a draft already
        in progress when the overlay starts is rebuilt pick by pick. This is
        the only way to join a draft midway without restarting it.

        Measured at around 30 seconds, most of it spent loading the set
        dataset, so the button says so: an unexplained wait reads as a freeze.
        """
        self.reload_button.setEnabled(False)
        self.status_label.setText("Relecture du log, jusqu'à 30 s...")
        self.bridge.full_rescan()

    def _on_card_hovered(self, card):
        self.preview.show_card(card, QCursor.pos())

    def _on_name_hovered(self, card_name):
        """The advisor only carries a name, so map it back to the card."""
        card = self._cards_by_name.get(card_name)
        if card is not None:
            self.preview.show_card(card, QCursor.pos())

    def _apply_mode(self, snapshot):
        """
        While a booster is on screen the pick is the only question, so the
        advisor leads and the Booster tab is selected. Once the draft is over
        there is no booster left and the pool becomes the useful view.
        """
        drafting = bool(snapshot.pack_cards)
        self.advisor.setVisible(drafting)
        if drafting and self._mode != "pick":
            self._mode = "pick"
            self.tabs.setCurrentIndex(0)
        elif not drafting and self._mode != "build":
            self._mode = "build"
            self.tabs.setCurrentIndex(1)

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
        self.preview.hide_card()
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
    # Pass the SetDictionary itself, not its .data mapping: ArenaScanner reads
    # set_list.special_events, so a bare dict makes every event lookup raise
    # and no draft is ever recognised.
    scanner = ArenaScanner(
        filename=path,
        set_list=limited_sets,
        retrieve_unknown=True,
        db_path=configuration.settings.database_location,
    )
    configuration.settings.arena_log_location = path

    # Same sequence as main.load_data: identify the event, attach its ratings
    # dataset, then replay the picks. Skipping it leaves every win rate and
    # advisor score at zero even though the cards themselves resolve.
    rebuild_draft(scanner, configuration)
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
