"""
overlay_qt/bridge.py
Bridges the upstream DraftOrchestrator to Qt signals.

Upstream shape (src/ui/orchestrator.py, no Tk in it):
  * a daemon thread polling the Player.log every 0.5 s
  * a queue.Queue carrying {"status": ...} dicts and the "REFRESH" sentinel
  * a Tk consumer polling that queue with root.after(100, ...)

Here the consumer is a QTimer, and the expensive part of a refresh
(DraftAdvisor.evaluate_pack, which rebuilds candidate decks per card from pack
3 onwards) runs on a QThreadPool worker so the overlay never freezes.
"""

import logging
import queue

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal

from src.ui.orchestrator import DraftOrchestrator

from overlay_qt.state import (build_snapshot, rebuild_draft, suggest_decks,
                              switch_draft_log, take_raw_snapshot)

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 100


class _WorkerSignals(QObject):
    finished = pyqtSignal(object)
    failed = pyqtSignal(str)


class _SnapshotWorker(QRunnable):
    """Computes a DraftSnapshot off the GUI thread."""

    def __init__(self, scanner, configuration):
        super().__init__()
        self.scanner = scanner
        self.configuration = configuration
        self.signals = _WorkerSignals()

    def run(self):
        try:
            raw = take_raw_snapshot(self.scanner, blocking=True)
            snapshot = build_snapshot(self.scanner, self.configuration, raw=raw)
            self.signals.finished.emit(snapshot)
        except Exception as error:            # never kill the pool thread
            logger.exception("Snapshot computation failed")
            self.signals.failed.emit(str(error))


class _RescanWorker(QRunnable):
    """Resets the read offsets and asks for a full scan, off the GUI thread."""

    def __init__(self, scanner, orchestrator, configuration=None):
        super().__init__()
        self.scanner = scanner
        self.orchestrator = orchestrator
        self.configuration = configuration
        self.signals = _WorkerSignals()

    def run(self):
        try:
            with self.scanner.lock:
                rebuild_draft(self.scanner, self.configuration)
            self.orchestrator.trigger_full_scan()
        except Exception as error:
            logger.exception("Full rescan failed")
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit(None)


class _DeckWorker(QRunnable):
    """Runs the upstream deck builder off the GUI thread."""

    def __init__(self, snapshot, configuration):
        super().__init__()
        self.snapshot = snapshot
        self.configuration = configuration
        self.signals = _WorkerSignals()

    def run(self):
        try:
            self.signals.finished.emit(suggest_decks(self.snapshot, self.configuration))
        except Exception as error:
            logger.exception("Deck suggestion failed")
            self.signals.failed.emit(str(error))


class _SwitchWorker(QRunnable):
    """Points the scanner at another log, off the GUI thread."""

    def __init__(self, scanner, configuration, path):
        super().__init__()
        self.scanner = scanner
        self.configuration = configuration
        self.path = path
        self.signals = _WorkerSignals()

    def run(self):
        try:
            with self.scanner.lock:
                switch_draft_log(self.scanner, self.path, self.configuration)
        except Exception as error:
            logger.exception("Draft log switch failed")
            self.signals.failed.emit(str(error))
        finally:
            self.signals.finished.emit(None)


class DraftBridge(QObject):
    """
    Owns the upstream orchestrator and turns it into Qt signals.

    snapshot_ready carries a DraftSnapshot; status_changed carries the
    orchestrator's own progress strings.
    """

    snapshot_ready = pyqtSignal(object)
    status_changed = pyqtSignal(str)
    decks_ready = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, scanner, configuration, parent=None):
        super().__init__(parent)
        self.scanner = scanner
        self.configuration = configuration
        self._pool = QThreadPool(self)
        # One refresh at a time: they are not additive, and evaluate_pack is
        # heavy enough that queueing several would only add latency.
        self._pool.setMaxThreadCount(1)
        self._busy = False
        self._refresh_pending = False
        # Control actions must never queue behind a snapshot on the single
        # snapshot thread, otherwise a reload waits for the current refresh.
        self._control_pool = QThreadPool(self)
        self._control_pool.setMaxThreadCount(1)

        self.orchestrator = DraftOrchestrator(
            scanner, configuration, refresh_callback=self._on_orchestrator_refresh
        )

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_INTERVAL_MS)
        self._timer.timeout.connect(self._drain_queue)

    # --- lifecycle ------------------------------------------------------

    def start(self):
        if not self.orchestrator.is_alive():
            self.orchestrator.start()
        self._timer.start()
        self.request_refresh()

    def stop(self):
        self._timer.stop()
        try:
            self.orchestrator.stop()
        except Exception:
            logger.exception("Failed to stop the orchestrator")
        self._pool.waitForDone(2000)

    # --- orchestrator plumbing ------------------------------------------

    def _on_orchestrator_refresh(self, *_args, **_kwargs):
        """
        Called from the orchestrator's own thread. Only sets a flag: emitting
        a signal here would be safe, but the queue drain below is the single
        place that decides when a refresh actually happens.
        """
        self._refresh_pending = True

    def _drain_queue(self):
        refresh = self._refresh_pending
        self._refresh_pending = False

        while True:
            try:
                message = self.orchestrator.update_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(message, dict) and "status" in message:
                self.status_changed.emit(str(message["status"]))
            elif message == "REFRESH":
                refresh = True

        if refresh:
            self.request_refresh()

    # --- refresh --------------------------------------------------------

    def request_refresh(self):
        if self._busy:
            self._refresh_pending = True      # coalesce, run again when free
            return
        self._busy = True
        worker = _SnapshotWorker(self.scanner, self.configuration)
        worker.signals.finished.connect(self._on_snapshot)
        worker.signals.failed.connect(self._on_failure)
        self._pool.start(worker)

    def _on_snapshot(self, snapshot):
        self._busy = False
        if snapshot is not None:
            self.snapshot_ready.emit(snapshot)

    def _on_failure(self, message):
        self._busy = False
        self.error.emit(message)

    # --- passthrough for the UI -----------------------------------------

    def set_log_file(self, path):
        """Switch logs on the control thread, dataset binding included."""
        self.status_changed.emit("Bascule de draft...")
        worker = _SwitchWorker(self.scanner, self.configuration, path)
        worker.signals.finished.connect(lambda _: self._on_rescan_done())
        worker.signals.failed.connect(self._on_failure)
        self._control_pool.start(worker)

    def full_rescan(self):
        """
        Re-read the log from byte zero, rebuilding a draft already in progress.

        The work runs off the GUI thread on purpose. clear_draft() needs the
        scanner lock, which the orchestrator holds for as long as it takes to
        load the set dataset; measured at 27 seconds on a cold MSH cache. Taking
        that lock from the GUI thread froze the whole overlay for the duration.
        """
        self.status_changed.emit("Relecture du log...")
        worker = _RescanWorker(self.scanner, self.orchestrator, self.configuration)
        worker.signals.finished.connect(lambda _: self._on_rescan_done())
        self._control_pool.start(worker)

    def build_decks(self, snapshot):
        """Suggest 40-card decks from the pool. Cheap enough to redo on demand."""
        worker = _DeckWorker(snapshot, self.configuration)
        worker.signals.finished.connect(self.decks_ready)
        worker.signals.failed.connect(self._on_failure)
        self._control_pool.start(worker)

    def _on_rescan_done(self):
        # If the rescan turned up nothing new the orchestrator never queues a
        # REFRESH, so ask for one explicitly rather than leave a stale message.
        self.request_refresh()
