# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import suppress
from dataclasses import replace
import os
from pathlib import Path

from qgis.PyQt.QtCore import QLockFile, Qt, QTimer, QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtNetwork import QHostAddress, QNetworkInterface
from qgis.PyQt.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSystemTrayIcon, QWidget,
)
from qgis.core import Qgis

from remote_workspace.client.connection import endpoint
from remote_workspace.common.credentials import load_token, save_token
from remote_workspace.common.manager_requests import ManagerRequests
from remote_workspace.common.protocol import new_token

from .autostart import configure, remove_macos_loaded
from .process import manager_command, python_env, spawn
from .settings import ACCOUNT, LOCAL_CONTROL, TITLE, Settings


def spin(value):
    field = QSpinBox()
    field.setRange(1, 65535)
    field.setValue(value)
    return field


class Window(QWidget):
    def __init__(self, home, settings):
        super().__init__()
        self.home, self.settings = home, settings
        self.path = home / "settings.json"
        self.setWindowTitle(TITLE)
        self.setMinimumWidth(570)
        self.requests = ManagerRequests(self)
        self.token, self.control = "", ""
        with suppress(OSError, ValueError):
            self.token = load_token(ACCOUNT)
            self.control = load_token(LOCAL_CONTROL)
        self.pending = False
        self.daemon_process = None
        self.command = ""
        self.current = None
        self.startup_checks = 0
        self.waiting_for_address = False
        self.tray = QSystemTrayIcon(QApplication.windowIcon(), self)
        menu = QMenu(self)
        menu.addAction("打开设置", self.show_settings)
        self.tray_shutdown = menu.addAction("退出管理服务", lambda: self.send("manager_shutdown"))
        menu.addAction("退出管理界面", QApplication.instance().quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.tray_activated)
        self.tray.setToolTip(TITLE)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        form = QFormLayout(self)
        self.address = QLineEdit(settings.address)
        self.port, self.manager_port = spin(settings.port), spin(settings.manager_port)
        self.folder = QLineEdit(settings.shared_directory)
        self.additional = QPlainTextEdit("\n".join(settings.additional_directories))
        self.additional.setPlaceholderText("每行一个目录，可留空")
        self.additional.setFixedHeight(62)
        self.project = QLineEdit(settings.project)
        self.token_field = QLineEdit(self.token or new_token())
        self.token_field.setEchoMode(QLineEdit.Password)
        self.auto_start = QCheckBox("登录后自动启动并在系统托盘驻留")
        self.auto_start.setChecked(settings.auto_start)
        installation = os.environ.get("REMOTEQGIS_DESKTOP", "")
        version = QLabel(f"QGIS {Qgis.QGIS_VERSION}\n{installation}")
        version.setTextFormat(Qt.PlainText)
        version.setWordWrap(True)
        form.addRow("检测到的运行环境", version)
        form.addRow("默认授权目录", self.row(self.folder, self.button("选择…", self.choose_folder)))
        form.addRow("其他授权目录", self.row(self.additional, self.button("添加…", self.add_folder)))
        form.addRow("启动项目", self.row(self.project, self.button("选择…", self.choose_project)))
        form.addRow("本机监听地址", self.address)
        form.addRow("GIS 端口", self.port)
        form.addRow("管理端口", self.manager_port)
        show = QCheckBox("显示")
        show.toggled.connect(lambda shown: self.token_field.setEchoMode(QLineEdit.Normal if shown else QLineEdit.Password))
        copy = self.button("复制", self.copy_token)
        copy.setToolTip("复制当前管理服务使用的令牌")
        self.regenerate = self.button("重新生成", lambda: self.token_field.setText(new_token()))
        form.addRow("访问令牌", self.row(self.token_field, show, copy, self.regenerate))
        form.addRow(self.auto_start)
        self.save = self.button("保存并启动管理服务", self.save_setup)
        self.start = self.button("打开 QGIS", lambda: self.send("manager_start"))
        self.stop = self.button("关闭 QGIS", lambda: self.send("manager_stop"))
        self.shutdown = self.button("退出管理服务", lambda: self.send("manager_shutdown"))
        form.addRow(self.row(self.save, self.start, self.stop))
        form.addRow(self.row(self.shutdown, self.button("打开日志目录", self.open_logs)))
        self.status = QLabel("请配置共享目录并保存。")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        form.addRow(self.status)
        self.timer = QTimer(self, interval=800)
        self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.update_controls()
        self.poll()

    def show_settings(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_settings()

    def closeEvent(self, event):
        if self.tray.isVisible():
            event.ignore()
            self.hide()
        else:
            super().closeEvent(event)

    def copy_token(self):
        token = self.token or self.token_field.text().strip()
        QApplication.clipboard().setText(token)

    @staticmethod
    def button(label, action):
        button = QPushButton(label)
        button.clicked.connect(action)
        return button

    @staticmethod
    def row(*widgets):
        row = QHBoxLayout()
        for widget in widgets:
            row.addWidget(widget)
        return row

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "选择共享目录", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "添加其他授权目录", self.folder.text())
        if folder and folder not in self.additional.toPlainText().splitlines():
            self.additional.appendPlainText(folder)

    def choose_project(self):
        folder = Path(self.folder.text()).expanduser().resolve()
        project, _ = QFileDialog.getOpenFileName(self, "选择启动项目", str(folder), "QGIS 项目 (*.qgs *.qgz)")
        if project:
            try:
                self.project.setText(Path(project).resolve().relative_to(folder).as_posix())
            except ValueError:
                QMessageBox.warning(self, TITLE, "请选择共享目录内的项目。")

    def open_logs(self):
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.home)))

    def save_setup(self):
        try:
            settings = Settings(self.address.text().strip(), self.port.value(), self.manager_port.value(),
                                self.folder.text().strip(), self.project.text().strip(),
                                self.auto_start.isChecked(), tuple(
                                    line.strip() for line in self.additional.toPlainText().splitlines() if line.strip()
                                ))
            token = self.token_field.text().strip()
            settings.validate(token)
            self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
            lease = QLockFile(str(self.home / "manager.lock"))
            lease.setStaleLockTime(0)
            if not lease.tryLock():
                if replace(settings, auto_start=self.settings.auto_start) != self.settings or token != self.token:
                    raise ValueError("请先退出管理服务，再更改工作站配置。")
                self.save_autostart(settings)
                return
            try:
                control = self.control or new_token()
                if control == token:
                    control = new_token()
                save_token(LOCAL_CONTROL, control)
                save_token(ACCOUNT, token)
                self.save_autostart(settings)
                self.token, self.control = token, control
            finally:
                lease.unlock()
            self.start_daemon()
        except (OSError, ValueError, KeyError, RuntimeError) as exc:
            QMessageBox.warning(self, TITLE, str(exc))

    def save_autostart(self, settings):
        if settings.auto_start != self.settings.auto_start or settings.auto_start:
            configure(settings.auto_start, self.home)
        settings.write(self.path)
        if not settings.auto_start and self.settings.auto_start:
            remove_macos_loaded()
        self.settings = settings

    def start_daemon(self):
        self.daemon_process = spawn(manager_command(self.home), self.home / "manager.log",
                                    env=python_env(), detached=True)
        self.startup_checks = 12
        self.status.setText("正在启动管理服务。")
        self.update_controls()

    def start_background(self):
        if self.settings.auto_start:
            self.settings.validate(self.token)
            self.waiting_for_address = True
            self.poll()

    def call(self, operation, callback):
        host = endpoint(self.settings.address, self.settings.manager_port, False)
        data = {"control": self.control} if operation in {"manager_stop", "manager_shutdown"} else {}
        self.requests.call(host, self.token, operation, data, callback)

    def poll(self):
        if self.pending or self.command or not self.token or not self.path.is_file():
            return
        if self.waiting_for_address:
            if QHostAddress(self.settings.address) not in QNetworkInterface.allAddresses():
                self.status.setText("正在等待工作站的监听地址就绪。")
                return
            self.waiting_for_address = False
            try:
                self.start_daemon()
            except OSError as exc:
                self.status.setText(f"无法启动管理服务（{exc}）。")
                return
        self.pending = True
        self.call("manager_status", self.polled)

    def polled(self, value, status):
        self.pending = False
        self.current = value
        if value is not None:
            self.startup_checks = 0
            if value["state"] == "stopped":
                self.status.setText("管理服务已就绪，等待客户端连接。")
            else:
                self.status.setText(value.get("detail", ""))
        elif self.startup_checks:
            self.startup_checks -= 1
        elif status == 401:
            self.status.setText("运行中的管理服务未接受已保存的令牌。请退出旧版管理服务后重新启动。")
        else:
            self.status.setText("管理服务未运行。可重新启动，或在日志目录查看原因。")
        self.update_controls()

    def send(self, operation):
        if self.command:
            return
        self.command = operation

        def done(value, status):
            self.command = ""
            if value is None:
                QMessageBox.warning(self, TITLE, f"管理服务未接受请求（HTTP {status or '超时'}）。")
            self.polled(value, status)

        self.call(operation, done)

    def update_controls(self):
        value = self.current
        locked = value is not None or bool(self.startup_checks)
        self.token_field.setReadOnly(locked)
        self.regenerate.setEnabled(not locked)
        self.start.setEnabled(value is not None and not value["requested"] and value["state"] != "stopping")
        self.stop.setEnabled(value is not None and value["requested"])
        self.shutdown.setEnabled(value is not None and bool(self.control))
        self.tray_shutdown.setEnabled(self.shutdown.isEnabled())
        self.tray.setToolTip(f"{TITLE}\n{self.status.text()}")
