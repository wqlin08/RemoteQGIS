# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from collections import OrderedDict
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
import json
from typing import Set

from tornado.web import Application, HTTPError, RequestHandler
from tornado.websocket import WebSocketClosedError, WebSocketHandler

from ..common.protocol import EVENTS_PATH, INFO_PATH, MAX_MESSAGE_BYTES, authorized
from ..common.workspace import API, MAX_DOCUMENT, TILES, WorkspaceError
from .rasters import MAX_ZOOM, categories, sample
from .raster_source import MEMBER, SOURCE, raster_manifest, read_range


class Authenticated:
    def initialize(self, api: "HostAPI"):
        self.api = api

    def prepare(self):
        if "Origin" in self.request.headers:
            raise HTTPError(403)
        header = self.request.headers.get("Authorization", "")
        self.viewer = bool(self.api.viewer_token and authorized(header, self.api.viewer_token))
        if not self.viewer and not authorized(header, self.api.token):
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
        if self.viewer and operation not in {"state", "vector", "raster_source", "pixel", "view_state"}:
            raise HTTPError(403, reason="展示连接仅有读取权限。")
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
        if z > MAX_ZOOM or x >= 1 << z or y >= 1 << z:
            raise HTTPError(404, reason="影像瓦片不存在。")
        if not spec or version != spec["version"]:
            raise HTTPError(410, reason="工作区或瓦片版本已更新。")
        try:
            self._job = asyncio.create_task(self.api.render_tile(identifier, version, z, x, y))
            image = await self._job
        except asyncio.CancelledError:
            return
        except WorkspaceError as exc:
            raise HTTPError(exc.status, reason=str(exc))
        except (RuntimeError, OSError):
            raise HTTPError(422, reason="影像瓦片读取失败。")
        self.set_header("Content-Type", "image/png")
        self.set_header("Cache-Control", "private, max-age=31536000")
        self.finish(image)


class RasterSourceHandler(Authenticated, RequestHandler):
    async def head(self, identifier, version, member):
        await self.respond(identifier, version, member, True)

    async def get(self, identifier, version, member):
        await self.respond(identifier, version, member, False)

    async def respond(self, identifier, version, member, head):
        spec = self.api.state[2].get(identifier)
        if not spec or spec["source_version"] != version:
            raise HTTPError(404)
        try:
            status, headers, data = await self.api.tile_job(
                read_range, spec, member, self.request.headers.get("Range", ""), head
            )
        except WorkspaceError as exc:
            raise HTTPError(exc.status, reason=str(exc))
        self.set_status(status)
        for name, value in headers.items():
            self.set_header(name, value)
        self.finish(data)


class HostAPI:
    def __init__(self, token: str, state: tuple, dispatcher, *, viewer_token=""):
        self.token = token
        self.viewer_token = viewer_token
        self.state = state
        self.dispatcher = dispatcher
        self._raster_workers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="RemoteQGIS raster")
        self._tile_slots = asyncio.Semaphore(4)
        self._tile_cache = OrderedDict()
        self._tile_tasks = {}
        self.clients: Set[EventsHandler] = set()
        self._sending = set()
        self.application = Application(
            [
                (INFO_PATH, InfoHandler, {"api": self}),
                (EVENTS_PATH, EventsHandler, {"api": self}),
                (API, WorkspaceHandler, {"api": self}),
                (SOURCE + rf"/([A-Za-z0-9_]+)/([0-9a-f]{{24}})/({MEMBER})", RasterSourceHandler, {"api": self}),
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

    async def render_tile(self, *key):
        if key in self._tile_cache:
            self._tile_cache.move_to_end(key)
            return self._tile_cache[key]
        if key not in self._tile_tasks:
            task = asyncio.create_task(self._render_and_cache(key))
            # A map pan can drop every waiter before an in-flight render finishes.
            task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            self._tile_tasks[key] = task
        return await asyncio.shield(self._tile_tasks[key])

    async def _render_and_cache(self, key):
        try:
            async with self._tile_slots:
                tile = await asyncio.wrap_future(self.dispatcher.submit_tile(*key))
            self._tile_cache[key] = tile
            if len(self._tile_cache) > 512:
                self._tile_cache.popitem(last=False)
            return tile
        finally:
            self._tile_tasks.pop(key, None)

    async def call(self, operation: str, arguments: dict):
        if operation in ("pixel", "raster_source", "raster_categories"):
            key = arguments.get("id")
            spec = self.state[2].get(key) if isinstance(key, str) else None
            if spec is None:
                raise WorkspaceError("影像不存在。", 404)
            if operation == "raster_source":
                return await self.tile_job(raster_manifest, spec["path"], spec["stamp"], spec["crs"])
            if operation == "raster_categories":
                return await self.tile_job(categories, spec, arguments.get("band"))
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
        pending = tuple(self._tile_tasks.values())
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self._raster_workers.shutdown(wait=False, cancel_futures=True)
        for client in tuple(self.clients):
            client.close(1001, "Host stopped")
        deadline = asyncio.get_running_loop().time() + 1
        while self.clients and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        for client in tuple(self.clients):
            client.ws_connection.stream.close()
