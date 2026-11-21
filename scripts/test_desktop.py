# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the release ZIP in two isolated QGIS Desktop profiles."""

import argparse
from contextlib import ExitStack
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
from threading import Thread
import zlib
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def fixture_png():
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    pixels = (bytes([0]) + bytes([36, 104, 172]) * 256) * 256
    return (bytes.fromhex("89504e470d0a1a0a") +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class FixtureTiles(BaseHTTPRequestHandler):
    image = fixture_png()

    def log_message(self, *_):
        pass

    def do_GET(self):
        if not self.path.startswith("/remoteqgis-fixture/"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(self.image)))
        self.end_headers()
        self.wfile.write(self.image)


def expect_http(port: int, path: str, expected: int, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
    try:
        connection.request("GET", path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        assert response.status == expected, (path, expected, response.status, body)
        return body
    finally:
        connection.close()


def wait_for(root: Path, marker: str, processes: dict, timeout: float):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if (root / marker).exists():
            return
        for role, process in processes.items():
            if (root / f"{role}-error").exists() or process.poll() is not None:
                raise RuntimeError(f"QGIS {role} exited before producing {marker} (exit {process.poll()}).")
        sleep(0.05)
    raise TimeoutError(f"Timed out waiting for {marker}.")


def stop_process(process: subprocess.Popen):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qgis", type=Path, required=True)
    parser.add_argument("--zip", type=Path, required=True)
    parser.add_argument("--offscreen", action="store_true", help="Use Qt's optional offscreen backend.")
    parser.add_argument("--stage", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()

    with TemporaryDirectory(prefix="remoteqgis-") as directory, ExitStack() as stack:
        root = Path(directory)
        unresponsive = stack.enter_context(socket.socket())
        unresponsive.bind(("127.0.0.1", 0))
        unresponsive.listen(8)
        timeout_port = unresponsive.getsockname()[1]
        tiles = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), FixtureTiles))
        Thread(target=tiles.serve_forever, daemon=True).start()
        stack.callback(tiles.shutdown)
        port_socket = socket.socket()
        with port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        token = secrets.token_urlsafe(32)
        settings = {"port": port, "timeout_port": timeout_port, "token": token, "version": args.stage, "tiles": tiles.server_port}
        (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        (root / "shared").mkdir()
        with ZipFile(args.zip) as archive:
            assert archive.testzip() is None
        profiles = root / "qgis"
        processes = {}

        def launch(role):
            env = dict(
                os.environ, REMOTEQGIS_TEST_ROOT=str(root), REMOTEQGIS_TEST_ROLE=role,
                REMOTEQGIS_TEST_ZIP=str(args.zip.resolve()),
                REMOTEQGIS_TEST_SCRIPTS=str(ROOT / "tests/integration"),
            )
            if args.offscreen:
                env["QT_QPA_PLATFORM"] = "offscreen"
            log = stack.enter_context((root / f"{role}.log").open("wb"))
            process = subprocess.Popen(
                [
                    str(args.qgis.resolve()), "--nologo", "--noversioncheck", "--noplugins",
                    "--profiles-path", str(profiles), "--profile", role,
                    "--code", str(ROOT / "tests/integration/desktop_scenario.py"),
                ],
                env=env, stdout=log, stderr=subprocess.STDOUT,
            )
            stack.callback(stop_process, process)
            processes[role] = process

        try:
            launch("host")
            wait_for(root, "host-ready", processes, 35)
            auth = {"Authorization": f"Bearer {token}"}
            info = json.loads(expect_http(port, "/api/v1/info", 200, auth))
            assert info["host"]["project_name"] == "测试项目 · 初始"
            expect_http(port, "/api/v1/info", 401)
            expect_http(port, "/api/v1/info", 403, {**auth, "Origin": "https://example.org"})
            expect_http(port, "/api/v1/files", 404, auth)
            upgrade = {
                "Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
            }
            expect_http(port, "/api/v1/events", 401, upgrade)
            expect_http(port, "/api/v1/events", 403, {**upgrade, **auth, "Origin": "https://example.org"})
            print("PASS: HTTP response, unauthorized requests, and WebSocket upgrade authentication", flush=True)
            launch("client")
            wait_for(root, "client-done", processes, 85 if args.stage > 1 else 40)
            print((root / "client-done").read_text(encoding="utf-8"), flush=True)
            (root / "finish").touch()
            wait_for(root, "host-done", processes, 12)
            with socket.socket() as probe:
                probe.settimeout(2)
                assert probe.connect_ex(("127.0.0.1", port)) != 0, "Listener still accepts connections"
            print((root / "host-done").read_text(encoding="utf-8"), flush=True)
        except Exception:
            for file in sorted(root.glob("*-error")) + sorted(root.glob("*.log")):
                print(f"{file.name}\n{file.read_text(encoding='utf-8', errors='replace')[-6000:]}")
            raise


if __name__ == "__main__":
    main()
