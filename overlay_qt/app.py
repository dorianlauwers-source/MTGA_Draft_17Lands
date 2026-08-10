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

from overlay_qt import prefs
from overlay_qt.bridge import DraftBridge
from overlay_qt.state import rebuild_draft
from overlay_qt.views.advisor_view import AdvisorPanel
from overlay_qt.views.card_preview import CardPreview
from overlay_qt.views.deck_view import DeckView
from overlay_qt.views.info_strip import InfoStrip
from overlay_qt.views.pack_view import PackView
from overlay_qt.views.pool_view import PoolView

logger = logging.getLogger(__name__)

ARENA_PROCESS = "MTGA.exe"
ARENA_POLL_MS = 5000

# Defaults live in overlay_qt.prefs; these are only the floor.
MIN_OVERLAY_WIDTH = 280


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

    def wheelEvent(self, event):
        """Scrolling the title bar adjusts transparency, no dialog needed."""
        window = self.window()
        if hasattr(window, "adjust_opacity"):
            window.adjust_opacity(1 if event.angleDelta().y() > 0 else -1)
            event.accept()
        else:
            super().wheelEvent(event)


class OverlayWindow(QMainWindow):
    def __init__(self, scanner, configuration, daemon_mode=False):
        super().__init__()
        self.daemon_mode = daemon_mode

        self.setWindowTitle("MTGA Draft Overlay")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # Narrow enough to live beside Arena permanently, and translucent so
        # the cards it covers stay readable. Both are remembered between runs.
        self.prefs = prefs.load()
        self.resize(self.prefs["width"], self.prefs["height"])
        self.setMinimumWidth(MIN_OVERLAY_WIDTH)

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

        self.opacity_button = self._header_button(
            "◐",
            "Transparence de l'overlay\n"
            "Molette sur la barre de titre pour ajuster finement",
            self._cycle_opacity,
        )
        header_layout.addWidget(self.opacity_button)

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
        self.deck_view = DeckView()
        self.tabs.addTab(self.pack_view, "Booster")
        self.tabs.addTab(self.pool_view, "Pool")
        self.tabs.addTab(self.deck_view, "Deck")
        self.tabs.currentChanged.connect(self._on_tab_changed)
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
        self.deck_view.card_hovered.connect(self._on_card_hovered)
        self.deck_view.card_left.connect(self.preview.hide_card)
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

        self._central = central
        self._header = header
        self._footer = footer
        self._apply_opacity(self.prefs["opacity"])

        self.bridge = DraftBridge(scanner, configuration, parent=self)
        self.bridge.snapshot_ready.connect(self.on_snapshot)
        self.bridge.status_changed.connect(self.status_label.setText)
        self.bridge.decks_ready.connect(self._on_decks_ready)
        self.bridge.error.connect(lambda msg: self.status_label.setText(f"Erreur : {msg}"))
        self.bridge.start()

        self._drag_position = None
        self._mode = None
        self._snapshot = None
        self._decks_stale = True

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
        self._snapshot = snapshot
        self._decks_stale = True
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

    # --- appearance ------------------------------------------------------

    def _apply_opacity(self, value):
        """
        Transparency through the background alpha, not setWindowOpacity.

        The Wayland plugin does not implement window opacity: the value is
        stored and nothing changes on screen. Painting the backgrounds with an
        alpha channel works everywhere, and it is the better result anyway,
        because the text stays fully opaque and readable while the cards behind
        show through.
        """
        alpha = int(255 * value)
        body = "rgba(18,18,18,%d)" % alpha
        chrome = "rgba(44,62,80,%d)" % alpha
        accent = "rgba(52,73,94,%d)" % alpha

        self._central.setStyleSheet(
            "QWidget { background: %s; color: #ecf0f1; }"
            "QTreeView { background: transparent; border: none; font-size: 13px; }"
            "QTreeView::item { padding: 3px; }"
            "QHeaderView::section { background: %s; color: #ecf0f1; padding: 4px;"
            " border: none; font-weight: bold; }"
            "QTabWidget::pane { border: none; }"
            "QTabBar::tab { background: %s; color: #bdc3c7; padding: 5px 14px; }"
            "QTabBar::tab:selected { background: %s; color: #ecf0f1;"
            " font-weight: bold; }" % (body, accent, chrome, accent)
        )
        self._header.setStyleSheet(
            "DragHandle { background: %s; border-top-left-radius: 5px;"
            " border-top-right-radius: 5px; }"
            "DragHandle QLabel { background: transparent; color: #ecf0f1; }" % chrome
        )
        self._footer.setStyleSheet("background: %s;" % body)

    def set_opacity(self, value):
        value = max(prefs.MIN_OPACITY, min(prefs.MAX_OPACITY, round(value, 2)))
        self.prefs["opacity"] = value
        self._apply_opacity(value)
        self.status_label.setText(f"Opacité : {value * 100:.0f}%")

    def _cycle_opacity(self):
        """Step down through opacity and wrap round, so one button is enough."""
        value = self.prefs["opacity"] - 0.15
        if value < prefs.MIN_OPACITY:
            value = prefs.MAX_OPACITY
        self.set_opacity(value)

    def adjust_opacity(self, steps):
        self.set_opacity(self.prefs["opacity"] + steps * prefs.OPACITY_STEP)

    def _on_tab_changed(self, index):
        """Build the decks the first time the Deck tab is looked at."""
        if self.tabs.widget(index) is self.deck_view:
            self._request_decks()

    def _request_decks(self):
        if not self._decks_stale or self._snapshot is None:
            return
        if len(self._snapshot.taken_cards or []) < 15:
            self.deck_view.set_decks({}, self._snapshot.active_filter)
            return
        self._decks_stale = False
        self.deck_view.set_pending()
        self.bridge.build_decks(self._snapshot)

    def _on_decks_ready(self, decks):
        active = self._snapshot.active_filter if self._snapshot else None
        self.deck_view.set_decks(decks, active)

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
            self.tabs.setCurrentWidget(self.deck_view)
            self._request_decks()

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
        self.prefs["width"] = self.width()
        self.prefs["height"] = self.height()
        prefs.save(self.prefs)
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
