"""Bounded, latest-only publication of detached routine report snapshots."""
from copy import deepcopy
import threading

from .report_snapshots import PreparedReport, PairedReportWriter


class ReportPublicationError(RuntimeError):
    """Report publication failed or the publisher is unavailable."""


class CoalescingReportPublisher:
    """One pending report plus an in-flight write; urgent reports publish last.

    writer(path, report) owns atomic publication. close() flushes and joins:
    an in-progress filesystem write cannot be forcibly cancelled.
    """

    def __init__(self, path, *, writer):
        if not callable(writer):
            raise TypeError('Report writer must be callable')
        self.path, self._writer = path, writer
        self._condition = threading.Condition()
        self._publication_lock = threading.Lock()
        self._pending = None
        self._generation = self._urgent_generation = self._published_generation = 0
        self._active_urgent = 0
        self._closed, self._error = False, None
        self._last_snapshot, self._flushed = None, False
        self._thread = threading.Thread(target=self._run,
            name='kalshi-report-publisher', daemon=True)
        self._thread.start()

    def _check_error_locked(self):
        if self._error is not None:
            raise ReportPublicationError('Report publication failed') from self._error

    def _require_open_locked(self):
        self._check_error_locked()
        if self._closed:
            raise ReportPublicationError('Report publisher is closed')

    def _remember_failure(self, error):
        with self._condition:
            if self._error is None:
                self._error = error
            self._pending = None
            self._condition.notify_all()

    @staticmethod
    def _detach(report):
        try:
            return report if isinstance(report, PreparedReport) else deepcopy(report)
        except Exception as error:
            raise ReportPublicationError('Report snapshot could not be detached') from error

    def check_health(self):
        with self._condition:
            self._check_error_locked()

    def submit(self, report):
        """Detach and enqueue the latest report without waiting for disk I/O."""
        snapshot = self._detach(report)
        with self._condition:
            self._require_open_locked()
            self._generation += 1
            self._pending = (self._generation, snapshot)
            self._last_snapshot = snapshot
            self._condition.notify_all()

    def publish_urgent(self, report):
        """Publish synchronously after any older in-flight routine write."""
        snapshot = self._detach(report)
        if isinstance(snapshot, PreparedReport):
            snapshot = snapshot.with_urgent()
        with self._condition:
            self._require_open_locked()
            self._generation += 1
            generation = self._generation
            self._urgent_generation = generation
            self._last_snapshot = snapshot
            self._pending = None
            self._active_urgent += 1
            self._condition.notify_all()
        try:
            self._publish(generation, snapshot)
        finally:
            with self._condition:
                self._active_urgent -= 1
                self._condition.notify_all()

    def _publish(self, generation, snapshot):
        # Recheck generations AFTER obtaining the publication lock. A routine
        # already dequeued before an urgent update must not overwrite it later.
        with self._publication_lock:
            with self._condition:
                self._check_error_locked()
                if generation < self._urgent_generation or generation <= self._published_generation:
                    return
            try:
                value = (snapshot.compose() if isinstance(snapshot, PreparedReport)
                         and not isinstance(self._writer, PairedReportWriter) else snapshot)
                self._writer(self.path, value)
            except Exception as error:
                self._remember_failure(error)
                raise ReportPublicationError('Report publication failed') from error
            with self._condition:
                self._published_generation = generation
                self._condition.notify_all()

    def _run(self):
        try:
            while True:
                with self._condition:
                    self._condition.wait_for(lambda: self._pending is not None
                        or self._closed or self._error is not None)
                    if self._error is not None or self._pending is None:
                        return
                    generation, snapshot = self._pending
                    self._pending = None
                try:
                    self._publish(generation, snapshot)
                except ReportPublicationError:
                    return
        except BaseException as error:
            self._remember_failure(error)

    def close(self):
        if threading.current_thread() is self._thread:
            raise ReportPublicationError('The report writer cannot close its own publisher')
        with self._condition:
            self._closed = True
            self._condition.notify_all()
        self._thread.join()
        with self._condition:
            self._condition.wait_for(lambda: self._active_urgent == 0)
            self._check_error_locked()
        if isinstance(self._writer, PairedReportWriter) and isinstance(self._last_snapshot, PreparedReport) and not self._flushed:
            with self._publication_lock:
                try:
                    self._writer.flush(self.path, self._last_snapshot)
                    self._flushed = True
                except Exception as error:
                    self._remember_failure(error)
                    raise ReportPublicationError('Report publication failed') from error

    def __enter__(self):
        with self._condition:
            self._require_open_locked()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except ReportPublicationError:
            if exc_type is None:
                raise
        return False
