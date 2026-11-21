# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import errno
import socket
import threading
from typing import Optional

from ..common.protocol import HostInfo
from ..common.workspace import MAX_DOCUMENT
from .config import HostConfig

PORT_IN_USE_EXIT_CODE = 10
WSAEADDRINUSE = 10048


def port_in_use_message(config: HostConfig) -> str:
    address = f"[{config.address}]" if ":" in config.address else config.address
    return f"监听地址 {address}:{config.port} 已被占用。请停止占用该端口的服务，或更换端口。"


@dataclass(frozen=True)
class ServiceStatus:
    state: str
    detail: str = ""
    port_in_use: bool = False


class HostService:
    """Own the network loop. The QGIS thread publishes immutable snapshots."""

    def __init__(self, config: HostConfig, info: HostInfo, dispatcher, *, viewer_token=""):
        self.config = config
        self.dispatcher = dispatcher
        self._viewer_token = viewer_token
        self._info = (info, 0, {})
        self._lock = threading.Lock()
        self._status = ServiceStatus("starting")
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_signal: Optional[asyncio.Event] = None
        self._publish = None
        self._thread = threading.Thread(target=self._run, name="RemoteQGIS Host", daemon=True)
        self._thread.start()

    @property
    def status(self) -> ServiceStatus:
        with self._lock:
            return self._status

    def publish(self, info: HostInfo, revision: int, rasters: dict):
        state = (info, revision, rasters)
        with self._lock:
            if state == self._info:
                return
            self._info = state
            if self._status.state == "running":
                self._loop.call_soon_threadsafe(self._publish, state)

    def stop(self):
        with self._lock:
            if self._status.state in ("starting", "running"):
                self._status = ServiceStatus("stopping")
                if self._stop_signal is not None:
                    self._loop.call_soon_threadsafe(self._stop_signal.set)

    def _run(self):
        try:
            asyncio.run(self._serve())
        except OSError as exc:
            port_in_use = exc.errno in (errno.EADDRINUSE, WSAEADDRINUSE)
            detail = (port_in_use_message(self.config) if port_in_use else
                      f"网络服务启动或停止失败（{exc.strerror or type(exc).__name__}）。")
            with self._lock:
                self._status = ServiceStatus("error", detail, port_in_use)
        except Exception as exc:
            with self._lock:
                self._status = ServiceStatus("error", f"网络服务异常（{type(exc).__name__}）。")
        else:
            with self._lock:
                self._status = ServiceStatus("stopped")

    async def _serve(self):
        from tornado.httpserver import HTTPServer

        from .api import HostAPI

        with self._lock:
            if self._status.state == "stopping":
                return
            api = HostAPI(self.config.token, self._info, self.dispatcher, viewer_token=self._viewer_token)
            self._loop = asyncio.get_running_loop()
            self._stop_signal = asyncio.Event()
            self._publish = api.publish

        server = HTTPServer(
            api.application, max_header_size=8192, max_body_size=MAX_DOCUMENT,
            body_timeout=45, idle_connection_timeout=15, decompress_request=False,
        )
        family = socket.AF_INET6 if ":" in self.config.address else socket.AF_INET
        try:
            # create_server closes its socket even when the bind fails.
            with socket.create_server((self.config.address, self.config.port), family=family, backlog=128) as listener:
                listener.setblocking(False)
                server.add_socket(listener)
                try:
                    with self._lock:
                        if self._status.state != "stopping":
                            api.publish(self._info)
                            self._status = ServiceStatus("running")
                    await self._stop_signal.wait()
                finally:
                    server.stop()
        finally:
            with self._lock:
                if self._status.state == "running":
                    self._status = ServiceStatus("stopping")
            await api.close()
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(server.close_all_connections(), timeout=3)
