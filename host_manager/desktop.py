# SPDX-License-Identifier: GPL-3.0-or-later

"""Manage the normal RemoteQGIS plugin within QGIS's plugin lifecycle."""

from datetime import datetime
from functools import partial
import json
import os
from pathlib import Path
import sys
import traceback

from qgis.PyQt.QtCore import QCoreApplication, QFileSystemWatcher, QObject, QSettings, Qt, QTimer, pyqtSlot
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
        self.host_started = False
        self.controls_ready = False
        self.start_wait = ""

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

        try:
            self.launch_config = config, token, health
            self.setup()
        except Exception as exc:
            traceback.print_exc()
            self.report_failure(f"QGIS 托管插件初始化失败（{type(exc).__name__}）。详见 host.log。")

    def setup(self):
        self.watcher = QFileSystemWatcher(self)
        if not self.watcher.addPath(str(self.stop_file.parent)):
            raise OSError("无法监听托管 QGIS 的关闭请求目录。")
        self.watcher.directoryChanged.connect(self.stop_requested, Qt.QueuedConnection)
        self.retry = self.timer(200, self.stop_requested)
        self.confirm = self.timer(1000, self.confirm_close)
        self.test_prompt = self.timer(100, self.cancel_prompt)
        self.startup = self.timer(200, self.start_host)
        self.exit_failure = self.timer(200, self.abort_startup)
        QCoreApplication.instance().aboutToQuit.connect(self.quitting_qgis)
        self.iface.initializationCompleted.connect(self.startup.start)
        self.controls_ready = True
        self.stop_file.with_suffix(".active").touch()
        self.note(f"RemoteQGIS managed plugin initialized; QGIS settings={QSettings().fileName()}")
        # Also handle a native profile which loads this plugin after the signal.
        self.startup.start()

    def make_panel(self, config, token, health):
        additional_roots = tuple(Path(root) for root in config.get("additional_directories", ()))
        self.plugin = RemoteWorkspacePlugin(
            self.iface, health_token=health, additional_roots=additional_roots, workspace_factory=partial(
                ManagedWorkspace, selection_file=Path(config["workspace"]), project=config["project"],
                additional_roots=additional_roots,
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
        return panel

    @staticmethod
    def note(message, file=None):
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        print(f"{stamp} pid={os.getpid()} {message}", file=file, flush=True)

    def timer(self, interval, callback):
        timer = QTimer(self, singleShot=True, interval=interval)
        timer.timeout.connect(callback)
        return timer

    @pyqtSlot()
    def start_host(self):
        if self.quitting or self.host_started:
            return
        modal = QApplication.activeModalWidget()
        wait = ""
        if modal is not None and modal.isVisible():
            wait = f"QGIS modal dialog ({modal.metaObject().className()})"
        elif not self.iface.mainWindow().isVisible():
            wait = "QGIS main window"
        if wait:
            if wait != self.start_wait:
                self.start_wait = wait
                self.note(f"RemoteQGIS waiting for {wait}")
            self.startup.start()
            return
        if self.stop_file.is_file():
            self.stop_requested()
            if not self.quitting and not self.closing:
                self.startup.start()
            return
        self.host_started = True
        parameters, self.launch_config = self.launch_config, None
        self.note("RemoteQGIS beginning GIS listener setup")
        try:
            self.make_panel(*parameters)._start_host()
        except Exception as exc:
            traceback.print_exc()
            self.report_failure(f"QGIS 无法启动托管 GIS 服务（{type(exc).__name__}）。详见 host.log。")

    @pyqtSlot(str, str)
    def host_status(self, state, detail):
        if self.quitting:
            return
        if state == "running":
            self.stop_file.with_suffix(".startup-error").unlink(missing_ok=True)
            self.stop_file.with_suffix(".ready").touch()
            self.ready = True
            self.note("RemoteQGIS Desktop Host ready")
        elif state == "error" or (state == "stopped" and self.ready):
            status = self.plugin._panel._last_service_status
            port_in_use = bool(status and status.port_in_use)
            self.stop_file.with_suffix(".ready").unlink(missing_ok=True)
            self.report_failure(detail, port_in_use)
            if port_in_use and not self.ready:
                self.exit_failure.start()

    def report_failure(self, detail, port_in_use=False):
        self.note(detail, sys.stderr)
        self.stop_file.with_suffix(".startup-error").write_text(json.dumps({
            "detail": detail, "port_in_use": port_in_use,
        }, ensure_ascii=False), encoding="utf-8")

    def busy(self):
        if self.plugin is None or self.plugin._panel is None:
            return False
        dispatcher = self.plugin._panel._dispatcher
        return dispatcher is not None and (dispatcher.busy or any(
            job["state"] not in TERMINAL for job in dispatcher.workspace.processing.jobs.values()
        ))

    @pyqtSlot()
    def abort_startup(self):
        if self.quitting or self.plugin is None or self.ready:
            return
        modal = QApplication.activeModalWidget()
        project = QgsProject.instance()
        if self.busy() or (modal is not None and modal.isVisible()) or project.isDirty() or project.count():
            return
        self.unload()
        QCoreApplication.exit(PORT_IN_USE_EXIT_CODE)

    @pyqtSlot()
    def stop_requested(self):
        if (self.quitting or self.closing or not self.controls_ready or self.retry.isActive()
                or not self.stop_file.is_file()):
            return
        modal = QApplication.activeModalWidget()
        if self.busy() or (modal is not None and modal.isVisible()):
            self.retry.start(200)
            return
        self.stop_file.unlink(missing_ok=True)
        self.closing = True
        self.note("RemoteQGIS Desktop close requested")
        project = QgsProject.instance()
        if not project.isDirty() and not QgsApplication.taskManager().countActiveTasks() and not any(
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
        self.stop_file.with_suffix(".ready").unlink(missing_ok=True)
        directories = self.watcher.directories()
        if directories:
            self.watcher.removePaths(directories)

    def unload(self):
        if self.controls_ready:
            self.quitting_qgis()
        if self.plugin is not None:
            self.plugin.unload()
            self.plugin = None
