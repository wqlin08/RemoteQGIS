# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import QgsReferencedRectangle


class DesktopView:
    def __init__(self, canvas):
        self.canvas = canvas

    def crs(self):
        return self.canvas.mapSettings().destinationCrs()

    def extent(self):
        return self.canvas.extent()

    def rotation(self):
        return self.canvas.rotation()

    def scale(self):
        return self.canvas.scale()

    def apply(self, extent, rotation, scale):
        self.canvas.setExtent(extent)
        self.canvas.setRotation(rotation)
        self.canvas.refresh()

    def opened(self, project):
        extent = project.viewSettings().defaultViewExtent()
        if not extent.isEmpty():
            self.canvas.setExtent(extent)
        self.canvas.refresh()

    def cleared(self):
        pass

    def save(self, project):
        project.viewSettings().setDefaultViewExtent(QgsReferencedRectangle(self.extent(), self.crs()))
