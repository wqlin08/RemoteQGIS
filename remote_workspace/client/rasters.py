# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import QgsDataSourceUri, QgsNetworkAccessManager, QgsRasterLayer
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from ..common.workspace import TILES


class RasterTiles:
    def __init__(self, connection, failure):
        self.connection = connection
        self.failure = failure
        self.descriptors = []
        self._preprocessor = None
        self._reported = set()
        self._network = QgsNetworkAccessManager.instance()
        self._network.finished.connect(self._tile_finished)
        connection.status_changed.connect(self._status)

    def _status(self, state, _):
        self._reported.clear()
        if self._preprocessor:
            QgsNetworkAccessManager.removeRequestPreprocessor(self._preprocessor)
            self._preprocessor = None
        if state == "connected":
            base = self.connection.origin + TILES + "/"
            token = self.connection.authorization

            def apply_header(request):
                if request.url().toString().startswith(base):
                    request.setRawHeader(b"Authorization", token)
                    request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)

            self._preprocessor = QgsNetworkAccessManager.setRequestPreprocessor(apply_header)

    def url(self, descriptor):
        return f"{self.connection.origin}{TILES}/{descriptor['id']}/{descriptor['tile_version']}/{{z}}/{{x}}/{{y}}.png"

    def uri(self, descriptor):
        uri = QgsDataSourceUri()
        uri.setParam("type", "xyz")
        uri.setParam("url", self.url(descriptor))
        uri.setParam("zmin", "0")
        uri.setParam("zmax", "22")
        return bytes(uri.encodedUri()).decode()

    def layer(self, descriptor):
        layer = QgsRasterLayer(self.uri(descriptor), descriptor["name"], "wms")
        if not layer.isValid():
            raise ValueError("QGIS 无法建立 XYZ 影像代理。")
        return layer

    def set_descriptors(self, descriptors):
        self.descriptors = descriptors

    def _tile_finished(self, reply):
        error = reply.error()
        if not self.connection.ready or error in (QNetworkReply.NoError, QNetworkReply.OperationCanceledError):
            return
        url = reply.request().url().toString()
        prefix = self.connection.origin + TILES + "/"
        if not url.startswith(prefix):
            return
        parts = url[len(prefix):].split("/", 2)
        if len(parts) != 3:
            return
        identifier, version, _ = parts
        descriptor = next((item for item in self.descriptors
                           if item["id"] == identifier and item["tile_version"] == version), None)
        if descriptor is None:
            return
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        if status == 410:
            return
        report = (identifier, version, status, error)
        if report not in self._reported:
            self._reported.add(report)
            reason = f"HTTP {status}" if status is not None else reply.errorString()
            self.failure(f"影像「{descriptor['name']}」的瓦片加载失败（{reason}）。")

    def close(self):
        self._network.finished.disconnect(self._tile_finished)
        self.connection.status_changed.disconnect(self._status)
        self._status("idle", "")
