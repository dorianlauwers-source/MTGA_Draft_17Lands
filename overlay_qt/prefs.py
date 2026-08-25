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
    # Only honoured on X11/XWayland: Wayland gives placement to the compositor
    # and an application cannot position its own top-level surface.
    "x": None,
    "y": None,
    # Columns hidden in the booster table, by index.
    "hidden_columns": None,
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

    # Every value is defended: a hand-edited or truncated file must not stop
    # the overlay from starting, and this file is written on close, when the
    # process may be going away mid-write.
    def _number(key, cast, floor, ceiling=None):
        try:
            value = cast(prefs[key])
        except (TypeError, ValueError):
            value = cast(DEFAULTS[key])
        value = max(floor, value)
        return value if ceiling is None else min(ceiling, value)

    prefs["opacity"] = _number("opacity", float, MIN_OPACITY, MAX_OPACITY)
    prefs["width"] = _number("width", int, 280)
    prefs["height"] = _number("height", int, 320)
    for axis in ("x", "y"):
        try:
            prefs[axis] = int(prefs[axis]) if prefs[axis] is not None else None
        except (TypeError, ValueError):
            prefs[axis] = None
    columns = prefs.get("hidden_columns")
    if isinstance(columns, list):
        prefs["hidden_columns"] = [int(c) for c in columns if isinstance(c, int)]
    else:
        prefs["hidden_columns"] = None
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
