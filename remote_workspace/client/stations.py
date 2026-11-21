# SPDX-License-Identifier: GPL-3.0-or-later

import json
import time
from uuid import uuid4

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QWidget,
)

from ..common.credentials import load_token, save_token
from ..common.manager_requests import ManagerRequests
from .connection import endpoint

KEY = "remoteqgis/workstations"


class Stations(QWidget):
    def __init__(self, panel):
        super().__init__(panel)
        self.panel = panel
        self.requests = ManagerRequests(self)
        self.settings = QgsSettings()
        try:
            profiles = json.loads(self.settings.value(KEY, "{}"))
            self.profiles = profiles if isinstance(profiles, dict) else {}
        except (TypeError, ValueError):
            self.profiles = {}
        self.pending = None
        self.active_manager = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.choices = QComboBox()
        layout.addWidget(self.choices, 1)
        self.use = QPushButton("连接工作站")
        self.save = QPushButton("保存当前地址")
        self.remove = QPushButton("删除")
        self.preview = QPushButton("远端展示")
        for button, action in ((self.use, self.connect), (self.save, self.store),
                               (self.remove, self.delete), (self.preview, self.show_preview)):
            layout.addWidget(button)
            button.clicked.connect(action)
        self.choices.currentIndexChanged.connect(self.selection_changed)
        self.reload()

    def reload(self, identifier=None):
        self.choices.clear()
        self.choices.addItem("选择已保存的工作站", None)
        for key, profile in sorted(self.profiles.items(), key=lambda item: item[1]["name"]):
            self.choices.addItem(profile["name"], key)
        if identifier is not None:
            self.choices.setCurrentIndex(self.choices.findData(identifier))
        self.update_buttons()

    def selection_changed(self):
        profile = self.profiles.get(self.choices.currentData())
        if profile is not None:
            self.panel.remote_host.setText(profile["host"])
            self.panel.remote_port.setValue(profile["port"])
            self.panel.secure.setChecked(profile["tls"])
            self.panel.remote_token.clear()
        self.update_buttons()

    def update_buttons(self):
        hosting = self.panel._host is not None
        active = self.panel._connection.active
        editable = not hosting and not active and self.pending is None
        self.choices.setEnabled(editable)
        self.use.setEnabled(editable and self.choices.currentData() is not None)
        self.remove.setEnabled(editable and self.choices.currentData() is not None)
        self.save.setEnabled(editable)
        self.preview.setEnabled(active and self.panel._connection.ready and self.active_manager is not None)

    def store(self):
        identifier = self.choices.currentData()
        old = self.profiles.get(identifier, {})
        dialog = QDialog(self)
        dialog.setWindowTitle("保存工作站")
        form = QFormLayout(dialog)
        name = QLineEdit(old.get("name", self.panel.remote_host.text()))
        mode = QComboBox()
        mode.addItems(["Host Manager", "直接连接原有 Host"])
        mode.setCurrentIndex(1 if old and not old.get("manager_port") else 0)
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(old.get("manager_port") or 8766)
        port.setEnabled(mode.currentIndex() == 0)
        mode.currentIndexChanged.connect(lambda index: port.setEnabled(index == 0))
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow("名称", name)
        form.addRow("连接方式", mode)
        form.addRow("管理端口", port)
        form.addRow(buttons)
        if not dialog.exec():
            return
        try:
            label = name.text().strip()
            if not 1 <= len(label) <= 80:
                raise ValueError("工作站名称须为 1–80 个字符。")
            panel = self.panel
            host, gis_port, tls = panel.remote_host.text().strip(), panel.remote_port.value(), panel.secure.isChecked()
            endpoint(host, gis_port, tls)
            manager_port = port.value() if mode.currentIndex() == 0 else 0
            if manager_port == gis_port:
                raise ValueError("管理端口须与 GIS 端口不同。")
            token = panel.remote_token.text()
            if identifier is not None and not token.strip():
                token = load_token("client-" + identifier)
            identifier = identifier or uuid4().hex
            save_token("client-" + identifier, token)
            self.profiles[identifier] = {"name": label, "host": host, "port": gis_port, "tls": tls,
                                         "manager_port": manager_port}
            self.persist(identifier)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, "RemoteQGIS", str(exc))

    def delete(self):
        identifier = self.choices.currentData()
        if identifier is not None:
            try:
                from ..common.credentials import delete_token

                delete_token("client-" + identifier)
            except OSError as exc:
                QMessageBox.warning(self, "RemoteQGIS", str(exc))
                return
            self.profiles.pop(identifier, None)
            self.persist()

    def persist(self, identifier=None):
        self.settings.setValue(KEY, json.dumps(self.profiles, ensure_ascii=False))
        self.reload(identifier)

    def connect(self):
        identifier = self.choices.currentData()
        profile = self.profiles.get(identifier)
        if self.panel._connection.active or self.pending is not None or not profile:
            return
        try:
            token = load_token("client-" + identifier)
            manager_port, host, tls = profile["manager_port"], profile["host"], profile["tls"]
            manager = endpoint(host, manager_port, tls) if manager_port else None
            self.panel.remote_host.setText(host)
            self.panel.remote_port.setValue(profile["port"])
            self.panel.remote_token.setText(token)
            self.panel.secure.setChecked(tls)
            if manager is None:
                self.panel._connect()
                return
            key = self.pending = object()
            self.panel._client_status("connecting", "正在检查工作站管理服务…")
            deadline = time.monotonic() + 70

            def result(value, status, requested=False):
                if self.pending is not key:
                    return
                if not value:
                    self.pending = None
                    if not requested and status in (None, 404, 405):
                        self.panel._connect()
                    else:
                        self.panel._client_status("error", f"管理服务请求失败（HTTP {status or '超时'}）。")
                    return
                if value["state"] == "running":
                    if value.get("port") != profile["port"]:
                        self.pending = None
                        self.panel._client_status("error", "工作站报告的 GIS 端口和已保存配置不一致。")
                        return
                    self.pending = None
                    self.active_manager = (manager, token) if value.get("preview_allowed") else None
                    self.panel._connect()
                    return
                if time.monotonic() >= deadline:
                    self.pending = None
                    self.panel._client_status("error", value.get("detail") or "后台 Host 启动超时。")
                    return
                if not value.get("requested"):
                    self.requests.call(manager, token, "manager_start", {},
                                       lambda value, status: result(value, status, True))
                    return
                self.panel._client_status("connecting", value.get("detail") or "正在等待后台 Host 就绪…")

                def poll():
                    if self.pending is key:
                        self.requests.call(manager, token, "manager_status", {},
                                           lambda value, status: result(value, status, True))

                QTimer.singleShot(800, poll)

            self.requests.call(manager, token, "manager_status", {}, result)
        except (OSError, ValueError) as exc:
            self.panel._client_status("error", str(exc))

    def cancel(self):
        self.pending = None
        self.active_manager = None

    def show_preview(self):
        if self.active_manager is None:
            return
        url, token = self.active_manager

        def completed(value, status):
            if value:
                self.panel.client_status.setText("工作站已接受只读展示请求。")
            else:
                self.panel.client_status.setText(f"工作站无法启动展示（HTTP {status or '超时'}）。")

        self.requests.call(url, token, "manager_preview", {}, completed)
