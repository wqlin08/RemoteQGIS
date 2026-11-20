# SPDX-License-Identifier: GPL-3.0-or-later

import asyncio
from contextlib import suppress
from typing import Set

from tornado.web import Application, HTTPError, RequestHandler
from tornado.websocket import WebSocketClosedError, WebSocketHandler

from ..common.protocol import EVENTS_PATH, INFO_PATH, MAX_MESSAGE_BYTES, HostInfo, authorized


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
        self.finish(self.api.info.message())


class EventsHandler(Authenticated, WebSocketHandler):
    async def open(self):
        self.set_nodelay(True)
        self.api.clients.add(self)
        with suppress(WebSocketClosedError):
            await self.write_message(self.api.info.message(event=True))

    def on_message(self, message):
        self.close(1008, "Read-only event stream")

    def on_close(self):
        self.api.clients.discard(self)


class HostAPI:
    def __init__(self, token: str, info: HostInfo):
        self.token = token
        self.info = info
        self.clients: Set[EventsHandler] = set()
        self._sending = set()
        self.application = Application(
            [
                (INFO_PATH, InfoHandler, {"api": self}),
                (EVENTS_PATH, EventsHandler, {"api": self}),
            ],
            websocket_ping_interval=15,
            websocket_ping_timeout=10,
            websocket_max_message_size=MAX_MESSAGE_BYTES,
            log_function=lambda _: None,
        )

    def publish(self, info: HostInfo):
        if info != self.info:
            self.info = info
            for client in tuple(self.clients):
                if client in self._sending:
                    continue
                self._sending.add(client)
                asyncio.create_task(self._send_latest(client))

    async def _send_latest(self, client: EventsHandler):
        try:
            while client in self.clients:
                info = self.info
                with suppress(WebSocketClosedError):
                    await asyncio.wait_for(client.write_message(info.message(event=True)), 5)
                if info == self.info:
                    return
        except asyncio.TimeoutError:
            client.close(1013, "Slow receiver")
        finally:
            self._sending.discard(client)

    async def close(self):
        for client in tuple(self.clients):
            client.close(1001, "Host stopped")
        deadline = asyncio.get_running_loop().time() + 1
        while self.clients and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.02)
        for client in tuple(self.clients):
            client.ws_connection.stream.close()
