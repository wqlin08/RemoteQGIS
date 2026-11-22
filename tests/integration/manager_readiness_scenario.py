# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise the supervisor while the simulated Desktop stays open."""

from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

from qgis.PyQt.QtCore import QCoreApplication

from host_manager.daemon import Manager
from host_manager.settings import Settings
from remote_workspace.common.protocol import new_token


def main(home):
    app = QCoreApplication([])
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
