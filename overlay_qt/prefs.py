"""
overlay_qt/prefs.py
Overlay-only preferences: size and opacity.

Kept in a file of our own rather than added to the upstream Settings model, so
following upstream stays a clean merge. Written atomically because it is saved
on close, when the process may be going away.
"""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

DEFAULTS = {
    "width": 340,
    "height": 720,
    # Enough to read the table, sheer enough to see the cards behind it.
    "opacity": 0.88,
}

MIN_OPACITY = 0.35
MAX_OPACITY = 1.0
OPACITY_STEP = 0.05


def _path():
    from src import constants

    return os.path.join(constants.TEMP_FOLDER, "overlay_qt_prefs.json")


def load() -> dict:
    prefs = dict(DEFAULTS)
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            prefs.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass

    prefs["opacity"] = max(MIN_OPACITY, min(MAX_OPACITY, float(prefs["opacity"])))
    prefs["width"] = max(280, int(prefs["width"]))
    prefs["height"] = max(320, int(prefs["height"]))
    return prefs


def save(prefs: dict) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handle, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump({key: prefs.get(key, value) for key, value in DEFAULTS.items()},
                      file, indent=2)
        os.replace(tmp, path)
    except OSError:
        logger.debug("Could not save overlay preferences", exc_info=True)
