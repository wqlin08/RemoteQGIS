# SPDX-License-Identifier: GPL-3.0-or-later

from collections import deque
from concurrent.futures import Future

from qgis.PyQt.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from ..common.workspace import WorkspaceError


class Dispatcher(QObject):
    changed = pyqtSignal()
    requested = pyqtSignal(object, str, object)

    def __init__(self, workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.active = True
        self._busy = False
        self._queue = deque()
        self.requested.connect(self._run, Qt.QueuedConnection)

    @property
    def busy(self):
        return self._busy

    def submit(self, operation: str, data: dict) -> Future:
        result = Future()
        self.requested.emit(result, operation, data)
        return result

    @pyqtSlot(object, str, object)
    def _run(self, result: Future, operation: str, data: dict):
        self._queue.append((result, operation, data))
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue:
                self._execute(*self._queue.popleft())
        finally:
            self._busy = False

    def _execute(self, result, operation, data):
        if not result.set_running_or_notify_cancel():
            return
        try:
            if not self.active:
                raise WorkspaceError("工作站服务已停止。", 503)
            value = self.workspace.execute(operation, data)
            if operation not in ("state", "files", "vector", "view", "view_state", "processing_catalog", "processing_status", "processing_cancel"):
                self.changed.emit()
            result.set_result(value)
        except Exception as exc:
            result.set_exception(exc)
