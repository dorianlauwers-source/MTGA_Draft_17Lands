"""
overlay_qt/views/card_preview.py
Card image shown on hover, and dismissed on leave.

Upstream binds the image to a click and CardToolTip.create() has no dismissal
path of its own: the window only closes when another card is clicked, so it
stays frozen over the game. Hover-to-show with an explicit hide on leave is
both the expected behaviour and the fix.
"""

import logging

from PyQt6.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QLabel

from src import constants

logger = logging.getLogger(__name__)

PREVIEW_WIDTH = 240
PREVIEW_HEIGHT = 336
CURSOR_OFFSET = 24


def card_image_url(card):
    """Card images are stored as a list; take the front face."""
    images = card.get(constants.DATA_SECTION_IMAGES) or []
    if isinstance(images, str):
        return images
    return images[0] if images else ""


class _LoaderSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)


class _ImageLoader(QRunnable):
    def __init__(self, url, verify_tls=True):
        super().__init__()
        self.url = url
        self.verify_tls = verify_tls
        self.signals = _LoaderSignals()

    def run(self):
        try:
            import requests

            response = requests.get(self.url, timeout=8, verify=self.verify_tls)
            if response.status_code != 200:
                return
            pixmap = QPixmap()
            if pixmap.loadFromData(response.content):
                self.signals.loaded.emit(self.url, pixmap)
        except Exception:
            logger.debug("Card image download failed: %s", self.url, exc_info=True)


class CardPreview(QLabel):
    """Frameless image that follows the cursor and hides as soon as it leaves."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.ToolTip
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.resize(PREVIEW_WIDTH, PREVIEW_HEIGHT)

        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._cache = {}
        self._current_url = None

    def show_card(self, card, global_position):
        url = card_image_url(card) if card else ""
        if not url:
            self.hide_card()
            return

        self._current_url = url
        self.move(global_position.x() + CURSOR_OFFSET,
                  max(0, global_position.y() - PREVIEW_HEIGHT // 2))

        pixmap = self._cache.get(url)
        if pixmap is not None:
            self._apply(pixmap)
            self.show()
            return

        loader = _ImageLoader(url)
        loader.signals.loaded.connect(self._on_loaded)
        self._pool.start(loader)

    def hide_card(self):
        self._current_url = None
        self.hide()

    def _on_loaded(self, url, pixmap):
        self._cache[url] = pixmap
        # The cursor may have moved on while the image was downloading.
        if url == self._current_url:
            self._apply(pixmap)
            self.show()

    def _apply(self, pixmap):
        self.setPixmap(
            pixmap.scaled(
                PREVIEW_WIDTH,
                PREVIEW_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
