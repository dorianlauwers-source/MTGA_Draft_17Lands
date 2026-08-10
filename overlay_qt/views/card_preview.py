"""
overlay_qt/views/card_preview.py
Card image shown on hover, and dismissed on leave.

Two things this gets right that the upstream tooltip does not.

Upstream binds the image to a click and CardToolTip.create() has no dismissal
path of its own: the window only closes when another card is clicked, so it
stays frozen over the game.

And it must be a child widget, not a top-level window. Wayland does not let an
application position a top-level surface, so a frameless popup moved with
move() either never appears where it was asked to or does not appear at all.
Living inside the overlay means positioning is a local-coordinate problem the
compositor has no say in.
"""

import logging

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel

from src import constants

logger = logging.getLogger(__name__)

IMAGE_HEADERS = {"User-Agent": "MTGADraftOverlay", "Accept": "image/*"}

PREVIEW_WIDTH = 232
PREVIEW_HEIGHT = 324
MARGIN = 6


def card_image_url(card):
    """Card images are stored as a list; take the front face."""
    if not card:
        return ""
    images = card.get(constants.DATA_SECTION_IMAGES) or []
    if isinstance(images, str):
        return images
    return images[0] if images else ""


class _LoaderSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)


class _ImageLoader(QRunnable):
    def __init__(self, url):
        super().__init__()
        self.url = url
        self.signals = _LoaderSignals()

    def run(self):
        try:
            import requests

            # Scryfall's CDN answers 400 to the default requests user agent.
            response = requests.get(self.url, timeout=8, headers=IMAGE_HEADERS)
            if response.status_code != 200:
                logger.debug("Card image %s returned %s", self.url, response.status_code)
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(response.content):
                self.signals.loaded.emit(self.url, pixmap)
        except Exception:
            logger.debug("Card image download failed: %s", self.url, exc_info=True)


class CardPreview(QLabel):
    """Floating image inside the overlay, following what the mouse points at."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet(
            "background: rgba(10,10,10,238); border: 1px solid #555;"
            " border-radius: 8px; color: #bdc3c7;"
        )
        self.resize(PREVIEW_WIDTH, PREVIEW_HEIGHT)
        self.hide()

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._cache = {}
        self._current_url = None

    def show_card(self, card, anchor_global=None):
        url = card_image_url(card)
        if not url:
            self.hide_card()
            return

        self._current_url = url
        self._reposition(anchor_global)

        pixmap = self._cache.get(url)
        if pixmap is not None:
            self._apply(pixmap)
        else:
            self.setText("Chargement...")
            loader = _ImageLoader(url)
            loader.signals.loaded.connect(self._on_loaded)
            self._pool.start(loader)

        self.show()
        self.raise_()

    def hide_card(self):
        self._current_url = None
        self.hide()

    # --- placement ------------------------------------------------------

    def _reposition(self, anchor_global):
        """
        Keep the image inside the overlay and away from the row being pointed
        at: hovering the top half shows it at the bottom and the other way
        round, so the card you are reading is never the one covered.
        """
        parent = self.parentWidget()
        if parent is None:
            return
        width, height = parent.width(), parent.height()
        left = max(MARGIN, (width - PREVIEW_WIDTH) // 2)

        top = height - PREVIEW_HEIGHT - MARGIN
        if anchor_global is not None:
            local_y = parent.mapFromGlobal(anchor_global).y()
            if local_y > height // 2:
                top = MARGIN
        self.move(left, max(MARGIN, top))

    # --- image ----------------------------------------------------------

    def _on_loaded(self, url, pixmap):
        self._cache[url] = pixmap
        # The pointer may have moved on while the image was downloading.
        if url == self._current_url and self.isVisible():
            self._apply(pixmap)

    def _apply(self, pixmap):
        self.setPixmap(
            pixmap.scaled(
                PREVIEW_WIDTH,
                PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
