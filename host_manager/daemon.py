# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import suppress
from datetime import datetime
import hmac
import json
from ipaddress import ip_address
import os
from pathlib import Path
import signal
import socket
import sys
import time
from urllib.error import URLError
from urllib.request import Request, ProxyHandler, build_opener

from qgis.PyQt.QtCore import QLockFile, QSettings, QTimer
from qgis.core import Qgis

from remote_workspace.common.protocol import HostInfo, INFO_PATH, decode_host_info, new_token
from remote_workspace.common.workspace import WorkspaceError
from remote_workspace.host.config import HostConfig
from remote_workspace.host.dependencies import load_tornado
from remote_workspace.host.dispatch import Dispatcher
from remote_workspace.host.service import HostService, PORT_IN_USE_EXIT_CODE, port_in_use_message

from .process import SOURCE, close_desktop_window, spawn

HTTP = build_opener(ProxyHandler({}))
START_TIMEOUT = 90
STOP_TIMEOUT = 12


def desktop_runtime(home):
    if sys.platform == "win32":
        runtime = Path(os.environ.get("LOCALAPPDATA", home.parent)) / "RemoteQGIS" / "managed"
    else:
        runtime = home / "desktop"
    profile = runtime / "profiles" / "remoteqgis-host"
    directory = profile / "python" / "plugins" / "remoteqgis_managed"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    (directory / "__init__.py").write_bytes((SOURCE / "host_manager/desktop_bootstrap.py").read_bytes())
    (directory / "metadata.txt").write_text(
        "[general]\nname=RemoteQGIS Managed Host\nversion=1.0\nqgisMinimumVersion=3.40\n"
        "description=Starts RemoteQGIS in the managed QGIS profile.\n",
        encoding="utf-8",
    )
    path = profile / "qgis.org" / "QGIS3.ini"
    path.parent.mkdir(parents=True, exist_ok=True)
    settings = QSettings(str(path), QSettings.IniFormat)
    settings.setValue("PythonPlugins/remoteqgis_managed", True)
    # The repaired managed plugin is tried on an explicit launch even if a
    # previous version left QGIS's crash-watchdog entry behind.
    settings.remove("PythonPlugins/watchDogTimestamp/remoteqgis_managed")
    settings.setValue("PythonPlugins/processing", True)
    settings.setValue("fonts/downloadMissingFonts", False)
    settings.sync()
    if settings.status() != QSettings.NoError:
        raise OSError("无法写入托管 QGIS 的用户配置。")
    return runtime


def origin(address, port):
    return f"http://[{address}]:{port}" if ip_address(address).version == 6 else f"http://{address}:{port}"


