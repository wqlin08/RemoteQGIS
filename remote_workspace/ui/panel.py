# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import Qgis, QgsMessageLog
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDockWidget, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from ..client.connection import Connection, endpoint
from ..common.protocol import HostInfo, new_token
from ..host.config import HostConfig
from ..host.dispatch import Dispatcher
from ..host.project import HostWorkspace
from ..host.dependencies import load_tornado
from ..host.qgis_info import environment, host_info, validate_environment
from ..host.service import HostService
from .browser import Browser


def plain_label(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def port_field() -> QSpinBox:
    field = QSpinBox()
    field.setRange(1, 65535)
    field.setValue(8765)
    return field


class WorkspacePanel(QDockWidget):
    def __init__(self, iface):
        super().__init__("RemoteQGIS", iface.mainWindow())
        self.iface = iface
        self._dispatcher = None
        self.setObjectName("RemoteQGISWorkspace")
        self._host = None
        self._host_state = "stopped"
        self._client_state = "idle"
        self._last_info = None
        self._connection = Connection(self)
        self._connection.status_changed.connect(self._client_status)
        self._connection.info_changed.connect(self._show_host_info)
        self._connection.latency_changed.connect(self._show_latency)
        self._host_timer = QTimer(self, interval=200)
        self._host_timer.timeout.connect(self._poll_host)

        widget = QWidget()
        layout = QVBoxLayout(widget)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._host_tab(), "Host · 工作站")
        self.tabs.addTab(self._client_tab(), "Client · 客户端")
        self.browser = Browser(self._connection, iface)
        self.tabs.addTab(self.browser, "远程工作区")
        layout.addWidget(self.tabs)
        layout.addWidget(plain_label(environment()))
        self.setWidget(widget)
        self._update_controls()

    def _host_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.bind_address = QLineEdit("127.0.0.1")
        self.bind_address.setPlaceholderText("本机 IP 地址")
        self.bind_port = port_field()
        self.directory = QLineEdit()
        browse = QPushButton("选择…")
        browse.clicked.connect(self._choose_directory)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.directory)
        folder_row.addWidget(browse)
        self.host_token = QLineEdit(new_token())
        self.host_token.setEchoMode(QLineEdit.Password)
        show_token = QCheckBox("显示")
        show_token.toggled.connect(
            lambda checked: self.host_token.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)
        )
        regenerate = QPushButton("重新生成")
        regenerate.clicked.connect(lambda: self.host_token.setText(new_token()))
        token_row = QHBoxLayout()
        for widget in (self.host_token, show_token, regenerate):
            token_row.addWidget(widget)
        self.start_host = QPushButton("启动服务")
        self.start_host.clicked.connect(self._start_host)
        self.stop_host = QPushButton("停止服务")
        self.stop_host.clicked.connect(self._stop_host)
        buttons = QHBoxLayout()
        buttons.addWidget(self.start_host)
        buttons.addWidget(self.stop_host)
        self.host_status = plain_label("服务尚未启动。")
        layout.addRow("监听地址", self.bind_address)
        layout.addRow("端口", self.bind_port)
        layout.addRow("共享目录", folder_row)
        layout.addRow("访问令牌", token_row)
        layout.addRow(buttons)
        layout.addRow(self.host_status)
        layout.addRow(plain_label("跨网络连接时，请使用工作站的 Tailscale IP 或其他加密网络地址。"))
        self._host_fields = (self.bind_address, self.bind_port, self.directory, browse, self.host_token, regenerate)
        return tab

    def _client_tab(self) -> QWidget:
        tab = QWidget()
        layout = QFormLayout(tab)
        self.remote_host = QLineEdit("127.0.0.1")
        self.remote_port = port_field()
        self.remote_token = QLineEdit()
        self.remote_token.setEchoMode(QLineEdit.Password)
        self.secure = QCheckBox("HTTPS / WSS")
        self.connect_button = QPushButton("连接")
        self.connect_button.clicked.connect(self._connect)
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.clicked.connect(self._connection.stop)
        buttons = QHBoxLayout()
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        self.client_status = plain_label("尚未连接。")
        self.info_labels = [plain_label("—") for _ in range(5)]
        remote = QGroupBox("远端工作站")
        info = QFormLayout(remote)
        for name, label in zip(("主机名", "QGIS 版本", "系统", "当前项目", "往返延迟"), self.info_labels):
            info.addRow(name, label)
        layout.addRow("主机名或 IP", self.remote_host)
        layout.addRow("端口", self.remote_port)
        layout.addRow("访问令牌", self.remote_token)
        layout.addRow(self.secure)
        layout.addRow(buttons)
        layout.addRow(self.client_status)
        layout.addRow(remote)
        self._client_fields = (self.remote_host, self.remote_port, self.remote_token, self.secure)
        return tab

    def _choose_directory(self):
        directory = QFileDialog.getExistingDirectory(self, "选择工作站共享目录", self.directory.text())
        if directory:
            self.directory.setText(directory)

    def _start_host(self):
        if self._host is not None or self._connection.active:
            return
        try:
            validate_environment()
            config = HostConfig.validate(
                self.bind_address.text(), self.bind_port.value(), self.directory.text(), self.host_token.text()
            )
            load_tornado()
            self._last_info = host_info()
            self._workspace = HostWorkspace(config.shared_directory, self.iface.mapCanvas())
            self._dispatcher = Dispatcher(self._workspace, self)
            self._host = HostService(config, self._last_info, self._dispatcher)
        except (ValueError, RuntimeError) as exc:
            self._show_host_status("error", str(exc))
            return
        self._show_host_status("starting", "正在启动服务…")
        self._host_timer.start()

    def _stop_host(self):
        if self._host is not None:
            self._dispatcher.active = False
            self._host.stop()
            self._poll_host()

    def _poll_host(self):
        status = self._host.status
        if status.state in ("stopped", "error"):
            self._host_timer.stop()
            self._host = None
            self._dispatcher.active = False
            self._dispatcher.deleteLater()
            self._dispatcher = None
        if status.state != self._host_state:
            details = {"stopping": "正在停止服务…", "stopped": "服务已停止。"}
            if status.state == "running":
                config = self._host.config
                details["running"] = f"正在监听 {config.address}，端口 {config.port}。"
            self._show_host_status(status.state, status.detail or details.get(status.state, ""))
        if status.state == "running":
            snapshot = host_info()
            self._host.publish(snapshot, self._workspace.refresh())

    def _connect(self):
        if self._connection.active or self._host is not None:
            return
        try:
            validate_environment()
            url = endpoint(self.remote_host.text(), self.remote_port.value(), self.secure.isChecked())
            self._connection.start(url, self.remote_token.text())
        except ImportError:
            self._client_status("error", "当前 QGIS 安装缺少 Qt WebSockets 模块。")
        except (ValueError, RuntimeError) as exc:
            self._client_status("error", str(exc))

    def _show_host_info(self, info: HostInfo):
        values = (info.hostname, info.qgis_version, info.platform, info.project_name)
        for label, value in zip(self.info_labels, values):
            label.setText(value)

    def _show_latency(self, milliseconds: int):
        self.info_labels[-1].setText(f"{milliseconds} ms" if milliseconds >= 0 else "—")

    def _show_host_status(self, state: str, detail: str):
        self._host_state = state
        self.host_status.setText(detail)
        self._log("host", state)
        self._update_controls()

    def _client_status(self, state: str, detail: str):
        self.client_status.setText(detail)
        if state != "connected":
            for label in self.info_labels:
                label.setText("—")
        if state != self._client_state:
            self._client_state = state
            self._log("client", state)
        self._update_controls()

    @staticmethod
    def _log(component: str, state: str):
        level = Qgis.Warning if state in ("error", "retry") else Qgis.Info
        QgsMessageLog.logMessage(f"component={component} state={state}", "RemoteQGIS", level, False)

    def _update_controls(self):
        hosting = self._host is not None
        connecting = self._connection.active
        self.start_host.setEnabled(not hosting and not connecting)
        self.stop_host.setEnabled(hosting and self._host_state != "stopping")
        self.connect_button.setEnabled(not hosting and not connecting)
        self.disconnect_button.setEnabled(connecting)
        for field in self._host_fields:
            field.setEnabled(not hosting)
        for field in self._client_fields:
            field.setEnabled(not connecting)

    def shutdown(self):
        self._host_timer.stop()
        if self._host is not None:
            self._dispatcher.active = False
            self._host.stop()
        self.browser.shutdown()
        self._connection.stop()
        self.host_token.clear()
        self.remote_token.clear()
