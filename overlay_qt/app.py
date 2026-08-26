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
from datetime import datetime

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
from overlay_qt.state import (detailed_logs_enabled, ensure_draft,
                              read_submitted_deck, rebuild_draft)
from overlay_qt.views.advisor_view import AdvisorPanel
from overlay_qt.views.card_preview import CardPreview
from overlay_qt.views.deck_view import DeckView
from overlay_qt.views.info_strip import InfoStrip
from overlay_qt.views.pack_view import PackView
from overlay_qt.views.pool_view import PoolView

logger = logging.getLogger(__name__)

# Identity presented to the compositor. On Wayland the app_id comes from
# desktopFileName, and without it the window reports something generic like
# "python3": a KWin rule cannot target it, which is what makes "keep above"
# unreliable when another window is clicked.
APPLICATION_NAME = "MTGA Draft Overlay"
DESKTOP_FILE_NAME = "mtga-overlay-qt"

NO_EVENT_LABEL = "choisir un draft  \u25be"

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
        self._restore_position()

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

        # Two lines: at 340px wide the status and the event name were fighting
        # for the same row and "Construction du deck" came out truncated.
        title_column = QVBoxLayout()
        title_column.setSpacing(0)
        self.title_label = QLabel("En attente d'un draft...")
        self.title_label.setFont(QFont("Sans Serif", 11, QFont.Weight.Bold))
        self.event_button = QPushButton(NO_EVENT_LABEL)
        self.event_button.setFlat(True)
        self.event_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.event_button.setToolTip(
            "Choisir le draft à suivre\n"
            "Le log en cours, ou un draft déjà terminé"
        )
        self.event_button.setStyleSheet(
            "QPushButton { background: transparent; color: #bdc3c7; border: none;"
            " font-size: 10px; text-align: left; padding: 0px; }"
            "QPushButton:hover { color: #ecf0f1; text-decoration: underline; }"
        )
        self.event_button.clicked.connect(self._show_draft_menu)
        title_column.addWidget(self.title_label)
        title_column.addWidget(self.event_button)
        header_layout.addLayout(title_column, 1)

        self.minimise_button = self._header_button(
            "\u2013", "Réduire dans la barre des tâches", self.showMinimized
        )
        header_layout.addWidget(self.minimise_button)

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

        # Without detailed logs Arena writes no draft data at all, so the
        # overlay would sit there looking broken with nothing to explain it.
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setTextFormat(Qt.TextFormat.RichText)
        self.warning_label.setStyleSheet(
            "background: #8e44ad; color: white; padding: 6px; font-size: 12px;"
            " font-weight: bold;"
        )
        self.warning_label.hide()
        layout.addWidget(self.warning_label)

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
        self.pack_view.set_hidden_columns(self.prefs.get("hidden_columns"))
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

        self.configuration = configuration
        self.bridge = DraftBridge(scanner, configuration, parent=self)
        self.bridge.snapshot_ready.connect(self.on_snapshot)
        self.bridge.status_changed.connect(self.status_label.setText)
        self.bridge.decks_ready.connect(self._on_decks_ready)
        self.bridge.error.connect(lambda msg: self.status_label.setText(f"Erreur : {msg}"))
        self.bridge.start()
        self._check_detailed_logs()

        self._drag_position = None
        self._mode = None
        self._snapshot = None
        self._decks_stale = True
        self._decks_pool_size = -1

        self.arena_timer = QTimer(self)
        if daemon_mode:
            self.arena_timer.timeout.connect(self.check_arena)
            self.arena_timer.start(ARENA_POLL_MS)
            QTimer.singleShot(0, self.check_arena)

    # --- data -----------------------------------------------------------

    def on_snapshot(self, snapshot):
        self.title_label.setText(snapshot.status_text)
        label = f"{snapshot.event_set} {snapshot.event_type}".strip()
        # Always leave something to click: with no event the menu would be
        # unreachable, and choosing a recorded draft is exactly what you want
        # when the live log has nothing in it.
        self.event_button.setText(f"{label}  \u25be" if label else NO_EVENT_LABEL)
        self._snapshot = snapshot
        # Only worth rebuilding when the pool actually changed: suggest_deck
        # runs four builders and a Monte Carlo simulation per candidate.
        pool_size = len(snapshot.taken_cards or [])
        if pool_size != self._decks_pool_size:
            self._decks_pool_size = pool_size
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
        # Keep the deck in step with the pool while the tab is being looked at,
        # rather than freezing on whatever it held when it was opened.
        if self.tabs.currentWidget() is self.deck_view:
            self._request_decks()

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

    def _restore_position(self):
        """
        Put the window back where it was, when the platform allows it.

        Wayland hands placement to the compositor: move() is silently ignored
        and QWidget.pos() reports what Qt intended rather than where the window
        is. Restoring there is impossible from inside the application, so we
        skip it instead of writing a value that would only ever be fiction.
        """
        if not self._position_is_ours():
            return
        x, y = self.prefs.get("x"), self.prefs.get("y")
        if x is not None and y is not None:
            self.move(x, y)

    @staticmethod
    def _position_is_ours() -> bool:
        application = QApplication.instance()
        return bool(application) and application.platformName() != "wayland"

    def available_drafts(self):
        """
        The live log plus any draft already recorded, most recent first.

        ArenaScanner writes a DraftLog_<SET>_<EVENT>_<id>.log per draft, and
        the orchestrator can be pointed at any of them, which is how upstream's
        history dropdown works. Same idea, reached by clicking the event name.
        """
        entries = []
        live = self.configuration.settings.arena_log_location
        if live and os.path.exists(live):
            entries.append(("\u25cf  En cours (Arena)", live))

        folder = constants.DRAFT_LOG_FOLDER
        recorded = []
        if os.path.isdir(folder):
            for name in os.listdir(folder):
                if not (name.startswith("DraftLog_") and name.endswith(".log")):
                    continue
                path = os.path.join(folder, name)
                try:
                    recorded.append((os.path.getmtime(path), name, path))
                except OSError:
                    continue
        recorded.sort(reverse=True)

        for mtime, name, path in recorded:
            parts = name[:-4].split("_")
            card_set = parts[1] if len(parts) > 1 else "?"
            event = parts[2] if len(parts) > 2 else "Draft"
            when = datetime.fromtimestamp(mtime).strftime("%d/%m %H:%M")
            entries.append((f"    {card_set} {event}  ({when})", path))
        return entries

    def _show_draft_menu(self):
        menu = QMenu(self)
        entries = self.available_drafts()
        if not entries:
            menu.addAction("Aucun draft disponible").setEnabled(False)
        else:
            current = self.configuration.settings.arena_log_location
            for label, path in entries:
                action = menu.addAction(label)
                action.setCheckable(True)
                action.setChecked(os.path.realpath(path) == os.path.realpath(current or ""))
                action.triggered.connect(
                    lambda _checked, p=path, l=label: self._switch_draft(p, l)
                )
        menu.exec(self.event_button.mapToGlobal(
            self.event_button.rect().bottomLeft()))

    def _switch_draft(self, path, label):
        """Point the scanner at another log. The rest follows on its own."""
        self.configuration.settings.arena_log_location = path
        self.status_label.setText(f"Bascule vers {label.strip()}...")
        self._decks_stale = True
        self.bridge.set_log_file(path)
        self._check_detailed_logs()

    def _check_detailed_logs(self):
        """
        Warn when Arena is not writing plugin-support logs.

        The setting does not follow the account between installs: enabling it
        on Flatpak Steam leaves a native Steam install disabled, and the
        overlay then has nothing whatsoever to read.
        """
        enabled = detailed_logs_enabled(self.configuration.settings.arena_log_location)
        if enabled is False:
            self.warning_label.setText(
                "Logs détaillés DÉSACTIVÉS dans cette installation d'Arena.<br>"
                "Aucune donnée de draft n'est écrite.<br>"
                "Arena &rarr; roue crantée &rarr; Compte &rarr; "
                "<i>Detailed Logs (Plugin Support)</i>, puis relancez le jeu."
            )
            self.warning_label.show()
        else:
            self.warning_label.hide()

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
        self._check_detailed_logs()
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
        self._load_submitted_deck()

    def _load_submitted_deck(self):
        """
        What Arena has on record for this event, for the comparison view.

        Only available once the deck is submitted: during the build Arena
        reports an empty MainDeck, so there is nothing to follow live.
        """
        if self._snapshot is None:
            return
        entries = read_submitted_deck(
            self.log_worker_path(), self._snapshot.event_string
        )
        self.deck_view.set_submitted_deck(entries, self._lookup_card)

    def log_worker_path(self):
        return self.configuration.settings.arena_log_location

    def _lookup_card(self, card_id):
        cards = self.bridge.scanner.set_data.get_data_by_id([str(card_id)])
        return cards[0] if cards else None

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
        advisor leads and the Booster tab is selected. Once there is no booster
        the deck becomes the useful view.

        Sealed never has a booster: the whole pool arrives at once, so the
        booster tab and the pick advisor are hidden outright rather than left
        there permanently empty.
        """
        drafting = bool(snapshot.pack_cards)
        sealed = snapshot.is_sealed

        self.advisor.setVisible(drafting and not sealed)
        self.tabs.setTabVisible(0, not sealed)
        self.info_strip.setVisible(not sealed or bool(snapshot.taken_cards))

        mode = "pick" if drafting and not sealed else "build"
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "pick":
            self.tabs.setCurrentWidget(self.pack_view)
        else:
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
        self.prefs["hidden_columns"] = self.pack_view.hidden_columns()
        if self._position_is_ours():
            self.prefs["x"], self.prefs["y"] = self.pos().x(), self.pos().y()
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

    # Catch up without discarding what the scanner restored from disk. A full
    # rebuild here used to wipe the persisted draft, history included, which
    # cannot be recovered once Arena has rotated the log.
    ensure_draft(scanner, configuration)
    return scanner


# =============================================================================
#  Installation : service utilisateur systemd et entree de menu
# =============================================================================

SERVICE_NAME = "mtga-overlay-qt.service"
DESKTOP_NAME = "mtga-overlay-qt.desktop"

SERVICE_UNIT = """[Unit]
Description=Overlay MTG Arena (Qt6)
Documentation=file://{project}/README.overlay.md
After=default.target

