# SPDX-License-Identifier: GPL-3.0-or-later

from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPException, HTTPSConnection
import json
import time

from qgis.PyQt.QtCore import QObject, QTimer, pyqtSlot

from .protocol import validate_token
from .workspace import API

TIMEOUT = 3.5
MAX_REPLY = 16 * 1024


def request_manager(scheme, host, port, token, payload):
    connection_class = HTTPSConnection if scheme == "https" else HTTPConnection
    connection = connection_class(host, port, timeout=TIMEOUT)
    try:
        connection.request("POST", API, body=payload, headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        })
        with connection.getresponse() as reply:
            status = reply.status
            if status != 200:
                return None, status
            raw = reply.read(MAX_REPLY + 1)
    except (OSError, HTTPException, ValueError):
        return None, None
    finally:
        connection.close()
    if len(raw) <= MAX_REPLY:
        try:
            result = json.loads(raw)
            if isinstance(result, dict) and result.get("manager") == 1 and result.get("state") in (
                "stopped", "starting", "running", "stopping", "error",
            ):
                return result, status
        except (ValueError, RecursionError):
            pass
    return None, status


class ManagerRequests(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix="RemoteQGIS manager request")
        self.pending = {}
        self.timer = QTimer(self, interval=40)
        self.timer.timeout.connect(self.complete)
        self.closed = False

    def call(self, url, token, operation, data, callback):
        if self.closed:
            return
        payload = json.dumps({"operation": operation, "data": data}).encode()
        future = self.worker.submit(request_manager, url.scheme(), url.host(), url.port(), validate_token(token), payload)
        self.pending[future] = (time.monotonic() + TIMEOUT, callback)
        if not self.timer.isActive():
            self.timer.start()

    @pyqtSlot()
    def complete(self):
        now = time.monotonic()
        for future in tuple(self.pending):
            entry = self.pending.get(future)
            if entry is None:
                continue
            deadline, callback = entry
            if future.done():
                result = future.result()
            elif now >= deadline:
                future.cancel()
                result = None, None
            else:
                continue
            del self.pending[future]
            if not self.pending:
                self.timer.stop()
            callback(*result)

    @pyqtSlot()
    def close(self):
        if self.closed:
            return
        self.closed = True
        self.timer.stop()
        self.pending.clear()
        self.worker.shutdown(wait=False, cancel_futures=True)
