# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import QgsCoordinateReferenceSystem, QgsReferencedRectangle, QgsRectangle


class HeadlessView:
    def __init__(self, project):
        self.project = project
        self.cleared()

    def crs(self):
        crs = self.project.crs()
        return crs if crs.isValid() else QgsCoordinateReferenceSystem("EPSG:4326")

    def extent(self):
        if self._extent.isEmpty():
            return self.project.viewSettings().fullExtent()
        return QgsRectangle(self._extent)

    def rotation(self):
        return self._angle

    def scale(self):
        return self._scale

    def apply(self, extent, rotation, scale):
        self._extent, self._angle, self._scale = QgsRectangle(extent), rotation, scale

    def opened(self, project):
        from qgis.core import QgsCoordinateTransform

        settings = project.viewSettings()
        extent = settings.defaultViewExtent()
        if not extent.isEmpty():
            try:
                transform = QgsCoordinateTransform(extent.crs(), self.crs(), project)
                extent = transform.transformBoundingBox(extent)
            except Exception:
                extent = QgsRectangle()
        self.apply(extent, settings.defaultRotation(), None)

    def cleared(self):
        self.apply(QgsRectangle(), 0, None)

    def save(self, project):
        settings = project.viewSettings()
        settings.setDefaultViewExtent(QgsReferencedRectangle(self.extent(), self.crs()))
        settings.setDefaultRotation(self.rotation())
