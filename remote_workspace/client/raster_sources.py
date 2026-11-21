# SPDX-License-Identifier: GPL-3.0-or-later

from http.client import HTTPConnection, HTTPException, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
from secrets import token_hex
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.parse import urlsplit
from uuid import uuid4
from xml.etree import ElementTree

SOURCE = "/api/v3/raster"
ROUTE = r"/[0-9a-f]{32}/[0-9a-f]{32}/([A-Za-z0-9_]+)/([0-9a-f]{24})/(source\.tif(?:\.ovr|\.msk)?)"
RANGE = re.compile(r"bytes=(\d{1,20})-(\d{1,20})")
CONTENT_RANGE = re.compile(r"bytes (\d{1,20})-(\d{1,20})/(\d{1,20})")
CHUNK = 16 * 1024 * 1024


class RasterRelay(ThreadingHTTPServer):
    request_queue_size = 128


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30
    disable_nagle_algorithm = True

    def setup(self):
        super().setup()
        self.connection_to_host = None
        self.host_origin = None

    def finish(self):
        if self.connection_to_host is not None:
            self.connection_to_host.close()
        super().finish()

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.forward()

    def do_GET(self):
        self.forward()

    def forward(self):
        self.response_started = False
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
            if self.command == "GET" and request_range.startswith("bytes=") and "," in request_range:
                self.forward_multiple(endpoint, header, path, request_range[6:])
                return
            if self.command == "GET" and large:
                start, end = map(int, large.groups())
                if start <= end and end - start + 1 > CHUNK:
                    self.forward_large(endpoint, header, path, start, end)
                    return
            self.send_upstream(endpoint, header, path, request_range)
        except (OSError, ValueError, EOFError, HTTPException):
            if not self.response_started:
                self.send_error(502, "Could not read complete raster bytes")
            self.close_connection = True

    def fetch(self, endpoint, header, path, request_range, method=None):
        if endpoint != self.host_origin:
            if self.connection_to_host is not None:
                self.connection_to_host.close()
            target = urlsplit(endpoint)
            connect = HTTPSConnection if target.scheme == "https" else HTTPConnection
            self.connection_to_host = connect(target.hostname, target.port, timeout=12)
            self.host_origin = endpoint
        connection = self.connection_to_host
        method = method or self.command
        headers = {"Authorization": header}
        if request_range:
            headers["Range"] = request_range
        for attempt in range(2):
            try:
                connection.request(method, path, headers=headers)
                reply = connection.getresponse()
                forwarded = {name: value for name in
                             ("Content-Length", "Content-Type", "Accept-Ranges", "Content-Range")
                             if (value := reply.getheader(name)) is not None}
                if reply.status in (500, 502, 503, 504):
                    raise HTTPException(f"Raster upstream HTTP {reply.status}")
                if method == "HEAD":
                    reply.read()
                    return reply.status, forwarded, b""
                length = int(forwarded.get("Content-Length", -1))
                if not 0 <= length <= CHUNK:
                    raise ValueError("Invalid raster response length")
                if request_range and reply.status < 400:
                    request = RANGE.fullmatch(request_range)
                    content = CONTENT_RANGE.fullmatch(forwarded.get("Content-Range", ""))
                    if reply.status != 206 or not request or not content:
                        raise ValueError("Raster server did not return a byte range")
                    first, last, size = map(int, content.groups())
                    if (not first <= last < size or first != int(request[1]) or last != min(int(request[2]), size - 1)
                            or last - first + 1 != length):
                        raise ValueError("Raster server returned a different byte range")
                data = reply.read(length + 1)
                if len(data) != length:
                    raise EOFError("Raster response was cut short")
                return reply.status, forwarded, data
            except (OSError, ValueError, EOFError, HTTPException):
                connection.close()
                if attempt:
                    raise

    def send_upstream(self, endpoint, header, path, request_range):
        status, headers, data = self.fetch(endpoint, header, path, request_range)
        self.response_started = True
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if data:
            self.wfile.write(data)

    def file_size(self, endpoint, header, path):
        status, headers, _ = self.fetch(endpoint, header, path, "", "HEAD")
        if status != 200:
            self.send_error(status)
            return None
        return int(headers["Content-Length"])

    def forward_multiple(self, endpoint, header, path, ranges):
        matches = [RANGE.fullmatch("bytes=" + part.strip()) for part in ranges.split(",")]
        if len(matches) > 64 or not all(matches):
            self.send_error(416)
            return
        size = self.file_size(endpoint, header, path)
        if size is None:
            return
        spans = [(int(m[1]), min(size - 1, int(m[2]))) for m in matches]
        lengths = [end - start + 1 for start, end in spans]
        if min(lengths) <= 0 or sum(lengths) > 256 * 1024 * 1024:
            self.send_error(416)
            return
        boundary = "remoteqgis-" + token_hex(8)
        parts = [f"--{boundary}\r\nContent-Type: application/octet-stream\r\n"
                 f"Content-Range: bytes {start}-{end}/{size}\r\n\r\n".encode("ascii")
                 for start, end in spans]
        closing = f"--{boundary}--\r\n".encode("ascii")
        self.response_started = True
        self.send_response(206)
        self.send_header("Content-Type", f"multipart/byteranges; boundary={boundary}")
        self.send_header("Content-Length", str(sum(lengths) + sum(len(part) + 2 for part in parts) + len(closing)))
        self.end_headers()
        for part, (start, end) in zip(parts, spans):
            self.wfile.write(part)
            self.copy_span(endpoint, header, path, start, end)
            self.wfile.write(b"\r\n")
        self.wfile.write(closing)

    def forward_large(self, endpoint, header, path, start, end):
        size = self.file_size(endpoint, header, path)
        if size is None:
            return
        end = min(end, size - 1)
        if start > end or end - start + 1 > 256 * 1024 * 1024:
            self.send_error(416)
            return
        self.response_started = True
        self.send_response(206)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        self.copy_span(endpoint, header, path, start, end)

    def copy_span(self, endpoint, header, path, start, end):
        for offset in range(start, end + 1, CHUNK):
            end_offset = min(end, offset + CHUNK - 1)
            status, _, data = self.fetch(endpoint, header, path, f"bytes={offset}-{end_offset}")
            if status != 206 or len(data) != end_offset - offset + 1:
                raise EOFError("Incomplete raster range")
            self.wfile.write(data)


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
        from qgis.core import QgsRasterLayer

        if self.server is None:
            self.server = RasterRelay(("127.0.0.1", 0), RelayHandler)
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
        revision = f"{descriptor['id']}/{descriptor['source_version']}"
        # GDAL caches remote reads by URL. An explicit reload gets a fresh key.
        url = f"http://127.0.0.1:{port}/{self.key}/{uuid4().hex}/{revision}/source.tif"
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
