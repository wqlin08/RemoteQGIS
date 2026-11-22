# SPDX-License-Identifier: GPL-3.0-or-later

from collections import deque
from concurrent.futures import Future

from qgis.PyQt.QtCore import QByteArray, QBuffer, QIODevice, QObject, QSize, Qt, pyqtSignal, pyqtSlot
from qgis.PyQt.QtGui import QColor
from qgis.core import QgsCoordinateReferenceSystem, QgsMapRendererParallelJob, QgsMapSettings, QgsRectangle

from ..common.workspace import WorkspaceError
from .rasters import TILE_SIZE, tile_bounds


class Dispatcher(QObject):
    changed = pyqtSignal()
    requested = pyqtSignal(object, str, object)
    tile_requested = pyqtSignal(object, str, str, int, int, int)

    def __init__(self, workspace, parent=None):
        super().__init__(parent)
        self.workspace = workspace
        self.active = True
        self._busy = False
        self._queue = deque()
        self._tile_jobs = set()
        self.requested.connect(self._run, Qt.QueuedConnection)
        self.tile_requested.connect(self._render_tile, Qt.QueuedConnection)

    @property
    def busy(self):
        return self._busy

    def stop(self):
        self.active = False
        for job in tuple(self._tile_jobs):
            job.cancelWithoutBlocking()

    def submit(self, operation: str, data: dict) -> Future:
        result = Future()
        self.requested.emit(result, operation, data)
        return result

    def submit_tile(self, identifier, version, zoom, x, y):
        result = Future()
        self.tile_requested.emit(result, identifier, version, zoom, x, y)
        return result

    @pyqtSlot(object, str, str, int, int, int)
    def _render_tile(self, result, identifier, version, zoom, x, y):
        if result.cancelled():
            return
        try:
            job = self._tile_job(identifier, version, zoom, x, y)
        except Exception as exc:
            if result.set_running_or_notify_cancel():
                result.set_exception(exc)
            return
        self._tile_jobs.add(job)

        def finished():
            try:
                if not result.set_running_or_notify_cancel():
                    return
                if not self.active:
                    raise WorkspaceError("工作站服务已停止。", 503)
                data = QByteArray()
                buffer = QBuffer(data)
                image = job.renderedImage()
                if job.errors() or not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, "PNG"):
                    raise WorkspaceError("工作站无法渲染影像瓦片。", 422)
                result.set_result(bytes(data))
            except Exception as exc:
                result.set_exception(exc)
            finally:
                self._tile_jobs.discard(job)
                job.finished.disconnect(finished)
                job.deleteLater()

        job.finished.connect(finished)
        job.start()

    def _tile_job(self, identifier, version, zoom, x, y):
        if not self.active:
            raise WorkspaceError("工作站服务已停止。", 503)
        project = self.workspace.project
        layer = project.mapLayer(identifier)
        spec = self.workspace.rasters.get(identifier)
        if layer is None or spec is None or spec["version"] != version:
            raise WorkspaceError("工作区或瓦片版本已更新。", 410)
        settings = QgsMapSettings()
        settings.setLayers([layer])
        settings.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
        settings.setTransformContext(project.transformContext())
        settings.setBackgroundColor(QColor(Qt.transparent))
        settings.setOutputSize(QSize(TILE_SIZE, TILE_SIZE))
        settings.setExtent(QgsRectangle(*tile_bounds(zoom, x, y)))
        job = QgsMapRendererParallelJob(settings)
        job.setParent(self)
        return job

    @pyqtSlot(object, str, object)
    def _run(self, result: Future, operation: str, data: dict):
        self._queue.append((result, operation, data))
        if self._busy:
            return
        self._busy = True
        try:
            while self._queue:
                self._execute(*self._queue.popleft())
        finally:
            self._busy = False

    def _execute(self, result, operation, data):
        if not result.set_running_or_notify_cancel():
            return
        try:
            if not self.active:
                raise WorkspaceError("工作站服务已停止。", 503)
            value = self.workspace.execute(operation, data)
            if operation not in ("state", "files", "vector", "view", "view_state", "processing_catalog", "processing_status", "processing_cancel"):
                self.changed.emit()
            result.set_result(value)
        except Exception as exc:
            result.set_exception(exc)
