# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.PyQt.QtCore import QCoreApplication, Qt
from qgis.PyQt.QtWidgets import QAction

from .ui.panel import WorkspacePanel


class RemoteWorkspacePlugin:
    def __init__(self, iface):
        self.iface = iface
        self._panel = None

    def initGui(self):
        self._action = QAction("RemoteQGIS", self.iface.mainWindow())
        self._action.triggered.connect(self._show_workspace)
        self.iface.addPluginToMenu("&RemoteQGIS", self._action)
        QCoreApplication.instance().aboutToQuit.connect(self._shutdown)

    def _show_workspace(self):
        if self._panel is None:
            self._panel = WorkspacePanel(self.iface)
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self._panel)
        self._panel.show()
        self._panel.raise_()

    def _shutdown(self):
        if self._panel is not None:
            self._panel.shutdown()

    def unload(self):
        QCoreApplication.instance().aboutToQuit.disconnect(self._shutdown)
        self._shutdown()
        if self._panel is not None:
            self.iface.removeDockWidget(self._panel)
            self._panel.deleteLater()
            self._panel = None
        self.iface.removePluginMenu("&RemoteQGIS", self._action)
        self._action.deleteLater()
