# SPDX-License-Identifier: GPL-3.0-or-later

"""Manage the normal RemoteQGIS plugin within QGIS's plugin lifecycle."""

from functools import partial
import json
import os
from pathlib import Path
import sys

from qgis.PyQt.QtCore import QCoreApplication, QFileSystemWatcher, QObject, Qt, QTimer, pyqtSlot
from qgis.PyQt.QtWidgets import QApplication, QMessageBox
from qgis.core import QgsApplication, QgsProject, QgsVectorLayer

from remote_workspace.host.processing import TERMINAL
from remote_workspace.host.service import PORT_IN_USE_EXIT_CODE
from remote_workspace.plugin import RemoteWorkspacePlugin

from .workspace import ManagedWorkspace


class DesktopHost(QObject):
    def __init__(self, iface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.plugin = None
        self.ready = self.closing = self.quitting = False

    def initGui(self):
        config = json.loads(os.environ.pop("REMOTEQGIS_HOST_CONFIG", "null"))
        token = os.environ.pop("REMOTEQGIS_HOST_TOKEN", "")
        health = os.environ.pop("REMOTEQGIS_HOST_HEALTH", "")
        self.cancel_for_test = os.environ.pop("REMOTEQGIS_TEST_CANCEL_CLOSE", "") == "1"
        if not config or not token or not health:
            return
        self.stop_file = Path(config["stop"])
        try:
            self.stop_file.with_suffix(".launch").unlink()
        except FileNotFoundError:
            # A QGIS reload can inherit an old launch's environment.
            return

        self.plugin = RemoteWorkspacePlugin(
            self.iface, health_token=health, workspace_factory=partial(
                ManagedWorkspace, selection_file=Path(config["workspace"]), project=config["project"],
            ),
        )
        self.plugin.initGui()
        self.plugin._show_workspace()
        panel = self.plugin._panel
        panel.bind_address.setText(config["address"])
        panel.bind_port.setValue(config["port"])
        panel.directory.setText(config["directory"])
        panel.host_token.setText(token)
        panel.host_state_changed.connect(self.host_status, Qt.QueuedConnection)

        self.watcher = QFileSystemWatcher([str(self.stop_file.parent)], self)
        self.watcher.directoryChanged.connect(self.stop_requested, Qt.QueuedConnection)
        self.retry = self.timer(200, self.stop_requested)
        self.confirm = self.timer(1000, self.confirm_close)
        self.test_prompt = self.timer(100, self.cancel_prompt)
        self.startup = self.timer(0, self.start_host)
        self.exit_failure = self.timer(200, self.abort_startup)
        QCoreApplication.instance().aboutToQuit.connect(self.quitting_qgis)
        self.startup.start()

    def timer(self, interval, callback):
        timer = QTimer(self, singleShot=True, interval=interval)
        timer.timeout.connect(callback)
        return timer

    @pyqtSlot()
    def start_host(self):
        if self.quitting or self.plugin is None:
            return
        from processing.core.Processing import Processing

        Processing.initialize()
        self.plugin._panel._start_host()
        self.stop_requested()

    @pyqtSlot(str, str)
    def host_status(self, state, detail):
        if self.quitting or self.ready:
            return
        if state == "running":
            self.ready = True
            print("RemoteQGIS Desktop Host ready", flush=True)
        elif state == "error":
            self.error_detail = detail
            self.exit_failure.start()

    def busy(self):
        dispatcher = self.plugin._panel._dispatcher
        return dispatcher is not None and (dispatcher.busy or any(
            job["state"] not in TERMINAL for job in dispatcher.workspace.processing.jobs.values()
        ))

    @pyqtSlot()
    def abort_startup(self):
        if self.quitting or self.plugin is None:
            return
        modal = QApplication.activeModalWidget()
        if self.busy() or (modal is not None and modal.isVisible()):
            self.exit_failure.start()
            return
        status = self.plugin._panel._last_service_status
        port_in_use = bool(status and status.port_in_use)
        detail = self.error_detail
        print(detail, file=sys.stderr, flush=True)
        self.stop_file.with_suffix(".startup-error").write_text(json.dumps({
            "detail": detail, "port_in_use": port_in_use,
        }, ensure_ascii=False), encoding="utf-8")
        self.unload()
        QCoreApplication.exit(PORT_IN_USE_EXIT_CODE if port_in_use else 1)

    @pyqtSlot()
    def stop_requested(self):
        if (self.quitting or self.closing or self.plugin is None or self.retry.isActive()
                or not self.stop_file.is_file()):
            return
        modal = QApplication.activeModalWidget()
        if (self.busy() or QgsApplication.taskManager().countActiveTasks()
                or (modal is not None and modal.isVisible())):
            self.retry.start(200)
            return
        self.stop_file.unlink(missing_ok=True)
        self.closing = True
        print("RemoteQGIS Desktop close requested", flush=True)
        project = QgsProject.instance()
        if not project.isDirty() and not any(
            layer.isTemporary() or (isinstance(layer, QgsVectorLayer) and layer.isEditable())
            for layer in project.mapLayers().values()
        ):
            self.unload()
            QCoreApplication.exit(0)
            return
        if self.cancel_for_test:
            self.test_prompt.start()
        self.iface.actionExit().trigger()
        if not self.quitting:
            self.confirm.start()

    @pyqtSlot()
    def confirm_close(self):
        if self.closing and not self.quitting and self.iface.mainWindow().isVisible():
            self.closing = False
            self.test_prompt.stop()
            self.retry.start(1500)
            self.stop_file.with_suffix(".canceled").touch()

    @pyqtSlot()
    def cancel_prompt(self):
        if self.quitting or not self.closing:
            return
        dialog = QApplication.activeModalWidget()
        if isinstance(dialog, QMessageBox) and dialog.isVisible():
            cancel = dialog.button(QMessageBox.Cancel)
            if cancel is not None:
                self.cancel_for_test = False
                cancel.click()
                return
        self.test_prompt.start()

    @pyqtSlot()
    def quitting_qgis(self):
        if self.quitting:
            return
        self.quitting = True
        for timer in (self.startup, self.retry, self.confirm, self.test_prompt, self.exit_failure):
            timer.stop()
        directories = self.watcher.directories()
        if directories:
            self.watcher.removePaths(directories)

    def unload(self):
        if self.plugin is not None:
            self.quitting_qgis()
            self.plugin.unload()
            self.plugin = None
