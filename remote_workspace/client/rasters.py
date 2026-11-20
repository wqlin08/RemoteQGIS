# SPDX-License-Identifier: GPL-3.0-or-later

import math

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsDataSourceUri, QgsNetworkAccessManager,
    QgsProject, QgsRasterLayer,
)
from qgis.PyQt.QtCore import QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkRequest

from ..common.workspace import TILES

HALF_WORLD = 20037508.342789244


class RasterTiles:
    def __init__(self, connection, canvas):
        self.connection = connection
        self.canvas = canvas
        self.descriptors = []
        self._preprocessor = None
        self._replies = []
        self._timer = QTimer(canvas, interval=300, singleShot=True)
        self._timer.timeout.connect(self.prefetch)
        canvas.extentsChanged.connect(self._schedule)
        connection.status_changed.connect(self._status)
        self.hits = self.requests = 0

    def _status(self, state, _):
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
        self._schedule()

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

    def _cancel(self):
        self._timer.stop()
        for reply in self._replies[:]:
            reply.abort()

    def _schedule(self):
        self._cancel()
        if self.connection.ready and self.descriptors:
            self._timer.start()

    def prefetch(self):
        project = QgsProject.instance()
        transform = QgsCoordinateTransform(self.canvas.mapSettings().destinationCrs(),
                                           QgsCoordinateReferenceSystem("EPSG:3857"), project)
        try:
            rect = transform.transformBoundingBox(self.canvas.extent())
        except Exception:
            return
        if rect.width() <= 0 or self.canvas.width() <= 0:
            return
        zoom = min(22, max(0, round(math.log2(2 * HALF_WORLD * self.canvas.width() / (256 * rect.width())))))
        size = 1 << zoom
        step = 2 * HALF_WORLD / size
        x = math.floor((rect.center().x() + HALF_WORLD) / step)
        y = math.floor((HALF_WORLD - rect.center().y()) / step)
        manager = QgsNetworkAccessManager.instance()
        for desc in self.descriptors[:1]:
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                if 0 <= x + dx < size and 0 <= y + dy < size:
                    url = self.url(desc).format(z=zoom, x=x + dx, y=y + dy)
                    request = QNetworkRequest(QUrl(url))
                    request.setAttribute(QNetworkRequest.CacheLoadControlAttribute, QNetworkRequest.PreferCache)
                    reply = manager.get(request)
                    self._replies.append(reply)
                    timer = QTimer(reply, interval=5000, singleShot=True)
                    timer.timeout.connect(reply.abort)
                    reply.finished.connect(lambda current=reply: self._done(current))
                    timer.start()

    def _done(self, reply):
        if not reply.error():
            self.requests += 1
            self.hits += bool(reply.attribute(QNetworkRequest.SourceIsFromCacheAttribute))
        self._replies.remove(reply)
        reply.deleteLater()

    def close(self):
        self.connection.status_changed.disconnect(self._status)
        self.canvas.extentsChanged.disconnect(self._schedule)
        self._status("idle", "")
        self._timer.deleteLater()
