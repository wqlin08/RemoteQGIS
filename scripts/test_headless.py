# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the real Headless Host protocol against disposable QGIS data."""

import argparse
import json
from pathlib import Path
import secrets
import socket
import subprocess
from tempfile import TemporaryDirectory
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from uuid import uuid4
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
HTTP = build_opener(ProxyHandler({}))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def request(port, token, path, *, payload=None, status=200, headers=None):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(f"http://127.0.0.1:{port}{path}", data=body,
                  headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})})
    try:
        with HTTP.open(req, timeout=3) as reply:
            assert reply.status == status
            return reply.read()
    except HTTPError as exc:
        assert exc.code == status, (exc.code, exc.read())
        return exc.read()


def rpc(port, token, operation, data=None, status=200):
    return json.loads(request(port, token, "/api/v2/workspace", payload={
        "operation": operation, "data": data or {},
    }, status=status))


def wait_for(fn, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError("Host did not reach the expected state")


def ready(port, token):
    try:
        return request(port, token, "/api/v1/info")
    except OSError:
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", default=ROOT / "packaging/run-macos", type=Path)
    parser.add_argument("--zip", required=True, type=Path)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="remoteqgis-standalone-") as directory:
        root = Path(directory)
        with ZipFile(args.zip) as archive:
            archive.extractall(root)
        shared = root / "shared"
        shared.mkdir()
        command = [str(args.launcher.resolve()), "--python"]
        subprocess.run([*command, str(ROOT / "tests/integration/make_headless_fixtures.py"), str(root)],
                       check=True)
        port = free_port()
        token, viewer = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        run = lambda op, data=None: rpc(port, token, op, data)
        with (root / "host.log").open("w+") as log:
            proc = subprocess.Popen([*command, "-m", "headless_host", "--profile", str(root / "profile"),
                                     "--directory", str(shared), "--port", str(port), "--viewer"],
                                    cwd=root, stdin=subprocess.PIPE, stdout=log, stderr=log, text=True)
            try:
                proc.stdin.write(f"{token}\n{viewer}\n")
                proc.stdin.flush()
                wait_for(lambda: ready(port, token))
                request(port, "bad", "/api/v1/info", status=401)
                rpc(port, viewer, "project", {"action": "close"}, status=403)
                folders = run("files")
                assert folders["entries"]
                state = run("import", {"paths": ["测试数据/影像.tif", "测试数据/points.gpkg", "测试数据/points.shp"]})
                raster = next(layer for layer in state["layers"] if layer["kind"] == "raster")
                key = raster["id"]
                assert run("pixel", {"id": key, "x": 101, "y": 29})["values"] == [1000, 2000, 3000]
                manifest = rpc(port, viewer, "raster_source", {"id": key})
                assert manifest
                data = request(port, viewer, f"/api/v3/raster/{key}/{raster['source_version']}/source.tif",
                               headers={"Range": "bytes=0-15"}, status=206)
                assert len(data) == 16
                descriptor = next(layer for layer in state["layers"] if layer["path"].endswith(".gpkg"))
                vector = descriptor["id"]
                before = run("enable_edit", {"id": vector})
                feature, = before["features"]
                after = dict(feature, values=["headless"])
                change = {"id": vector, "transaction": str(uuid4()), "base": before["revision"],
                          "fields": before["fields"], "delete": [], "add": [], "update": [after],
                          "dataset": [descriptor["path"], descriptor["table"]]}
                edited = run("edit", change)
                assert run("edit", change) == edited
                assert run("vector", {"id": vector})["features"][0]["values"] == ["headless"]
                run("create_vector", {"path": "new.gpkg", "crs": "EPSG:4326", "geometry": "Polygon", "fields": []})
                catalog = run("processing_catalog")["algorithms"]
                assert len(catalog) == 42, catalog
                for algorithm, parameters, target in (
                    ("native:buffer", {"INPUT": vector, "DISTANCE": 0.1}, "buffer.gpkg"),
                    ("gdal:translate", {"INPUT": key}, "translated.tif"),
                ):
                    job = uuid4().hex
                    run("processing_start", {"job": job, "algorithm": algorithm,
                                             "parameters": parameters, "output": target})
                    status = wait_for(lambda: finished(run, job))
                    assert status["state"] == "finished", status
                    assert (shared / target).is_file()
                run("view", {"crs": "EPSG:4326", "extent": [98, 26, 104, 32], "rotation": 27})
                original_view = rpc(port, viewer, "view_state")
                assert original_view["rotation"] == 27
                run("project", {"action": "save_as", "path": "workspace.qgz"})
                run("project", {"action": "close", "discard": True})
                run("project", {"action": "open", "path": "workspace.qgz", "discard": True})
                restored = run("state")
                assert len(restored["layers"]) == 6 and restored["view"]["rotation"] == 27
                assert restored["view"]["extent"] == original_view["extent"]
                proc.stdin.write("stop\n")
                proc.stdin.flush()
                assert proc.wait(timeout=15) == 0
            except Exception:
                log.seek(0)
                print(log.read())
                raise
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                proc.stdin.close()
        print("PASS: Headless Host auth, read-only preview, TIFF bytes/pixels, vector replay, project and Processing")


def finished(run, job):
    value = run("processing_status", {"job": job})
    return value if value["state"] in ("finished", "failed", "canceled") else None


if __name__ == "__main__":
    main()
