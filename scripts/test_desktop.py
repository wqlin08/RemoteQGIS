# SPDX-License-Identifier: GPL-3.0-or-later

"""Run the release ZIP in two isolated QGIS Desktop profiles."""

import argparse
from contextlib import ExitStack
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


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
        port_socket = socket.socket()
        with port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        token = secrets.token_urlsafe(32)
        settings = {"port": port, "timeout_port": timeout_port, "token": token, "version": args.stage}
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
