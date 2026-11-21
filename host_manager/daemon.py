# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import suppress
import hmac
from ipaddress import ip_address
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, ProxyHandler, build_opener

from qgis.PyQt.QtCore import QLockFile, QTimer
from qgis.core import Qgis

from remote_workspace.common.protocol import HostInfo, INFO_PATH, decode_host_info, new_token
from remote_workspace.common.workspace import WorkspaceError
from remote_workspace.host.config import HostConfig
from remote_workspace.host.dependencies import load_tornado
from remote_workspace.host.dispatch import Dispatcher
from remote_workspace.host.service import HostService

from .process import SOURCE, spawn, python_env

HTTP = build_opener(ProxyHandler({}))


def origin(address, port):
    return f"http://[{address}]:{port}" if ip_address(address).version == 6 else f"http://{address}:{port}"


class Manager:
    def __init__(self, app, home, settings, token, local_control):
        self.app, self.home, self.settings, self.token = app, home, settings, token
        self.local_control = local_control
        self.config = settings.validate(token)
        self.worker = self.preview = None
        self.worker_state, self.detail = "stopped", ""
        self.wanted = self.closing = False
        self.preview_token = ""
        self.failure_count = 0
        self.retry_at = self.deadline = self.next_health = 0
        self.dispatcher = Dispatcher(self)
        load_tornado()
        config = HostConfig(self.config.address, settings.manager_port, self.config.shared_directory, token)
        info = HostInfo(Qgis.QGIS_VERSION, socket.gethostname()[:512], sys.platform, "RemoteQGIS Host Manager")
        self.service = HostService(config, info, self.dispatcher)
        self.timer = QTimer(interval=500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def execute(self, operation, data):
        if operation == "manager_start":
            if self.closing:
                raise WorkspaceError("管理服务正在停止。", 409)
            if not self.wanted:
                self.failure_count = 0
                self.retry_at = 0
            self.wanted = True
        elif operation == "manager_preview":
            if self.closing or self.worker_state != "running":
                raise WorkspaceError("请先启动后台 Host。", 409)
            self.show_preview()
        elif operation in ("manager_stop", "manager_shutdown"):
            control = data.get("control")
            if not isinstance(control, str) or not control.isascii() or not hmac.compare_digest(control, self.local_control):
                raise WorkspaceError("需要工作站本地控制凭据。", 403)
            self.wanted = False
            if operation == "manager_shutdown":
                self.closing = True
        elif operation != "manager_status":
            raise WorkspaceError("管理服务不支持此操作。", 404)
        return self.status()

    def status(self):
        return {"manager": 1, "state": self.worker_state, "detail": self.detail, "port": self.config.port,
                "preview": self.preview is not None and self.preview.poll() is None,
                "preview_allowed": self.settings.allow_preview, "requested": self.wanted}

    def launch_worker(self):
        self.preview_token = new_token()
        cmd = [sys.executable, "-m", "headless_host", "--directory", str(self.config.shared_directory),
               "--address", self.config.address, "--port", str(self.config.port),
               "--profile", str(self.home / "qgis-core"), "--project", self.settings.project, "--viewer"]
        self.worker = spawn(cmd, self.home / "host.log", stdin=subprocess.PIPE, env=python_env())
        self.worker.stdin.write(f"{self.token}\n{self.preview_token}\n".encode("ascii"))
        self.worker.stdin.flush()
        self.worker_state, self.detail = "starting", "正在启动无界面 Host。"
        self.deadline = time.monotonic() + 60

    def stop_worker(self):
        if self.worker is not None and self.worker_state != "stopping":
            with suppress(OSError):
                self.worker.stdin.write(b"stop\n")
                self.worker.stdin.close()
            self.worker_state, self.detail = "stopping", "正在结束后台任务并释放端口。"
        if self.preview is not None and self.preview.poll() is None:
            self.preview.terminate()

    def healthy(self):
        request = Request(origin(self.config.address, self.config.port) + INFO_PATH,
                          headers={"Authorization": f"Bearer {self.preview_token}"})
        try:
            with HTTP.open(request, timeout=0.25) as reply:
                decode_host_info(reply.read(16385))
            return True
        except (OSError, URLError, ValueError):
            return False

    def tick(self):
        code = self.worker.poll() if self.worker is not None else None
        if code is not None:
            self.stop_worker()
            self.worker.stdin.close()
            self.worker = None
            if self.wanted:
                self.failure_count += 1
                self.retry_at = time.monotonic() + min(30, 2 ** min(self.failure_count, 5))
                self.worker_state, self.detail = "error", f"Host 已退出（{code}），即将重试。详见 host.log。"
            else:
                self.worker_state, self.detail = "stopped", ""
        if self.closing or self.service.status.state == "error":
            self.wanted = False
            self.stop_worker()
            self.dispatcher.active = False
            self.service.stop()
            if self.worker is None and self.service.status.state in ("stopped", "error"):
                if self.service.status.state == "error":
                    print(self.service.status.detail, file=sys.stderr, flush=True)
                self.app.exit(int(self.service.status.state == "error"))
        elif not self.wanted:
            self.stop_worker()
            if self.worker is None:
                self.worker_state, self.detail = "stopped", ""
        elif self.worker is None and time.monotonic() >= self.retry_at:
            try:
                self.launch_worker()
            except OSError as exc:
                self.retry_at = time.monotonic() + 5
                self.worker_state, self.detail = "error", f"无法启动 Host（{exc}）。"
        elif self.worker_state == "starting":
            if self.healthy():
                self.failure_count = 0
                self.worker_state, self.detail = "running", "后台 Host 已就绪。"
                self.next_health = time.monotonic() + 4
            elif time.monotonic() >= self.deadline:
                self.stop_worker()
        elif self.worker_state in ("running", "error") and time.monotonic() >= self.next_health:
            if self.healthy():
                self.worker_state, self.detail = "running", "后台 Host 已就绪。"
            else:
                self.worker_state, self.detail = "error", "后台进程仍在运行，GIS 端口暂未通过健康检查。"
            self.next_health = time.monotonic() + 4

    def show_preview(self):
        if not self.settings.allow_preview:
            raise WorkspaceError("此工作站未允许远程启动展示窗口。", 403)
        if self.preview is not None and self.preview.poll() is None:
            return
        executable = os.environ.get("REMOTEQGIS_DESKTOP", "")
        if not executable or not Path(executable).is_file() or not interactive_desktop():
            raise WorkspaceError("工作站当前没有可用于展示的 QGIS Desktop 会话。", 409)
        env = {key: value for key, value in os.environ.items() if key not in
               {"PYTHONHOME", "PYTHONPATH", "QT_QPA_PLATFORM", "QGIS_CUSTOM_CONFIG_PATH", "PYQGIS_STARTUP"}}
        env.update(REMOTEQGIS_PREVIEW_SOURCE=str(SOURCE), REMOTEQGIS_PREVIEW_TOKEN=self.preview_token,
                   REMOTEQGIS_PREVIEW_ORIGIN=origin(self.config.address, self.config.port))
        command = [executable, "--nologo", "--noversioncheck", "--noplugins",
                   "--profiles-path", str(self.home / "viewer"), "--profile", "remoteqgis-preview",
                   "--code", str(SOURCE / "host_manager/preview.py")]
        try:
            self.preview = spawn(command, self.home / "preview.log", env=env)
        except OSError as exc:
            raise WorkspaceError("无法启动工作站 QGIS Desktop。", 409) from exc


def interactive_desktop():
    if sys.platform == "darwin":
        return Path("/dev/console").stat().st_uid == os.getuid()
    if os.name == "nt":
        import ctypes

        # A service in session 0 must not launch a desktop window.
        session = ctypes.c_uint32()
        kernel = ctypes.windll.kernel32
        return bool(kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) and session.value)
    return False


def run(app, home, settings, token, control, start_worker):
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease = QLockFile(str(home / "manager.lock"))
    lease.setStaleLockTime(0)
    if not lease.tryLock():
        print("RemoteQGIS 管理服务已启动或配置目录不可写。", file=sys.stderr)
        return 1
    manager = Manager(app, home, settings, token, control)
    manager.wanted = start_worker

    def stop(*_):
        manager.closing = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return app.exec()
    finally:
        lease.unlock()
