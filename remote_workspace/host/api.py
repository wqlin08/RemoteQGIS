# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Set

from tornado.web import Application, HTTPError, RequestHandler
from tornado.websocket import WebSocketClosedError, WebSocketHandler

from ..common.protocol import EVENTS_PATH, INFO_PATH, MAX_MESSAGE_BYTES, authorized
from ..common.workspace import API, MAX_DOCUMENT, TILES, WorkspaceError
from .rasters import MAX_ZOOM, render, sample


class Authenticated:
    def initialize(self, api: "HostAPI"):
        self.api = api

    def prepare(self):
        if "Origin" in self.request.headers:
            raise HTTPError(403)
        if not authorized(self.request.headers.get("Authorization", ""), self.api.token):
            raise HTTPError(401)

    def write_error(self, status_code: int, **kwargs):
        if status_code == 401:
            self.set_header("WWW-Authenticate", 'Bearer realm="RemoteQGIS"')
        self.set_header("Cache-Control", "no-store")
        self.finish({"error": self._reason})


class InfoHandler(Authenticated, RequestHandler):
    def get(self):
        self.set_header("Cache-Control", "no-store")
        self.finish(self.api.state[0].message())


class EventsHandler(Authenticated, WebSocketHandler):
    async def open(self):
        self.set_nodelay(True)
        self.api.clients.add(self)
        with suppress(WebSocketClosedError):
            await self.write_message(self.api.event())

    def on_message(self, message):
        self.close(1008, "Read-only event stream")

    def on_close(self):
        self.api.clients.discard(self)


class WorkspaceHandler(Authenticated, RequestHandler):
    async def post(self):
        try:
            body = json.loads(self.request.body)
            operation, arguments = body["operation"], body["data"]
            if not isinstance(operation, str) or not isinstance(arguments, dict):
                raise ValueError
        except (ValueError, TypeError, KeyError, RecursionError):
            raise HTTPError(400, reason="请求结构无效。")
        try:
            result = await self.api.call(operation, arguments)
            encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except WorkspaceError as exc:
            raise HTTPError(exc.status, reason=str(exc))
        except asyncio.TimeoutError:
            raise HTTPError(504, reason="工作站未及时响应。")
        if len(encoded) > MAX_DOCUMENT:
            raise HTTPError(413, reason="结果超过传输大小限制。")
        self.set_header("Content-Type", "application/json; charset=UTF-8")
        self.set_header("Cache-Control", "no-store")
        self.finish(encoded)


class TileHandler(Authenticated, RequestHandler):
    def on_connection_close(self):
        job = getattr(self, "_job", None)
        if job is not None:
            job.cancel()

    async def get(self, identifier, version, zoom, x, y):
        spec = self.api.state[2].get(identifier)
        z, x, y = int(zoom), int(x), int(y)
        if not spec or version != spec["version"] or z > MAX_ZOOM or x >= 1 << z or y >= 1 << z:
            raise HTTPError(404, reason="影像瓦片或版本不存在。")
        try:
            self._job = asyncio.create_task(self.api.tile_job(
                render, spec["path"], spec["stamp"], spec["bands"], spec["stretch"],
                spec["resample"], spec["nodata"], z, x, y,
            ))
            image = await self._job
        except asyncio.CancelledError:
            return
        except (WorkspaceError, RuntimeError, OSError):
            raise HTTPError(422, reason="影像瓦片读取失败。")
        self.set_header("Content-Type", "image/png")
        self.set_header("Cache-Control", "private, max-age=31536000")
        self.finish(image)


class HostAPI:
    def __init__(self, token: str, state: tuple, dispatcher):
        self.token = token
        self.state = state
        self.dispatcher = dispatcher
        self._raster_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="RemoteQGIS raster")
        self.clients: Set[EventsHandler] = set()
        self._sending = set()
        self.application = Application(
            [
                (INFO_PATH, InfoHandler, {"api": self}),
                (EVENTS_PATH, EventsHandler, {"api": self}),
                (API, WorkspaceHandler, {"api": self}),
                (TILES + r"/([A-Za-z0-9_]+)/([0-9a-f]{24})/(\d{1,2})/(\d{1,8})/(\d{1,8})\.png",
                 TileHandler, {"api": self}),
            ],
            websocket_ping_interval=15,
            websocket_ping_timeout=10,
            websocket_max_message_size=MAX_MESSAGE_BYTES,
            log_function=lambda _: None,
        )

    def event(self):
        return dict(self.state[0].message(event=True), revision=self.state[1])

    async def tile_job(self, function, *args):
        return await asyncio.get_running_loop().run_in_executor(self._raster_workers, function, *args)

    async def call(self, operation: str, arguments: dict):
        if operation == "pixel":
            key = arguments.get("id")
            spec = self.state[2].get(key) if isinstance(key, str) else None
            if spec is None:
                raise WorkspaceError("影像不存在。", 404)
            return await self.tile_job(sample, spec, arguments.get("x"), arguments.get("y"))
        future = self.dispatcher.submit(operation, arguments)
        return await asyncio.wait_for(asyncio.wrap_future(future), 40)

    def publish(self, state: tuple):
        if state != self.state:
            self.state = state
            for client in tuple(self.clients):
                if client in self._sending:
                    continue
                self._sending.add(client)
                asyncio.create_task(self._send_latest(client))

    async def _send_latest(self, client: EventsHandler):
        try:
            while client in self.clients:
                state = self.state
                with suppress(WebSocketClosedError):
                    await asyncio.wait_for(client.write_message(self.event()), 5)
                if state == self.state:
                    return
        except asyncio.TimeoutError:
            client.close(1013, "Slow receiver")
        finally:
            self._sending.discard(client)

    async def close(self):
        self._raster_workers.shutdown(wait=False, cancel_futures=True)
        for client in tuple(self.clients):
            client.close(1001, "Host stopped")
        deadline = asyncio.get_running_loop().time() + 1
        while self.clients and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        for client in tuple(self.clients):
            client.ws_connection.stream.close()