class Manager:
    def __init__(self, app, home, settings, token, local_control):
        self.app, self.home, self.settings, self.token = app, home, settings, token
        self.local_control = local_control
        self.config = settings.validate(token)
        self.additional_roots = settings.additional_roots(self.config.shared_directory)
        self.worker = self.stop_file = None
        self.worker_state, self.detail = "stopped", ""
        self.wanted = self.closing = False
        self.port_in_use = False
        self.health_token = ""
        self.failure_count = 0
        self.retry_at = self.deadline = self.next_health = 0
        self.started_at = 0
        self.stop_deadline = 0
        self.start_failed = self.was_ready = False
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
                if self.worker is not None:
                    return self.status()
                self.failure_count = 0
                self.retry_at = 0
                self.port_in_use = False
                self.start_failed = False
            self.wanted = True
        elif operation in ("manager_stop", "manager_shutdown"):
            control = data.get("control")
            if not isinstance(control, str) or not control.isascii() or not hmac.compare_digest(control, self.local_control):
                raise WorkspaceError("需要工作站本地控制凭据。", 403)
            self.wanted = False
            self.port_in_use = False
            if self.stop_deadline < 0:
                self.stop_deadline = 0
            if operation == "manager_shutdown":
                self.closing = True
        elif operation != "manager_status":
            raise WorkspaceError("管理服务不支持此操作。", 404)
        return self.status()

    def status(self):
        return {"manager": 1, "state": self.worker_state, "detail": self.detail, "port": self.config.port,
                "requested": self.wanted and not self.start_failed,
                "port_in_use": self.port_in_use, "start_failed": self.start_failed,
                "desktop_active": self.worker is not None}

    def launch_worker(self):
        executable = os.environ.get("REMOTEQGIS_DESKTOP", "")
        if not executable or not Path(executable).is_file() or not interactive_desktop():
            raise OSError("工作站当前没有可用的 QGIS Desktop 或用户桌面会话。")
        self.health_token = new_token()
        self.stop_file = self.home / f"desktop-{self.health_token[:16]}.stop"
        env = {key: value for key, value in os.environ.items() if key not in
               {"PYTHONHOME", "PYTHONPATH", "QT_QPA_PLATFORM", "QGIS_CUSTOM_CONFIG_PATH", "PYQGIS_STARTUP"}}
        env.update(REMOTEQGIS_HOST_SOURCE=str(SOURCE), REMOTEQGIS_HOST_TOKEN=self.token,
                   REMOTEQGIS_HOST_HEALTH=self.health_token, REMOTEQGIS_HOST_CONFIG=json.dumps({
                       "address": self.config.address,
                       "port": self.config.port,
                       "directory": str(self.config.shared_directory),
                       "additional_directories": [str(root) for root in self.additional_roots],
                       "project": self.settings.project,
                       "stop": str(self.stop_file),
                       "workspace": str(self.home / "workspace.json"),
                   }))
        runtime = desktop_runtime(self.home)
        env["QGIS_CUSTOM_CONFIG_PATH"] = str(runtime)
        env["PYQGIS_STARTUP"] = str(SOURCE / "host_manager/desktop_startup.py")
        command = [executable, "--nologo", "--noversioncheck", "--profile", "remoteqgis-host"]
        launch = self.stop_file.with_suffix(".launch")
        launch.touch()
        self.started_at = time.monotonic()
        try:
            self.worker = spawn(command, self.home / "host.log", env=env)
        except OSError:
            launch.unlink(missing_ok=True)
            raise
        with suppress(OSError), (self.home / "host.log").open("a", encoding="utf-8") as log:
            stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            print(f"{stamp} pid={self.worker.pid} RemoteQGIS launched QGIS Desktop", file=log)
        self.worker_state, self.detail = "starting", "正在启动 QGIS Desktop。"
        self.deadline = self.started_at + START_TIMEOUT
        self.next_health = 0
        self.was_ready = self.start_failed = False

    def stop_worker(self):
        if self.worker is not None and not self.stop_deadline:
            self.stop_file.touch()
            self.worker_state = "stopping"
            self.stop_deadline = time.monotonic() + STOP_TIMEOUT
            self.detail = "正在关闭远端 QGIS。若有未保存的改动，请在工作站完成保存。"

    def startup_error(self):
        with suppress(OSError, ValueError):
            document = json.loads(self.stop_file.with_suffix(".startup-error").read_text(encoding="utf-8"))
            if isinstance(document, dict):
                return document
        return {}

    def healthy(self):
        request = Request(origin(self.config.address, self.config.port) + INFO_PATH,
                          headers={"Authorization": f"Bearer {self.health_token}"})
        try:
            with HTTP.open(request, timeout=0.25) as reply:
                decode_host_info(reply.read(16385))
            return True
        except (OSError, URLError, ValueError):
            return False

    def tick(self):
        code = self.worker.poll() if self.worker is not None else None
        if code is not None:
            startup_error = self.startup_error()
            for suffix in (".launch", ".python", ".active", ".ready", ".startup-error", ".canceled", ".stop"):
                self.stop_file.with_suffix(suffix).unlink(missing_ok=True)
            self.worker = self.stop_file = None
            self.stop_deadline = 0
            if self.wanted and (code == PORT_IN_USE_EXIT_CODE or startup_error.get("port_in_use")):
                self.wanted = False
                self.port_in_use = True
                self.fail_start(port_in_use_message(self.config))
            elif self.wanted and code != 0 and self.was_ready and not startup_error:
                self.failure_count += 1
                self.retry_at = time.monotonic() + min(30, 2 ** min(self.failure_count, 5))
                self.worker_state = "error"
                self.detail = f"QGIS 已退出（{code}），即将重试。详见 host.log。"
            elif (self.wanted and sys.platform == "win32" and code & 0xFFFFFFFF == 0xC0000005
                  and not self.failure_count and not startup_error):
                self.failure_count = 1
                self.retry_at = time.monotonic() + 1
                self.worker_state, self.detail = "starting", "QGIS 在初始化时发生访问违例，正在重试一次。"
                print(self.detail, file=sys.stderr, flush=True)
            elif self.wanted and (startup_error or not self.was_ready):
                self.wanted = False
                self.fail_start(startup_error.get("detail") or f"QGIS 在就绪前已退出（{code}）。详见 host.log。")
            else:
                self.wanted = False
                self.start_failed = False
                self.worker_state, self.detail = "stopped", ""
        if self.stop_deadline:
            canceled = self.stop_file.with_suffix(".canceled")
            if canceled.exists():
                canceled.unlink()
                self.wanted = True
                self.closing = False
                self.stop_deadline = 0
                self.worker_state = "error" if self.start_failed else ("running" if self.was_ready else "starting")
                self.detail = "远端 QGIS 取消了关闭。"
            elif 0 < self.stop_deadline <= time.monotonic():
                self.stop_deadline = -1
                self.worker_state = "error"
                if not self.stop_file.with_suffix(".active").is_file():
                    close_desktop_window(self.worker.pid, self.home / "host.log")
                self.detail = "远端 QGIS 尚未确认关闭。请处理工作站上的提示，或在 QGIS 中选择退出。"
        if self.closing or self.service.status.state == "error":
            self.wanted = False
            self.stop_worker()
            if self.worker is None:
                self.dispatcher.active = False
                self.service.stop()
                if self.service.status.state in ("stopped", "error"):
                    if self.service.status.state == "error":
                        print(self.service.status.detail, file=sys.stderr, flush=True)
                    self.app.exit(int(self.service.status.state == "error"))
        elif not self.wanted:
            self.stop_worker()
            if self.worker is None and not self.start_failed:
                self.worker_state, self.detail = "stopped", ""
        elif self.worker is None and time.monotonic() >= self.retry_at:
            try:
                self.launch_worker()
            except OSError as exc:
                self.wanted = False
                self.fail_start(f"无法启动 Host（{exc}）。")
        elif self.worker is not None and not self.stop_deadline:
            self.check_desktop()

    def fail_start(self, detail):
        if not self.start_failed or self.detail != detail:
            print(detail, file=sys.stderr, flush=True)
        self.start_failed = True
        self.worker_state, self.detail = "error", detail

    def check_desktop(self):
        error = self.startup_error()
        if error:
            self.port_in_use = bool(error.get("port_in_use"))
            self.fail_start(error.get("detail") or "QGIS 托管服务初始化失败。详见 host.log。")
            return
        now = time.monotonic()
        # A listener confirmation from the QGIS process also works when Windows
        # cannot connect to its own VPN address.
        ready = self.stop_file.with_suffix(".ready").is_file()
        if not ready and now >= self.next_health:
            self.next_health = now + 2
            ready = self.healthy()
        if ready:
            self.failure_count = 0
            self.start_failed = False
            self.was_ready = True
            if self.worker_state != "running":
                self.worker_state, self.detail = "running", "QGIS Desktop 已就绪。"
            self.next_health = now + 4
            return
        if self.stop_file.with_suffix(".active").is_file():
            detail = "QGIS 托管插件已加载，GIS 服务未就绪"
        elif self.stop_file.with_suffix(".python").is_file():
            detail = "QGIS 已初始化 Python，托管插件尚未加载"
        else:
            detail = "正在等待 QGIS 初始化 Python，托管插件尚未加载"
        if now >= self.deadline or self.was_ready:
            self.fail_start(f"{detail}。请检查工作站的 QGIS 窗口和 host.log。")
        elif now - self.started_at >= 10:
            self.detail = f"{detail}（已等待 {int(now - self.started_at)} 秒）。"
        elif self.stop_file.with_suffix(".active").is_file():
            self.detail = "QGIS 托管插件已加载，正在启动 GIS 服务。"


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
