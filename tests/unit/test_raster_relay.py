# SPDX-License-Identifier: GPL-3.0-or-later

from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest import TestCase

from remote_workspace.client.raster_sources import RANGE, RasterRelay, RelayHandler

DATA = bytes(range(256)) * 8


class RasterFixture(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True

    def log_message(self, *_):
        pass

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(DATA)))
        self.end_headers()

    def do_GET(self):
        if self.headers.get("Authorization") != "Bearer fixture":
            self.send_error(401)
            return
        start, end = map(int, RANGE.fullmatch(self.headers["Range"]).groups())
        data = DATA[start:end + 1]
        mode = self.server.next_fault
        self.server.next_fault = ""
        self.server.requests.append((self.client_address, start, end))
        self.send_response(206)
        self.send_header("Content-Length", str(len(data)))
        first = start + 1 if mode == "wrong-range" else start
        self.send_header("Content-Range", f"bytes {first}-{end}/{len(DATA)}")
        self.end_headers()
        if mode == "short":
            self.wfile.write(data[:7])
            self.close_connection = True
        else:
            self.wfile.write(data)


class RasterRelayTests(TestCase):
    def add_listener(self, server):
        Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def setUp(self):
        self.host = self.add_listener(ThreadingHTTPServer(("127.0.0.1", 0), RasterFixture))
        self.host.next_fault = ""
        self.host.requests = []
        self.relay = self.add_listener(RasterRelay(("127.0.0.1", 0), RelayHandler))
        key = "1" * 32
        self.relay.relay = SimpleNamespace(key=key, session=(f"http://127.0.0.1:{self.host.server_port}",
                                                            "Bearer fixture"))
        self.path = f"/{key}/{'2' * 32}/raster_a/{'3' * 24}/source.tif"
        self.client = HTTPConnection("127.0.0.1", self.relay.server_port, timeout=3)
        self.addCleanup(self.client.close)

    def get(self, ranges):
        self.client.request("GET", self.path, headers={"Range": f"bytes={ranges}"})
        response = self.client.getresponse()
        return response.status, response.headers, response.read()

    def test_retry_truncated_and_wrong_content_range(self):
        for fault in ("short", "wrong-range"):
            self.host.next_fault = fault
            status, headers, body = self.get("30-90")
            self.assertEqual(status, 206)
            self.assertEqual(headers["Content-Range"], f"bytes 30-90/{len(DATA)}")
            self.assertEqual(body, DATA[30:91])
        self.assertEqual(len(self.host.requests), 4)

    def test_reuse_connection_and_preserve_multipart_bytes(self):
        for range_header, data in (("0-63", DATA[:64]), ("200-204", DATA[200:205])):
            status, _, body = self.get(range_header)
            self.assertEqual((status, body), (206, data))
        status, headers, body = self.get("12-17,255-262")
        self.assertEqual(status, 206)
        self.assertEqual(int(headers["Content-Length"]), len(body))
        self.assertIn(DATA[12:18], body)
        self.assertIn(DATA[255:263], body)
        self.assertEqual(len({peer for peer, _, _ in self.host.requests}), 1)
