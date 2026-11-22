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


def quiet_profile(root, profile):
    """Keep the managed QGIS profile from downloading fonts on first use."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "profiles.ini").write_text(
        f"[core]\nlastProfile={profile}\nselectionPolicy=0\n", encoding="utf-8"
    )
    settings = root / "profiles" / profile / "qgis.org/QGIS3.ini"
    settings.parent.mkdir(parents=True, exist_ok=True)
    # QGIS classifies fixture files under the system temp directory as temporary layers.
    settings.write_text("[fonts]\ndownloadMissingFonts=false\n[app]\naskToSaveMemoryLayers=false\n",
                        encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--client-zip", type=Path)
    parser.add_argument("--qgis", type=Path)
    args = parser.parse_args()
    if bool(args.client_zip) != bool(args.qgis):
        parser.error("--client-zip and --qgis must be used together")
    with TemporaryDirectory(prefix="remoteqgis-manager-") as directory:
        home = Path(directory).resolve()
        shared = home / "shared"
        shared.mkdir()
        another = home / "其他数据"
        (another / "子目录").mkdir(parents=True)
        if args.client_zip:
            quiet_profile(home / "client-profiles", "managed-client")
        launcher = str(args.launcher.resolve())
        subprocess.run([launcher, "--smoke-test-ui", "--state-dir", str(home)], timeout=15, check=True)
        subprocess.run([launcher, "--python", str(ROOT / "tests/integration/manager_request_scenario.py")],
                       timeout=15, check=True)
        subprocess.run([launcher, "--python", str(ROOT / "tests/integration/make_headless_fixtures.py"),
                        str(home), "3"], timeout=30, check=True)
        manager_port, gis_port = free_port(), free_port()
        while manager_port == gis_port:
            gis_port = free_port()
        config = {"manager_port": manager_port, "port": gis_port, "shared_directory": str(shared),
                  "additional_directories": [str(another)]}
        (home / "settings.json").write_text(json.dumps(config), encoding="utf-8")
        token, control = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        subprocess.run([launcher, "--python", str(ROOT / "tests/integration/manager_readiness_scenario.py"),
                        str(home)], timeout=15, check=True)

        def state(name):
            result = rpc(manager_port, token, "manager_status")
            return result if result["state"] == name else None

        def launch(start):
            command = [launcher, "--daemon", "--state-dir", str(home), "--test-credentials-stdin"]
            if start:
                command.append("--start-worker")
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, text=True, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=dict(
                                        os.environ, REMOTEQGIS_TEST_CANCEL_CLOSE="1"))
            proc.stdin.write(f"{token}\n{control}\n")
            proc.stdin.close()
            return proc

        proc = None
        try:
            proc = launch(False)
            wait_for(lambda: ready(manager_port, token) or proc.poll() is not None)
            assert proc.poll() is None, proc.stdout.read()
            assert state("stopped")
            rpc(manager_port, token, "manager_start")
            wait_for(lambda: state("running"))
            assert ready(gis_port, token)
            assert next(home.glob("desktop-*.ready")).is_file()
            assert next(home.glob("desktop-*.python")).is_file()
            algorithms = rpc(gis_port, token, "processing_catalog")["algorithms"]
            assert {"native:buffer", "gdal:slope"} <= set(algorithms), algorithms
            rpc(manager_port, token, "manager_stop", {"control": control})
            wait_for(lambda: state("stopped"))
            quiet_profile(home / "desktop", "remoteqgis-host")
            with (home / "desktop/profiles/remoteqgis-host/qgis.org/QGIS3.ini").open("a", encoding="utf-8") as settings:
                settings.write("\n[PythonPlugins]\nwatchDogTimestamp\\remoteqgis_managed=1\n")
            with socket.socket() as occupied:
                if os.name == "posix":
                    occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", gis_port))
                occupied.listen()
                rpc(manager_port, token, "manager_start")
                conflict = wait_for(lambda: (value := state("error")) and value.get("port_in_use") and value)
                assert conflict.get("port_in_use") and not conflict["requested"], conflict
                assert f"127.0.0.1:{gis_port}" in conflict["detail"] and "已被占用" in conflict["detail"]
                assert "unclosed <socket.socket" not in (home / "host.log").read_text(encoding="utf-8")
                assert occupied.getsockname()[1] == gis_port
            if args.client_zip:
                rpc(manager_port, token, "manager_stop", {"control": control})
                wait_for(lambda: state("stopped"))
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
            wait_for(lambda: "RemoteQGIS Desktop Host ready" in
                     (home / "host.log").read_text(encoding="utf-8", errors="replace"))
            rpc(gis_port, token, "workspace_select", {"path": "测试数据"}, status=409)
            rpc(gis_port, token, "project", {"action": "save_as", "path": "managed.qgz"})
            listed = rpc(gis_port, token, "workspace_directories")
            assert any(entry["name"] == "测试数据" for entry in listed["entries"])
            assert listed["roots"] == [{"id": "0", "name": str(shared)}, {"id": "1", "name": str(another)}]
            rpc(gis_port, token, "workspace_select", {"path": "../"}, status=400)
            rpc(gis_port, token, "workspace_select", {"root": "2"}, status=403)
            rpc(gis_port, token, "workspace_select", {"root": "1", "path": "../"}, status=400)
            assert rpc(gis_port, token, "workspace_select", {"root": "1"})["changed"]
            assert rpc(gis_port, token, "workspace_directories")["selected_root"] == "1"
            assert {entry["name"] for entry in rpc(gis_port, token, "files")["entries"]} == {"子目录"}
            assert json.loads((home / "workspace.json").read_text(encoding="utf-8"))["root"] == str(another)
            assert rpc(gis_port, token, "workspace_select", {"path": "测试数据"})["changed"]
            paths = {entry["path"] for entry in rpc(gis_port, token, "files")["entries"]}
            assert "影像.tif" in paths and "managed.qgz" not in paths, paths
            assert not rpc(gis_port, token, "state")["layers"]
            nested = rpc(gis_port, token, "import", {"paths": ["影像.tif", "points.gpkg"]})
            vector = next(layer for layer in nested["layers"] if layer["kind"] == "vector")
            assert vector.get("workspace_scope")
            rpc(gis_port, token, "edit", {"id": vector["id"]}, status=409)
            rpc(gis_port, token, "project", {"action": "save_as", "path": "nested.qgz"})
            # A canceled close must keep the Desktop, the Host and the unsaved
            # project running; the project also stays protected from switching.
            rpc(gis_port, token, "import", {"paths": ["影像.tif"]})

            def canceled_close():
                value = state("running")
                return value if value and value["requested"] and "取消" in value["detail"] else None

            rpc(manager_port, token, "manager_stop", {"control": control})
            wait_for(canceled_close, 20)
            assert rpc(gis_port, token, "state")["layers"]
            rpc(gis_port, token, "workspace_select", {"path": ""}, status=409)
            rpc(gis_port, token, "project", {"action": "close", "discard": True})
            rpc(manager_port, token, "manager_stop", {"control": control})
            wait_for(lambda: state("stopped"))
            assert not ready(gis_port, token)
            rpc(manager_port, token, "manager_shutdown", {"control": control})
            assert proc.wait(timeout=15) == 0, proc.stdout.read()
            proc.stdout.close()

            (home / "settings.json").write_text(json.dumps(dict(config, project="测试数据/nested.qgz")), encoding="utf-8")
            proc = launch(True)
            wait_for(lambda: ready(manager_port, token))
            wait_for(lambda: state("running"))
            restored_state = rpc(gis_port, token, "state")
            assert len(restored_state["layers"]) == 2, (
                restored_state["project"], rpc(gis_port, token, "workspace_directories"),
                (home / "workspace.json").read_text(encoding="utf-8"),
            )
            assert rpc(gis_port, token, "workspace_directories")["selected"] == "测试数据"
            if os.name == "posix":
                lock = shared / ".remoteqgis-writer.lock"
                worker_pid = int(lock.read_text().splitlines()[0])
                command = subprocess.check_output(["ps", "-p", str(worker_pid), "-o", "command="], text=True)
                assert "--code" not in command and "--profile remoteqgis-host" in command, command
                os.kill(worker_pid, signal.SIGKILL)

                def restored():
                    return (lock.exists() and lock.read_text().splitlines()[0] != str(worker_pid)
                            and state("running") and len(rpc(gis_port, token, "state")["layers"]) == 2)

                wait_for(restored)
            # Manager shutdown uses the same QGIS save prompt and must also
            # preserve a changed project when the user cancels.
            rpc(gis_port, token, "import", {"paths": ["影像.tif"]})

            def canceled_shutdown():
                value = state("running")
                return value if value and value["requested"] and "取消" in value["detail"] else None

            rpc(manager_port, token, "manager_shutdown", {"control": control})
            wait_for(canceled_shutdown, 20)
            assert rpc(gis_port, token, "state")["layers"]
            # The fixture is disposable. Clear it before closing QGIS to avoid its save dialog.
            rpc(gis_port, token, "project", {"action": "close", "discard": True})
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
        print("PASS: Manager GUI, on-demand QGIS Desktop Host, authorized workspace switching, canceled close,"
              " shutdown, restart and project restoration"
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
