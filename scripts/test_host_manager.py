# SPDX-License-Identifier: GPL-3.0-or-later

"""Smoke-test the packaged Host Manager against an isolated shared directory."""

import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
from tempfile import TemporaryDirectory

from test_headless import ROOT, free_port, ready, rpc, wait_for


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--client-zip", type=Path)
    parser.add_argument("--qgis", type=Path)
    args = parser.parse_args()
    if bool(args.client_zip) != bool(args.qgis):
        parser.error("--client-zip and --qgis must be used together")
    with TemporaryDirectory(prefix="remoteqgis-manager-") as directory:
        home = Path(directory)
        shared = home / "shared"
        shared.mkdir()
        launcher = str(args.launcher.resolve())
        subprocess.run([launcher, "--smoke-test-ui", "--state-dir", str(home)], timeout=15, check=True)
        subprocess.run([launcher, "--python", str(ROOT / "tests/integration/make_headless_fixtures.py"),
                        str(home)], timeout=15, check=True)
        manager_port, gis_port = free_port(), free_port()
        while manager_port == gis_port:
            gis_port = free_port()
        config = {"manager_port": manager_port, "port": gis_port, "shared_directory": str(shared),
                  "allow_preview": args.preview}
        (home / "settings.json").write_text(json.dumps(config), encoding="utf-8")
        token, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)

        def state(name):
            result = rpc(manager_port, token, "manager_status")
            return result if result["state"] == name else None

        def launch(start):
            command = [launcher, "--daemon", "--state-dir", str(home), "--test-credentials-stdin"]
            if start:
                command.append("--start-worker")
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT)
            proc.stdin.write(f"{token}\n{control}\n")
            proc.stdin.close()
            return proc

        proc = None
        try:
            proc = launch(False)
            wait_for(lambda: ready(manager_port, token) or proc.poll() is not None)
            assert proc.poll() is None, proc.stdout.read()
            assert state("stopped")
            if args.client_zip:
                desktop_roundtrip(home, token, args)
            else:
                rpc(manager_port, token, "manager_start")
            wait_for(lambda: state("running"))
            assert ready(gis_port, token)
            rpc(manager_port, token, "edit", status=404)
            rpc(manager_port, token, "manager_stop", status=403)
            if not args.client_zip:
                rpc(gis_port, token, "import", {"paths": ["测试数据/影像.tif", "测试数据/points.gpkg"]})
            rpc(gis_port, token, "view", {"crs": "EPSG:4326", "extent": [98, 26, 104, 32], "rotation": 12})
            if args.preview:
                rpc(manager_port, token, "manager_preview")
                wait_for(lambda: rpc(manager_port, token, "manager_status")["preview"])
                def displayed():
                    log = home / "preview.log"
                    contents = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
                    return "RemoteQGIS preview layers: 2" in contents and "RemoteQGIS preview rotation: 12" in contents

                wait_for(displayed)
            rpc(gis_port, token, "project", {"action": "save_as", "path": "managed.qgz"})
            rpc(manager_port, token, "manager_stop", {"control": control})
            wait_for(lambda: state("stopped"))
            assert not ready(gis_port, token)
            rpc(manager_port, token, "manager_shutdown", {"control": control})
            assert proc.wait(timeout=15) == 0, proc.stdout.read()
            proc.stdout.close()

            (home / "settings.json").write_text(json.dumps(dict(config, project="managed.qgz")), encoding="utf-8")
            proc = launch(True)
            wait_for(lambda: ready(manager_port, token))
            wait_for(lambda: state("running"))
            assert len(rpc(gis_port, token, "state")["layers"]) == 2
            if os.name == "posix":
                lock = shared / ".remoteqgis-writer.lock"
                worker_pid = int(lock.read_text().splitlines()[0])
                os.kill(worker_pid, signal.SIGKILL)

                def restored():
                    return (lock.exists() and lock.read_text().splitlines()[0] != str(worker_pid)
                            and state("running") and len(rpc(gis_port, token, "state")["layers"]) == 2)

                wait_for(restored)
            rpc(manager_port, token, "manager_shutdown", {"control": control})
            assert proc.wait(timeout=15) == 0, proc.stdout.read()
            for port in (manager_port, gis_port):
                with socket.socket() as probe:
                    assert probe.connect_ex(("127.0.0.1", port)) != 0
        except Exception:
            for path in sorted(home.glob("*.log")):
                print(path.name, path.read_text(encoding="utf-8", errors="replace")[-5000:])
            raise
        finally:
            if proc is not None:
                stop_process(proc)
                proc.stdout.close()
        print("PASS: Manager GUI, authenticated on-demand startup, shutdown, restart and project restoration"
              + (", QGIS Desktop preview" if args.preview else "")
              + (", and saved-station QGIS client" if args.client_zip else ""))


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def desktop_roundtrip(home, token, args):
    env = dict(os.environ, REMOTEQGIS_TEST_ROOT=str(home), REMOTEQGIS_TEST_TOKEN=token,
               REMOTEQGIS_TEST_ZIP=str(args.client_zip.resolve()))
    with (home / "client.log").open("wb") as log:
        proc = subprocess.Popen([
            str(args.qgis.resolve()), "--nologo", "--noversioncheck", "--noplugins",
            "--profiles-path", str(home / "client-profiles"), "--profile", "managed-client",
            "--code", str(ROOT / "tests/integration/saved_station_scenario.py"),
        ], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            error = home / "saved-station-error"
            wait_for(lambda: (home / "saved-station-ok").exists() or error.exists() or proc.poll() is not None)
            assert (home / "saved-station-ok").exists(), error.read_text(encoding="utf-8") if error.exists() else proc.returncode
        finally:
            stop_process(proc)


if __name__ == "__main__":
    main()
