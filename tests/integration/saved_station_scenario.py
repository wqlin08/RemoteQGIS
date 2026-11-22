# SPDX-License-Identifier: GPL-3.0-or-later

"""Run in an isolated Desktop profile and connect through the saved-station UI."""

import json
import os
from pathlib import Path
import secrets
import traceback
from uuid import uuid4

from pyplugin_installer import instance as plugin_installer
from qgis import utils
from qgis.PyQt.QtCore import QTimer
from qgis.core import QgsRasterLayer, QgsRectangle, QgsVectorLayer

home = Path(os.environ["REMOTEQGIS_TEST_ROOT"])
token = os.environ.pop("REMOTEQGIS_TEST_TOKEN")
config = json.loads((home / "settings.json").read_text(encoding="utf-8"))
identifier = uuid4().hex
timer = QTimer()
panel = None
requested = False
checked_rejection = False


def failed():
    (home / "saved-station-error").write_text(traceback.format_exc(), encoding="utf-8")
    timer.stop()


def setup():
    global panel
    try:
        plugin_installer().installFromZipFile(os.environ["REMOTEQGIS_TEST_ZIP"])
        plugin = utils.plugins["remote_workspace"]
        plugin._action.trigger()
        panel = plugin._panel
        from remote_workspace.common.credentials import delete_token, save_token

        account = "client-" + identifier
        try:
            save_token(account, secrets.token_urlsafe(32))
            panel.stations.profiles[identifier] = {
                "name": "隔离工作站", "host": "127.0.0.1", "port": config["port"],
                "manager_port": config["manager_port"], "tls": False,
            }
            panel.stations.persist(identifier)
            panel.connect_button.click()
            settings = panel.stations.settings.value("remoteqgis/workstations")
            assert token not in settings
            assert panel.stations.pending is not None
        finally:
            delete_token(account)
        timer.timeout.connect(tick)
        timer.start(100)
    except Exception:
        failed()


def tick():
    global requested, checked_rejection
    try:
        if not checked_rejection:
            if panel._client_state != "error":
                return
            assert "拒绝了访问令牌" in panel.client_status.text(), panel.client_status.text()
            checked_rejection = True
            panel.remote_token.setText(token)
            panel.connect_button.click()
            return
        assert panel._client_state != "error", panel.client_status.text()
        if not panel._connection.ready:
            return
        if not requested:
            requested = True
            panel._connection.call("import", {"paths": ["测试数据/影像.tif", "测试数据/points.gpkg"]}, imported)
            return
        proxies = panel.browser.proxies
        if len(proxies.layers) == 2:
            layers = [proxies.project.mapLayer(local) for local in proxies.layers.values()]
            raster, = (layer for layer in layers if isinstance(layer, QgsRasterLayer))
            assert raster.isValid() and raster.bandCount() == 3 and raster.providerType() == "gdal"
            block = raster.dataProvider().block(2, QgsRectangle(100.5, 28.5, 101.5, 29.5), 16, 16)
            assert block.isValid() and block.value(8, 8) == 2000
            assert any(isinstance(layer, QgsVectorLayer) and layer.isValid() and layer.featureCount() == 1 for layer in layers)
            timer.stop()
            panel.disconnect_button.click()
            assert not panel._connection.active
            (home / "saved-station-ok").touch()
    except Exception:
        failed()


def imported(state, error):
    try:
        assert not error and len(state["imported"]) == 2, error
        panel.browser.proxies.apply(state)
    except Exception:
        failed()


QTimer.singleShot(100, setup)
