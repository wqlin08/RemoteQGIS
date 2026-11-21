# SPDX-License-Identifier: GPL-3.0-or-later

from functools import partial
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from secrets import token_hex
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from qgis.core import QgsRasterLayer

SOURCE = "/api/v3/raster"
ROUTE = r"/[0-9a-f]{32}/([A-Za-z0-9_]+)/([0-9a-f]{24})/(source\.tif(?:\.ovr|\.msk)?)"
RANGE = re.compile(r"bytes=(\d{1,20})-(\d{1,20})")
CHUNK = 16 * 1024 * 1024


class RelayHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.forward()

    def do_GET(self):
        self.forward()

    def forward(self):
        match = re.fullmatch(ROUTE, self.path)
        relay = self.server.relay
        endpoint, header = relay.session
        if not match or not self.path.startswith(f"/{relay.key}/") or not endpoint:
            self.send_error(404)
            return
        path = f"{SOURCE}/{'/'.join(match.groups())}"
        request_range = self.headers.get("Range", "")
        large = RANGE.fullmatch(request_range)
        try:
            if self.command == "GET" and large:
                start, end = map(int, large.groups())
                if start <= end and end - start + 1 > CHUNK:
                    self.forward_large(endpoint, header, path, start, end)
                    return
            self.send_upstream(endpoint, header, path, request_range)
        except (OSError, ValueError, EOFError):
            self.close_connection = True

    def upstream(self, endpoint, header, path, request_range, method=None):
        target = urlsplit(endpoint)
        connect = HTTPSConnection if target.scheme == "https" else HTTPConnection
        connection = connect(target.hostname, target.port, timeout=12)
        headers = {"Authorization": header}
        if request_range:
            headers["Range"] = request_range
        try:
            connection.request(method or self.command, path, headers=headers)
            return connection, connection.getresponse()
        except Exception:
            connection.close()
            raise

    def send_upstream(self, endpoint, header, path, request_range):
        connection, reply = self.upstream(endpoint, header, path, request_range)
        try:
            self.send_response(reply.status)
            for name in ("Content-Length", "Content-Type", "Accept-Ranges", "Content-Range"):
                value = reply.getheader(name)
                if value is not None:
                    self.send_header(name, value)
            self.end_headers()
            if self.command == "GET":
                self.copy(reply)
        finally:
            connection.close()

    def forward_large(self, endpoint, header, path, start, end):
        connection, info = self.upstream(endpoint, header, path, "", "HEAD")
        try:
            if info.status != 200:
                self.send_error(info.status)
                return
            size = int(info.getheader("Content-Length"))
        finally:
            connection.close()
        end = min(end, size - 1)
        if start > end or end - start + 1 > 256 * 1024 * 1024:
            self.send_error(416)
            return
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        for offset in range(start, end + 1, CHUNK):
            connection, response = self.upstream(endpoint, header, path,
                                                f"bytes={offset}-{min(end, offset + CHUNK - 1)}")
            try:
                if response.status != 206:
                    break
                self.copy(response)
            finally:
                connection.close()

    def copy(self, response):
        while True:
            block = response.read(65536)
            if not block:
                return
            self.wfile.write(block)


class RasterSources:
    def __init__(self, connection):
        self.connection = connection
        self.server = None
        self.directory = None
        self.key = token_hex(16)
        self.session = ("", "")
        connection.status_changed.connect(self.status)

    def status(self, state, _detail):
        if state == "connected":
            self.session = (self.connection.origin, self.connection.authorization.decode("ascii"))
        else:
            self.session = ("", "")

    def layer(self, descriptor, manifest):
        if self.server is None:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), RelayHandler)
            self.server.relay = self
            Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True,
                   name="RemoteQGIS raster loopback").start()
            self.directory = TemporaryDirectory(prefix="remoteqgis-vrt-")
        raw = manifest["vrt"]
        if not isinstance(raw, str) or "<!" in raw:
            raise ValueError("远端栅格清单无效。")
        root = ElementTree.fromstring(raw)
        if root.tag != "VRTDataset" or any(item.tag in ("PixelFunctionCode", "PixelFunctionType")
                                           for item in root.iter()):
            raise ValueError("远端栅格数据类型不受支持。")
        port = self.server.server_port
        url = f"http://127.0.0.1:{port}/{self.key}/{descriptor['id']}/{descriptor['source_version']}/source.tif"
        for item in root.iter("SourceFilename"):
            if item.text != "__REMOTEQGIS_SOURCE__":
                raise ValueError("远端栅格清单包含非预期数据源。")
            item.text = "/vsicurl/" + url
        path = Path(self.directory.name) / f"{uuid4().hex}.vrt"
        ElementTree.ElementTree(root).write(path, encoding="utf-8")
        layer = QgsRasterLayer(str(path), descriptor["name"], "gdal", QgsRasterLayer.LayerOptions(False))
        if not layer.isValid():
            raise ValueError("QGIS 无法创建原始波段栅格代理。")
        return layer

    def close(self):
        self.connection.status_changed.disconnect(self.status)
        self.session = ("", "")
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.directory.cleanup()