[Service]
Type=simple
# WorkingDirectory matters: upstream derives BASE_DIR from the current
# directory when not frozen, so Sets/, Temp/ and Logs/ would otherwise land
# wherever systemd happened to start us.
WorkingDirectory={project}
ExecStart={python} -m overlay_qt.app --daemon
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

DESKTOP_ENTRY = """[Desktop Entry]
Type=Application
Name=MTGA Draft Overlay
Comment=Overlay de draft pour MTG Arena
Exec={python} -m overlay_qt.app
Path={project}
Icon=applications-games
Terminal=false
Categories=Game;
"""


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def install_service():
    """
    Install a user service that follows the session.

    Not Steam launch options: under Flatpak Steam those run inside the
    pressure-vessel container, where neither the host Python nor PyQt6 exists.
    A user service on the host side works whatever the launcher, and --daemon
    keeps the window hidden until Arena actually appears.
    """
    import subprocess

    project = _project_root()
    fields = {"python": sys.executable, "project": project}

    unit = _write(
        os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}"),
        SERVICE_UNIT.format(**fields),
    )
    desktop = _write(
        os.path.expanduser(f"~/.local/share/applications/{DESKTOP_NAME}"),
        DESKTOP_ENTRY.format(**fields),
    )
    print(f"Service    : {unit}")
    print(f"Menu       : {desktop}")

    for command in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", SERVICE_NAME],
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            if result.returncode:
                print(f"  {' '.join(command)} -> {result.stderr.strip()}", file=sys.stderr)
                return 1
        except (OSError, subprocess.SubprocessError) as error:
            print(f"  {' '.join(command)} a echoue : {error}", file=sys.stderr)
            return 1

    print("\nActif. L'overlay apparaitra au lancement d'Arena et disparaitra a sa fermeture.")
    print(f"Journal    : journalctl --user -u {SERVICE_NAME} -f")
    print(f"Desactiver : systemctl --user disable --now {SERVICE_NAME}")
    return 0


