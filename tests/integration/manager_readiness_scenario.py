# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the supervisor while the simulated Desktop stays open."""

from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from qgis.PyQt.QtCore import QCoreApplication, QSettings

from host_manager.daemon import Manager, desktop_runtime
from host_manager.settings import Settings
from remote_workspace.common.protocol import new_token


def check_windows_settings(home):
    local = home / "windows-local"
    runtime = local / "RemoteQGIS/managed"
    profile = runtime / "profiles/remoteqgis-host"
    legacy = profile / "qgis.org/QGIS3.ini"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("[PythonPlugins]\nremoteqgis_managed=true\n", encoding="utf-8")
    native = profile / "QGIS/QGIS3.ini"
    native.parent.mkdir()
    settings = QSettings(str(native), QSettings.IniFormat)
    settings.setValue("PythonPlugins/remoteqgis_managed", False)
    settings.setValue("PythonPlugins/watchDogTimestamp/remoteqgis_managed", 1)
    settings.setValue("locale/userLocale", "zh_CN")
    settings.sync()
    with patch.dict("os.environ", LOCALAPPDATA=str(local)), patch("host_manager.daemon.sys", SimpleNamespace(platform="win32")):
        assert desktop_runtime(home) == runtime
    settings.sync()
    assert settings.value("PythonPlugins/remoteqgis_managed", type=bool)
    assert not settings.contains("PythonPlugins/watchDogTimestamp/remoteqgis_managed")
    assert settings.value("locale/userLocale") == "zh_CN"
    assert legacy.read_text(encoding="utf-8") == "[PythonPlugins]\nremoteqgis_managed=true\n"
    print("PASS: Windows managed profile settings path and preservation", flush=True)


def main(home):
    app = QCoreApplication([])
    check_windows_settings(home)
    control = new_token()
    manager = Manager(app, home, Settings.read(home / "settings.json"), new_token(), control)
    manager.timer.stop()
    try:
        manager.worker = SimpleNamespace(poll=lambda: None, pid=12345)
        manager.stop_file = home / "simulated-desktop.stop"
        manager.wanted, manager.worker_state = True, "starting"
        manager.deadline = time.monotonic() - 1
        manager.healthy = lambda: False

        manager.check_desktop()
        assert manager.status()["start_failed"] and manager.status()["desktop_active"]
        assert "尚未加载" in manager.detail and not manager.stop_file.exists()
        manager.stop_file.with_suffix(".python").touch()
        manager.check_desktop()
        assert "已初始化 Python" in manager.detail and not manager.stop_file.exists()
        manager.stop_file.with_suffix(".active").touch()
        manager.check_desktop()
        assert "GIS 服务未就绪" in manager.detail and not manager.stop_file.exists()
        manager.stop_file.with_suffix(".ready").touch()
        manager.check_desktop()
        assert manager.status()["state"] == "running" and not manager.status()["start_failed"]

        manager.execute("manager_stop", {"control": control})
        manager.tick()
        assert manager.stop_file.exists() and manager.status()["state"] == "stopping"
        with patch("host_manager.daemon.close_desktop_window") as close:
            with patch("host_manager.daemon.time.monotonic", return_value=manager.stop_deadline + 1):
                manager.tick()
            close.assert_not_called()  # The managed plugin may be saving a project.
            assert manager.status()["state"] == "error" and "尚未确认关闭" in manager.detail
            manager.tick()
            assert manager.stop_deadline < 0
            manager.stop_file.with_suffix(".active").unlink()
            manager.execute("manager_stop", {"control": control})
            manager.tick()
            with patch("host_manager.daemon.time.monotonic", return_value=manager.stop_deadline + 1):
                manager.tick()
            close.assert_called_once_with(manager.worker.pid, home / "host.log")
        print("PASS: startup stages, readiness without self-HTTP, bounded graceful close", flush=True)
    finally:
        for path in home.glob("simulated-desktop.*"):
            path.unlink()
        manager.service.stop()
        manager.service._thread.join(timeout=5)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
