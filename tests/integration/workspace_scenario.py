# SPDX-License-Identifier: GPL-3.0-or-later

"""Synthetic, shared-directory fixtures for the native two-session scenario."""

from osgeo import gdal, ogr, osr

from qgis.core import QgsMimeDataUtils, QgsRasterLayer, QgsRectangle, QgsVectorLayer
from qgis.PyQt.QtCore import Qt


def fixtures(root, version):
    directory = root / "shared" / "测试数据"
    directory.mkdir()
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    size = 4096 if version >= 3 else 32
    raster = gdal.GetDriverByName("GTiff").Create(
        str(directory / "影像.tif"), size, size, 3, gdal.GDT_UInt16,
        options=["TILED=YES", "COMPRESS=DEFLATE"],
    )
    raster.SetGeoTransform((-2, 4 / size, 0, 2, 0, -4 / size))
    raster.SetProjection(srs.ExportToWkt())
    for i in range(1, 4):
        raster.GetRasterBand(i).Fill(i * 1000)
    if version >= 3:
        raster.BuildOverviews("AVERAGE", [2, 4, 8, 16])
    raster = None
    for driver, suffix in (("GPKG", ".gpkg"), ("ESRI Shapefile", ".shp")):
        shape = ogr.GetDriverByName(driver).CreateDataSource(str(directory / ("points" + suffix)))
        layer = shape.CreateLayer("points", srs, ogr.wkbPoint)
        layer.CreateField(ogr.FieldDefn("name", ogr.OFTString))
        feature = ogr.Feature(layer.GetLayerDefn())
        feature.SetField("name", "demo")
        feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT (1 1)"))
        assert layer.CreateFeature(feature) == 0
        layer = feature = shape = None


class Scenario:
    def __init__(self, panel, version, mark, root):
        self.panel = panel
        self.browser = panel.browser
        self.connection = panel._connection
        self.version = version
        self.mark = mark
        self.root = root
        self.step = 0

    def tick(self):
        browser = self.browser
        proxies = browser.proxies
        if self.step == 0:
            self.connection.call("files", {"path": "../"}, self._path_test)
            self.step = 1
        if self.step == 2:
            if browser.directory != "测试数据":
                return False
            data_items = {browser.tree.topLevelItem(i).text(0): browser.tree.topLevelItem(i)
                          for i in range(browser.tree.topLevelItemCount())}
            mime = browser.tree.mimeData([data_items["影像.tif"]])
            uri, = QgsMimeDataUtils.decodeUriList(mime)
            assert uri.layerType == "custom" and uri.providerKey == "remoteqgis"
            assert browser.drop_handler.handleCustomUriCanvasDrop(uri, self.panel.iface.mapCanvas())
            for name in ("points.gpkg", "points.shp"):
                data_items[name].setSelected(True)
            browser.import_selected()
            self.step = 3
        if self.step == 3 and len(proxies.layers) == 3:
            count = 0
            for key, local in proxies.layers.items():
                descriptor = proxies.descriptors[key]
                layer = proxies.project.mapLayer(local)
                assert layer.isValid()
                if descriptor["kind"] == "vector":
                    assert isinstance(layer, QgsVectorLayer) and layer.featureCount() == 1
                    feature = next(layer.getFeatures())
                    assert feature["name"] == "demo" and feature.geometry().asWkt() == "Point (1 1)"
                    count += 1
            assert count == 2
            self.mark("workspace-imported")
            self.step = 4
        if self.step == 4 and (self.root / "workspace-host-verified").exists():
            if self.version < 3:
                return True
            raster, = [desc for desc in proxies.descriptors.values() if desc["kind"] == "raster"]
            self.raster_id = raster["id"]
            self.tile_version = raster["tile_version"]
            self.connection.call("pixel", {"id": self.raster_id, "x": 0.2, "y": 0.2}, self._pixel)
            self.step = 5
        if self.step == 6:
            raster = proxies.descriptors[self.raster_id]
            if raster["tile_version"] == self.tile_version:
                return False
            layer = proxies.project.mapLayer(proxies.layers[self.raster_id])
            assert isinstance(layer, QgsRasterLayer) and layer.isValid(), layer
            # Native provider work exercises its network preprocessor and PNG decode.
            block = layer.dataProvider().block(1, QgsRectangle(24000, 24000, 50000, 50000), 16, 16)
            assert block.isValid() and not block.isEmpty()
            color = block.color(8, 8)
            assert (color >> 24) == 255 and 120 <= (color & 255) <= 135, hex(color)
            self.mark("raster-verified")
            self.step = 7
        return self.step == 7

    def _path_test(self, result, error):
        assert error and result is None
        self.browser.browse("测试数据")
        self.step = 2

    def _pixel(self, data, error):
        assert not error and data["values"] == [1000, 2000, 3000], (data, error)
        self.connection.call("render", {"id": self.raster_id, "bands": [2],
                                        "stretch": [[0, 4000]], "resample": "near"}, self._rendered)

    def _rendered(self, data, error):
        assert not error, error
        self.browser.proxies.refresh()
        self.step = 6