KWIN_RULE_ID = "mtga-overlay-qt-keep-above"


def install_kwin_rule():
    """
    Ask KWin to keep the overlay above other windows, for good.

    Wayland gives stacking to the compositor: WindowStaysOnTopHint is a
    request, and a game raising itself wins over it. The keep-above toggle in
    the window menu is per-window and transient, and our window is frameless
    so it has no window menu to begin with. A rule matching the app_id is the
    only thing that survives clicking on Arena.

    Needs the app_id to be stable, which is why the application sets
    desktopFileName; without it the window reports something generic and the
    rule would match every Python program.
    """
    import configparser
    import subprocess

    path = os.path.expanduser("~/.config/kwinrulesrc")
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    if not parser.has_section("General"):
        parser.add_section("General")
    existing = [r for r in (parser.get("General", "rules", fallback="") or "").split(",") if r]

    if KWIN_RULE_ID not in existing:
        existing.append(KWIN_RULE_ID)
    parser.set("General", "rules", ",".join(existing))
    parser.set("General", "count", str(len(existing)))

    if not parser.has_section(KWIN_RULE_ID):
        parser.add_section(KWIN_RULE_ID)
    for key, value in {
        "Description": "MTGA Draft Overlay: keep above",
        "above": "true",
        "aboverule": "2",            # 2 = Force, the compositor stops arbitrating
        "wmclass": DESKTOP_FILE_NAME,
        "wmclasscomplete": "false",
        "wmclassmatch": "1",         # 1 = exact match on the app_id
    }.items():
        parser.set(KWIN_RULE_ID, key, value)

    with open(path, "w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)
    print(f"Regle ecrite : {path}  [{KWIN_RULE_ID}]")

    try:
        subprocess.run(["qdbus", "org.kde.KWin", "/KWin", "reconfigure"],
                       capture_output=True, timeout=15)
        print("KWin rechargé.")
    except (OSError, subprocess.SubprocessError):
        print("Rechargez KWin manuellement, ou reconnectez-vous.", file=sys.stderr)

    print("L'overlay doit maintenant rester au-dessus d'Arena, meme au clic.")
    return 0


def uninstall_kwin_rule():
    import configparser
    import subprocess

    path = os.path.expanduser("~/.config/kwinrulesrc")
    parser = configparser.RawConfigParser()
    parser.optionxform = str
    parser.read(path, encoding="utf-8")

    remaining = [
        r for r in (parser.get("General", "rules", fallback="") or "").split(",")
        if r and r != KWIN_RULE_ID
    ]
    parser.set("General", "rules", ",".join(remaining))
    parser.set("General", "count", str(len(remaining)))
    parser.remove_section(KWIN_RULE_ID)
    with open(path, "w", encoding="utf-8") as handle:
        parser.write(handle, space_around_delimiters=False)

    try:
        subprocess.run(["qdbus", "org.kde.KWin", "/KWin", "reconfigure"],
                       capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        pass
    print("Regle KWin retiree.")
    return 0


def uninstall_service():
    import subprocess

    for command in (
        ["systemctl", "--user", "disable", "--now", SERVICE_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ):
        try:
            subprocess.run(command, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass

    for path in (
        os.path.expanduser(f"~/.config/systemd/user/{SERVICE_NAME}"),
        os.path.expanduser(f"~/.local/share/applications/{DESKTOP_NAME}"),
    ):
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"Supprime : {path}")
            except OSError as error:
                print(f"Suppression impossible : {error}", file=sys.stderr)
                return 1
    print("Desinstalle.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Overlay Qt6 pour les drafts MTG Arena")
    parser.add_argument("-f", "--file", help="Chemin vers Player.log")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Masque la fenêtre tant qu'Arena n'est pas lancé",
    )
    parser.add_argument("--debug", action="store_true", help="Journalisation détaillée")
    parser.add_argument(
        "--install-service",
        action="store_true",
        help="Installe le service utilisateur systemd et l'entrée de menu",
    )
    parser.add_argument(
        "--uninstall-service", action="store_true", help="Retire le service"
    )
    parser.add_argument(
        "--install-kwin-rule",
        action="store_true",
        help="Force KWin à garder l'overlay au-dessus des autres fenêtres",
    )
    parser.add_argument(
        "--uninstall-kwin-rule", action="store_true", help="Retire la règle KWin"
    )
    parser.add_argument(
        "--x11",
        action="store_true",
        help="Forcer XWayland, seul moyen de retrouver la position de la fenêtre",
    )
    args = parser.parse_args(argv)

    if args.install_service:
        return install_service()
    if args.uninstall_service:
        return uninstall_service()
    if args.install_kwin_rule:
        return install_kwin_rule()
    if args.uninstall_kwin_rule:
        return uninstall_kwin_rule()

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    if args.x11:
        # Wayland refuses to let an application place its own top-level
        # surface, so the saved position can only be honoured under XWayland.
        # The trade is losing native Wayland rendering.
        os.environ["QT_QPA_PLATFORM"] = "xcb"

    configuration, _ = read_configuration()
    scanner = build_scanner(configuration, args.file)

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APPLICATION_NAME)
    app.setApplicationDisplayName(APPLICATION_NAME)
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    app.setQuitOnLastWindowClosed(not args.daemon)
    window = OverlayWindow(scanner, configuration, daemon_mode=args.daemon)
    if not args.daemon:
        window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
