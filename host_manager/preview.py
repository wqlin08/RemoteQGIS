# SPDX-License-Identifier: GPL-3.0-or-later

"""Run within an isolated QGIS Desktop process launched by Host Manager."""

import os
import sys


def start():
    from qgis.PyQt.QtCore import QCoreApplication, QTimer, QUrl
    from qgis.core import Qgis, QgsProject
    from qgis.utils import iface

    sys.path.insert(0, os.environ.pop("REMOTEQGIS_PREVIEW_SOURCE"))
    from remote_workspace.client.connection import Connection
    from remote_workspace.client.proxy import Proxies

    connection = Connection(iface.mainWindow())
    warning = lambda message: iface.messageBar().pushMessage("RemoteQGIS", message, Qgis.Warning, 5)
    layer_count = 0

    def layers_changed():
        nonlocal layer_count
        count = len(proxies.layers)
        if count != layer_count:
            layer_count = count
            print(f"RemoteQGIS preview layers: {count}", flush=True)

    proxies = Proxies(connection, iface, layers_changed, warning, warning, read_only=True)
    previous_view = None
    waiting = False

    def update_view(view, error):
        nonlocal previous_view, waiting
        waiting = False
        if not error and view != previous_view:
            initial = previous_view is None
            previous_view = view
            proxies._initial_view(view)
            if initial:
                print(f"RemoteQGIS preview rotation: {iface.mapCanvas().rotation():g}", flush=True)

    def follow_view():
        nonlocal waiting
        if connection.ready and not waiting:
            waiting = True
            connection.call("view_state", {}, update_view)

    def status(state, detail):
        nonlocal previous_view
        if state == "connected":
            previous_view = None
            iface.messageBar().pushMessage("RemoteQGIS", "只读展示已连接。", Qgis.Info, 4)
        elif state == "error":
            warning(detail)

    timer = QTimer(iface.mainWindow(), interval=700)
    timer.timeout.connect(follow_view)
    timer.start()
    connection.status_changed.connect(status)
    connection.start(QUrl(os.environ.pop("REMOTEQGIS_PREVIEW_ORIGIN")), os.environ.pop("REMOTEQGIS_PREVIEW_TOKEN"))
    iface.mainWindow().setWindowTitle("RemoteQGIS · 只读展示")

    def close():
        timer.stop()
        # A read-only viewer project is disposable.
        QgsProject.instance().setDirty(False)
        proxies.close()
        connection.stop()

    QCoreApplication.instance().aboutToQuit.connect(close)


if "REMOTEQGIS_PREVIEW_SOURCE" in os.environ:
    from qgis.PyQt.QtCore import QTimer

    QTimer.singleShot(0, start)
