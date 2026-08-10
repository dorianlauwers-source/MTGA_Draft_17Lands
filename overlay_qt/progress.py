"""
overlay_qt/progress.py
Qt replacement for the Tk-shaped progress contract in src/ui_progress.py.

src.file_extractor.FileExtractor inherits UIProgress, whose _update_status /
_update_progress call `self.status.set(...)`, `self.progress["value"] = ...`,
`self.ui.winfo_exists()` and `self.ui.after(0, cb)`. Rather than modify the
1350-line extractor, we hand it duck-typed stand-ins that emit Qt signals.

Qt queues signals emitted from a worker thread to the receiver's thread
automatically, which is exactly the marshaling UIProgress hand-rolls with
`after(0, ...)`.
"""

from PyQt6.QtCore import QObject, pyqtSignal


class ProgressReporter(QObject):
    """Emits what the extractor reports. Connect to it from the GUI thread."""

    status_changed = pyqtSignal(str)
    value_changed = pyqtSignal(float)

    def as_ui_progress_kwargs(self) -> dict:
        """
        Keyword arguments for anything inheriting src.ui_progress.UIProgress.

        `ui=None` is deliberate: UIProgress only touches `self.ui` to marshal
        onto the Tk main thread, and with signals we no longer need to. With
        `ui` unset, its worker-thread branch becomes a no-op and our shims do
        the reporting instead.
        """
        return {
            "progress": _ProgressBarShim(self),
            "status": _StatusVarShim(self),
            "ui": None,
        }


class _StatusVarShim:
    """Stands in for a tkinter.StringVar."""

    def __init__(self, reporter: ProgressReporter):
        self._reporter = reporter
        self._value = ""

    def set(self, value):
        self._value = str(value)
        self._reporter.status_changed.emit(self._value)

    def get(self):
        return self._value


class _ProgressBarShim:
    """
    Stands in for a ttk.Progressbar.

    UIProgress checks `winfo_exists` before writing, then assigns
    `self.progress["value"]`, so we implement both.
    """

    def __init__(self, reporter: ProgressReporter):
        self._reporter = reporter
        self._value = 0.0

    def winfo_exists(self):
        return True

    def __setitem__(self, key, value):
        if key == "value":
            self._value = float(value)
            self._reporter.value_changed.emit(self._value)

    def __getitem__(self, key):
        if key == "value":
            return self._value
        raise KeyError(key)
