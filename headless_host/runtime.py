# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import suppress
import signal
from threading import Thread
import sys

from qgis.PyQt.QtCore import QTimer
from qgis.core import QgsProject

from remote_workspace.host.dependencies import load_tornado
from remote_workspace.host.dispatch import Dispatcher
from remote_workspace.host.processing import TERMINAL
from remote_workspace.host.project import HostWorkspace
from remote_workspace.host.lease import acquire
from remote_workspace.host.qgis_info import host_info
from remote_workspace.host.service import HostService

from .view import HeadlessView


class HeadlessWorkspace(HostWorkspace):
    def execute(self, operation, data):
        if operation == "view_state":
            return self.current_view()
        return super().execute(operation, data)


def serve(app, config, initial_project="", *, viewer_token=""):
    lock = acquire(config.shared_directory)
    try:
        return _serve(app, config, initial_project, viewer_token)
    finally:
        QgsProject.instance().clear()
        lock.unlock()


def _serve(app, config, initial_project, viewer_token):
    load_tornado()
    project = QgsProject.instance()
    workspace = HeadlessWorkspace(config.shared_directory, view_adapter=HeadlessView(project))
    if initial_project:
        workspace.execute("project", {"action": "open", "path": initial_project, "discard": True})
    dispatcher = Dispatcher(workspace)
    service = HostService(config, host_info(), dispatcher, viewer_token=viewer_token)
    stopping = False
    exit_code = 0

    def stop(*_):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        dispatcher.active = False
        workspace.processing.close()
        service.stop()

    def publish():
        service.publish(host_info(), workspace.refresh(), workspace.rasters)

    def tick():
        nonlocal exit_code
        if service.status.state == "error" and not stopping:
            print(service.status.detail, file=sys.stderr, flush=True)
            exit_code = 1
            stop()
        if stopping:
            if (service.status.state in ("stopped", "error")
                    and all(job["state"] in TERMINAL for job in workspace.processing.jobs.values())):
                app.exit(exit_code)
        else:
            publish()

    from qgis.PyQt.QtCore import QObject, pyqtSignal

    class Shutdown(QObject):
        requested = pyqtSignal()

    shutdown = Shutdown()
    shutdown.requested.connect(stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def wait_for_parent():
        with suppress(OSError):
            sys.stdin.readline()
        shutdown.requested.emit()

    Thread(target=wait_for_parent, daemon=True).start()
    timer = QTimer(interval=200)
    timer.timeout.connect(tick)
    dispatcher.changed.connect(publish)
    timer.start()
    result = app.exec()
    timer.stop()
    return result
