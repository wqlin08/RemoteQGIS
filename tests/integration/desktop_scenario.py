# SPDX-License-Identifier: GPL-3.0-or-later

"""Runs inside QGIS Desktop through the --code option."""

import faulthandler
import json
import os
from pathlib import Path
import secrets
import traceback
import sys

from pyplugin_installer import instance as plugin_installer
from qgis import utils
from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QTimer

faulthandler.enable()
root = Path(os.environ["REMOTEQGIS_TEST_ROOT"])
role = os.environ["REMOTEQGIS_TEST_ROLE"]
settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
port, timeout_port, token = settings["port"], settings["timeout_port"], settings["token"]
project = QgsProject.instance()
key = "remote_workspace"
version = settings.get("version", 1)
if version > 1:
    sys.path.insert(0, os.environ["REMOTEQGIS_TEST_SCRIPTS"])
    from workspace_scenario import Scenario, fixtures

stage = -1
timer = QTimer()


def mark(name: str, text: str = ""):
    temporary = root / f"{role}.marker"
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(root / name)


def guarded(action):
    try:
        action()
    except Exception:
        timer.stop()
        mark(f"{role}-error", traceback.format_exc())


def setup():
    global panel, scenario
    plugin_installer().installFromZipFile(os.environ["REMOTEQGIS_TEST_ZIP"])
    assert utils.isPluginLoaded(key), "QGIS could not install and start the ZIP"
    plugin = utils.plugins[key]
    plugin._action.trigger()
    panel = plugin._panel
    assert panel.isVisible() and "QGIS 3." in panel.widget().layout().itemAt(1).widget().text()
    if role == "host":
        if version > 1:
            fixtures(root, version)
        project.setTitle("测试项目 · 初始")
        panel.bind_port.setValue(timeout_port)
        panel.directory.setText(str(root / "shared"))
        panel.host_token.setText(token)
        panel.start_host.click()
    else:
        if version > 1:
            scenario = Scenario(panel, version, mark, root)
        panel.tabs.setCurrentIndex(1)
        panel.remote_port.setValue(timeout_port)
        panel.remote_token.setText(token)
        panel.connect_button.click()
    timer.timeout.connect(lambda: guarded(host_tick if role == "host" else client_tick))
    timer.start(100)


def host_tick():
    global stage, service
    if stage == -1:
        if panel._host_state == "error":
            panel.bind_port.setValue(port)
            panel.start_host.click()
            stage = 0
        return
    assert panel._host_state != "error", panel.host_status.text()
    if stage == 0 and panel._host_state == "running":
        mark("host-ready")
        stage = 1
    if stage == 1 and (root / "client-connected").exists():
        project.setTitle("测试项目 · 已变更")
        stage = 2
    if stage == 2 and (root / "client-update").exists():
        panel.stop_host.click()
        stage = 3
    if stage == 3 and panel._host_state == "stopped" and (root / "client-retry").exists():
        project.setTitle("")
        project.setFileName(str(root / "测试项目 · 服务重启.qgz"))
        panel.start_host.click()
        stage = 4
    if stage == 4 and (root / "workspace-imported").exists() and not (root / "workspace-host-verified").exists():
        assert len(project.mapLayers()) == 3
        assert all(str(root / "shared") in layer.source() for layer in project.mapLayers().values())
        mark("workspace-host-verified")
    if stage == 4 and version >= 4 and not (root / "project-layout-verified").exists():
        layers = [layer for layer in project.mapLayers().values() if layer.name() == "远程点样式"]
        if layers and abs(panel.iface.mapCanvas().rotation() - 12) < 0.1:
            layer = layers[0]
            assert abs(layer.opacity() - 0.55) < 1e-6
            assert layer.renderer().symbol().color().name() == "#a33b87"
            assert project.layerTreeRoot().findLayer(layer.id()).parent().name() == "远程数据组"
            mark("project-layout-verified")
    if stage == 4 and (root / "finish").exists():
        service = panel._host
        assert utils.unloadPlugin(key)
        stage = 5
    if stage == 5 and service.status.state == "stopped":
        timer.stop()
        mark("host-done", "PASS: Host recovered from a busy port and released its listener on unload")


def client_tick():
    global stage
    state = panel._client_state
    status = panel.client_status.text()
    labels = [label.text() for label in panel.info_labels]
    if stage == -1 and state == "retry":
        assert "超时" in status, status
        panel.disconnect_button.click()
        panel.remote_port.setValue(port)
        panel.remote_token.setText(secrets.token_urlsafe(32))
        panel.connect_button.click()
        stage = 0
        return
    if stage == 0 and state == "error":
        assert "拒绝访问" in status, status
        panel.remote_token.setText(token)
        panel.connect_button.click()
        stage = 1
        return
    if stage > 0:
        assert state != "error", status
    if stage == 1 and state == "connected" and labels[-1].endswith(" ms"):
        assert labels[0] != "—" and labels[1] == Qgis.QGIS_VERSION and labels[2] != "—", labels
        assert labels[3] == "测试项目 · 初始", labels
        mark("client-connected")
        stage = 2
    if stage == 2 and labels[3] == "测试项目 · 已变更":
        mark("client-update")
        stage = 3
    if stage == 3 and state == "retry":
        assert labels == ["—"] * 5, labels
        mark("client-retry")
        stage = 4
    if stage == 4 and state == "connected" and (labels[3] == "测试项目 · 服务重启"
                                                   or version > 1 and scenario.step > 0):
        if version > 1 and not scenario.tick():
            return
        panel.disconnect_button.click()
        assert panel._client_state == "idle" and not panel._connection.active
        assert utils.unloadPlugin(key)
        timer.stop()
        mark(
            "client-done",
            "PASS: Two QGIS Desktop sessions handled a request timeout, authenticated, displayed the "
            "heartbeat, pushed a project change, reconnected after service restart, and disconnected cleanly",
        )


QTimer.singleShot(100, lambda: guarded(setup))
