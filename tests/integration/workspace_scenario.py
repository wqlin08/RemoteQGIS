# SPDX-License-Identifier: GPL-3.0-or-later

"""Synthetic, shared-directory fixtures for the native two-session scenario."""

from uuid import uuid4

import numpy as np
from osgeo import gdal, ogr, osr

from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsGeometry, QgsMimeDataUtils, QgsRasterLayer,
    QgsPalettedRasterRenderer, QgsRectangle, QgsVectorLayer,
)
from qgis.PyQt.QtCore import QPoint, Qt, QTimer, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtTest import QTest
from qgis.PyQt.QtWidgets import QApplication


def layer_in_canvas(canvas, identifier):
    return any(layer.id() == identifier for layer in canvas.layers())


def canvas_center_color(canvas):
    if canvas.isDrawing():
        return None
    image = canvas.viewport().grab().toImage()
    return image.pixelColor(image.width() // 2, image.height() // 2)


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
    raster.SetGeoTransform((99, 4 / size, 0, 31, 0, -4 / size))
    raster.SetProjection(srs.ExportToWkt())
    for i in range(1, 4):
        band = raster.GetRasterBand(i)
        band.Fill(i * 1000)
        band.WriteArray(np.full((size, size // 4), 4000, dtype=np.uint16))
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
            canvas = self.panel.iface.mapCanvas()
            canvas.setDestinationCrs(QgsCoordinateReferenceSystem("EPSG:4326"))
            canvas.setExtent(QgsRectangle(-2, -2, 2, 2))
            assert browser.drop_handler.handleCustomUriCanvasDrop(uri, canvas)
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
            canvas = self.panel.iface.mapCanvas()
            to_image = QgsCoordinateTransform(canvas.mapSettings().destinationCrs(),
                                              QgsCoordinateReferenceSystem("EPSG:4326"), proxies.project)
            center = to_image.transform(canvas.extent().center())
            if not 99 < center.x() < 103:
                self._focus_checks = getattr(self, "_focus_checks", 0) + 1
                assert self._focus_checks < 30, ("Canvas did not zoom to the imported raster", center,
                                                 canvas.extent(), canvas.mapSettings().destinationCrs().authid(),
                                                 browser.message.text())
                return False
            assert layer_in_canvas(canvas, proxies.layers[self.raster_id]), "Raster is missing from the canvas"
            color = canvas_center_color(canvas)
            if color is None or color.lightness() >= 240:
                self._import_checks = getattr(self, "_import_checks", 0) + 1
                assert self._import_checks < 50, ("Imported raster was blank before changing its style", color)
                return False
            self.connection.call("pixel", {"id": self.raster_id, "x": 101.2, "y": 29.2}, self._pixel)
            self.step = 5
        if self.step == 6:
            raster = proxies.descriptors[self.raster_id]
            if raster["tile_version"] == self.tile_version:
                return False
            layer = proxies.project.mapLayer(proxies.layers[self.raster_id])
            assert isinstance(layer, QgsRasterLayer) and layer.isValid(), layer
            # Native provider work exercises its network preprocessor and PNG decode.
            if raster.get("source_version"):
                assert layer.providerType() == "gdal" and layer.bandCount() == 3
                block = layer.dataProvider().block(2, QgsRectangle(100.5, 28.5, 101.5, 29.5), 16, 16)
                assert block.isValid() and block.value(8, 8) == 2000
            else:
                block = layer.dataProvider().block(1, QgsRectangle(11240000, 3350000, 11260000, 3370000), 16, 16)
                assert block.isValid() and not block.isEmpty()
                color = block.color(8, 8)
                assert (color >> 24) == 255 and 120 <= (color & 255) <= 135, hex(color)
            self.step = 61
        if self.step == 61:
            color = canvas_center_color(self.panel.iface.mapCanvas())
            if color is None or not 120 <= color.red() <= 135:
                self._canvas_checks = getattr(self, "_canvas_checks", 0) + 1
                assert self._canvas_checks < 50, ("Raster did not render on the canvas", color)
                return False
            raster = proxies.descriptors[self.raster_id]
            if raster.get("source_version"):
                layer = proxies._local_layers()[self.raster_id]
                palette = [QgsPalettedRasterRenderer.Class(2000, QColor("#d02c4f"), "示例类别")]
                layer.setRenderer(QgsPalettedRasterRenderer(layer.dataProvider(), 2, palette))
                layer.triggerRepaint()
                self.step = 62
            else:
                self.mark("raster-verified")
                self.step = 7
        if self.step == 62 and (self.root / "raster-style-host-verified").exists():
            color = canvas_center_color(self.panel.iface.mapCanvas())
            if color is None:
                return False
            assert color.name() == "#d02c4f", ("Native category color is missing", color)
            self.mark("raster-verified")
            self.step = 7
        if self.step == 7:
            if self.version < 4:
                return True
            vector_key, = [key for key, desc in proxies.descriptors.items() if desc["path"].endswith(".gpkg")]
            layer = proxies.project.mapLayer(proxies.layers[vector_key])
            layer.setName("远程点样式")
            layer.setOpacity(0.55)
            layer.renderer().symbol().setColor(QColor("#a33b87"))
            item = proxies.group.findLayer(layer.id())
            group = proxies.group.insertGroup(0, "远程数据组")
            group.addChildNode(item.clone())
            item.parent().removeChildNode(item)
            self.panel.iface.mapCanvas().setRotation(12)
            self.step = 8
        if self.step == 8 and (self.root / "project-layout-verified").exists():
            self.step = 9
            self.connection.call("project", {"action": "save_as", "path": "测试数据/远程.qgz"},
                                 self._project_saved)
        if self.step == 10 and not proxies.descriptors:
            self.step = 11
            self.connection.call("project", {"action": "open", "path": "测试数据/远程.qgz",
                                             "discard": True}, self._project_reopened)
        if self.step == 12 and len(proxies.layers) == 3:
            layer = next(l for l in proxies._local_layers().values() if l.name() == "远程点样式")
            assert abs(layer.opacity() - 0.55) < 1e-6
            assert layer.renderer().symbol().color().name() == "#a33b87"
            assert proxies.group.findLayer(layer.id()).parent().name() == "远程数据组"
            if proxies.descriptors[self.raster_id].get("source_version"):
                raster = proxies._local_layers()[self.raster_id]
                assert isinstance(raster.renderer(), QgsPalettedRasterRenderer)
                assert raster.renderer().classes()[0].color.name() == "#d02c4f"
            self.mark("project-verified")
            self.step = 13
        if self.step == 13:
            if self.version < 5:
                return True
            self.shp = self._key("points.shp")
            self.gpkg = self._key("points.gpkg")
            self.enabled = set()
            self._rollback_requested = False
            self._rollback_verified = False
            self.step = 14
            for key in (self.shp, self.gpkg):
                self.connection.call("enable_edit", {"id": key},
                                     lambda doc, error, k=key: self._enabled(k, doc, error))
        if self.step == 14 and len(self.enabled) == 2 and not proxies.pending:
            if not self._rollback_requested:
                self._rollback_requested = True
                descriptor = proxies.descriptors[self.gpkg]
                base = proxies.edits.records[self.gpkg][1]
                self.rollback_revision = base["revision"]
                self.connection.call("edit", {
                    "id": self.gpkg, "dataset": [descriptor["path"], descriptor["table"]],
                    "base": base["revision"], "transaction": str(uuid4()),
                    "fields": base["fields"] + [["test_rev", 2, 0, 0, 0]],
                    "delete": [base["features"][0]["id"]], "update": [],
                    "add": [{"id": str(uuid4()), "geometry": "invalid-wkb", "values": ["bad", 1]}],
                }, self._invalid_edit)
                return False
            if not self._rollback_verified:
                return False
            layer = proxies._local_layers()[self.shp]
            assert layer.startEditing()
            old = next(layer.getFeatures())
            assert layer.deleteFeature(old.id())
            assert layer.addAttribute(QgsField("score", QVariant.Int))
            item = QgsFeature(layer.fields())
            item["name"] = "shapefile"
            item["score"] = 7
            item.setGeometry(QgsGeometry.fromWkt("Point (2 2)"))
            assert layer.addFeature(item)
            assert layer.commitChanges(), layer.commitErrors()
            self.step = 15
        if self.step == 15:
            layer = proxies._local_layers()[self.shp]
            if proxies.edits.working_pending(self.shp) or layer.readOnly():
                return False
            feature = next(layer.getFeatures())
            if feature["name"] != "shapefile" or feature["score"] != 7:
                return False
            self.mark("shapefile-verified")
            layer = proxies._local_layers()[self.gpkg]
            assert layer.startEditing()
            original = next(layer.getFeatures())
            self.gpkg_uuid = original["rq_uuid"]
            layer.beginEditCommand("Move point")
            assert layer.changeGeometry(original.id(), QgsGeometry.fromWkt("Point (0 0)"))
            assert layer.changeAttributeValue(original.id(), layer.fields().indexFromName("name"), "offline")
            layer.endEditCommand()
            layer.undoStack().undo()
            assert next(layer.getFeatures())["name"] == "demo"
            layer.undoStack().redo()
            assert next(layer.getFeatures())["name"] == "offline"
            self.mark("edit-disconnect")
            self.step = 16
        if self.step == 16 and not self.connection.ready:
            layer = proxies._local_layers()[self.gpkg]
            assert layer.commitChanges(), layer.commitErrors()
            path = proxies.edits.path(proxies.descriptors[self.gpkg])
            assert path.exists(), "Pending edit was not written before local commit"
            self.saved_patch = proxies.edits.recovery.read(path)["patch"]
            self.mark("edit-journal-written")
            self.step = 17
        if self.step == 17 and self.connection.ready and self.gpkg in proxies.edits.records:
            if proxies.edits.working_pending(self.gpkg):
                return False
            layer = proxies._local_layers()[self.gpkg]
            feature = next(layer.getFeatures())
            if feature["name"] != "offline":
                return False
            assert feature["rq_uuid"] == self.gpkg_uuid
            assert feature.geometry().asWkt() == "Point (0 0)"
            self.step = 18
            self.connection.call("edit", dict(self.saved_patch, id=self.gpkg), self._replayed)
        if self.step == 19 and not proxies.pending:
            layer = next((layer for layer in proxies._local_layers().values() if layer.name() == "新建面"), None)
            if layer is None:
                return False
            self.new_id = layer.customProperty("remoteqgis/id")
            self.new_local = layer.id()
            self._draw_polygon(layer)
        if self.step == 20 and not proxies.edits.working_pending(self.new_id):
            layer = proxies._local_layers()[self.new_id]
            assert layer.id() == self.new_local
            assert self.panel.iface.activeLayer() == layer
            assert layer.featureCount() == 1 and next(layer.getFeatures()).geometry().area() > 0
            canvas = self.panel.iface.mapCanvas()
            assert layer_in_canvas(canvas, layer.id()), "Saved polygon is missing from the canvas"
            color = canvas_center_color(canvas)
            if color is None or color.name() != self.polygon_color:
                self._polygon_checks = getattr(self, "_polygon_checks", 0) + 1
                assert self._polygon_checks < 50, ("Saved polygon did not render on the canvas", color)
                return False
            self.mark("vector-verified")
            self.step = 21
        if self.step == 21 and (self.root / "vector-host-verified").exists():
            self.step = 211
            if hasattr(self.panel, "_toolbox"):
                self._run_analysis()
            if hasattr(proxies, "online"):
                self._prepare_online()
            else:
                self.step = 22
                self.connection.call("project", {"action": "save"}, self._final_save)
        if self.step == 210:
            self._prepare_online()
        if self.step in (212, 213, 214):
            self._online_step()
        return self.step == 24

    def _run_analysis(self):
        from processing.core.Processing import Processing

        registry = QgsApplication.processingRegistry()
        provider = registry.providerById("remoteqgis")
        assert len(provider.algorithms()) >= 35
        layers = self.browser.proxies._local_layers()
        vector = layers[self.gpkg]
        raster = layers[self.raster_id]
        result = Processing.runAlgorithm("remoteqgis:native_buffer", {
            "INPUT": vector.id(), "DISTANCE": 0.1, "REMOTE_OUTPUT": "测试数据/缓冲.gpkg",
        })
        assert result["REMOTE_OUTPUT"] == "测试数据/缓冲.gpkg", result
        assert (self.root / "shared" / result["REMOTE_OUTPUT"]).is_file()
        result = Processing.runAlgorithm("remoteqgis:native_rastercalc", {
            "LAYERS": [raster.id()], "EXPRESSION": f'"{raster.name()}@2" + 5',
            "REMOTE_OUTPUT": "测试数据/计算.tif",
        })
        assert result["REMOTE_OUTPUT"] == "测试数据/计算.tif", result
        ds = gdal.Open(str(self.root / "shared" / result["REMOTE_OUTPUT"]))
        assert ds.GetRasterBand(1).ReadAsArray(ds.RasterXSize // 2, ds.RasterYSize // 2, 1, 1).item() == 2005
        ds = None
        result = Processing.runAlgorithm("remoteqgis:gdal_cliprasterbyextent", {
            "INPUT": raster.id(), "PROJWIN": "100,102,28,30 [EPSG:4326]",
            "REMOTE_OUTPUT": "测试数据/裁剪.tif",
        })
        assert result["REMOTE_OUTPUT"] == "测试数据/裁剪.tif", result
        ds = gdal.Open(str(self.root / "shared" / result["REMOTE_OUTPUT"]))
        assert ds.RasterXSize < 4096 and ds.RasterCount == 3
        ds = None
        self.mark("processing-verified")

    def _prepare_online(self):
        proxies = self.browser.proxies
        project = proxies.project
        if hasattr(self.panel, "_toolbox"):
            outputs = {"测试数据/计算.tif", "测试数据/裁剪.tif", "测试数据/缓冲.gpkg"}
            loaded = {desc.get("path"): proxies.layers[key] for key, desc in proxies.descriptors.items()
                      if key in proxies.layers and desc.get("path") in outputs}
            if loaded.keys() != outputs:
                self.step = 210
                return
            # Exercise online drawing separately from simultaneous full raster-calculation previews.
            for path in outputs - {"测试数据/缓冲.gpkg"}:
                proxies.group.findLayer(loaded[path]).setItemVisibilityChecked(False)
        self.local_test = QgsVectorLayer("Point?crs=EPSG:4326", "本地拒绝测试", "memory")
        source = "type=xyz&url=https://example.invalid/remoteqgis-fixture/{z}/{x}/{y}.png&zmin=0&zmax=19"
        self.online_test = QgsRasterLayer(source, "在线测试瓦片", "wms")
        assert self.online_test.isValid()
        project.addMapLayer(self.local_test)
        project.addMapLayer(self.online_test)
        self.step = 212

    def _online_step(self):
        proxies = self.browser.proxies
        root = proxies.project.layerTreeRoot()
        if self.step == 212:
            keys = {self.local_test.id(), self.online_test.id()}
            if not keys <= proxies.online.positions.keys():
                return
            assert not any(desc["kind"] == "online" for desc in proxies.descriptors.values())
            self._drop_on_group(root.findLayer(self.local_test.id()))
            self.step = 213
        if self.step == 213:
            restored = root.findLayer(self.local_test.id())
            if restored.parent() == proxies.group:
                return
            warning = self.browser._layer_warning
            assert warning is not None and "本地数据" in warning.text(), self.browser.message.text()
            warning.accept()
            self._drop_on_group(root.findLayer(self.online_test.id()))
            self.mark("online-dragged")
            self.step = 214
        if self.step == 214 and (self.root / "online-host-verified").exists():
            handles = [key for key, desc in proxies.descriptors.items() if desc["kind"] == "online"]
            if not handles:
                return
            key, = handles
            assert proxies.layers[key] == self.online_test.id()
            canvas = self.panel.iface.mapCanvas()
            color = canvas_center_color(canvas)
            if color is None or color.name() != "#2468ac":
                self._online_checks = getattr(self, "_online_checks", 0) + 1
                assert self._online_checks < 80, (color, canvas.extent(),
                                                   [layer.name() for layer in canvas.layers()],
                                                   self.browser.message.text())
                return
            self.mark("online-client-verified")
            self.step = 22
            self.connection.call("project", {"action": "save"}, self._final_save)

    def _drop_on_group(self, node):
        identifier = node.layerId()
        self.browser.proxies.group.insertChildNode(0, node.clone())
        node.parent().removeChildNode(node)
        actual = self.browser.proxies.project.layerTreeRoot().findLayer(identifier)
        assert actual is not None and actual.parent() == self.browser.proxies.group

    def _draw_polygon(self, layer):
        self.step = 191
        iface = self.panel.iface
        canvas = iface.mapCanvas()
        canvas.setRotation(0)
        transform = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"),
                                          canvas.mapSettings().destinationCrs(), self.browser.proxies.project)
        canvas.setExtent(transform.transformBoundingBox(QgsRectangle(100, 28, 102, 30)))
        canvas.refresh()
        assert iface.setActiveLayer(layer) and layer.startEditing()
        self.polygon_color = layer.renderer().symbol().color().name()
        add_feature = iface.actionAddFeature()
        assert add_feature.isEnabled()
        add_feature.trigger()
        viewport = canvas.viewport()
        center = viewport.rect().center()
        modal = []

        def dismiss_form():
            widget = QApplication.activeModalWidget()
            if widget is not None:
                modal.append(widget.windowTitle())
                widget.reject()

        for offset in (QPoint(-30, -30), QPoint(30, -30), QPoint(30, 30), QPoint(-30, 30)):
            QTest.mouseClick(viewport, Qt.LeftButton, pos=center + offset)
        QTimer.singleShot(150, dismiss_form)
        QTest.mouseClick(viewport, Qt.RightButton, pos=center)
        QTest.qWait(180)
        assert not modal, f"A new polygon with no user fields opened a form: {modal}"
        assert layer.featureCount() == 1, "Native right-click did not finish the polygon"
        assert layer.commitChanges(), layer.commitErrors()
        self.step = 20


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

    def _project_saved(self, _, error):
        assert not error, error
        assert (self.root / "shared" / "测试数据" / "远程.qgz").exists()
        self.connection.call("project", {"action": "close", "discard": True}, self._project_closed)

    def _project_closed(self, _, error):
        assert not error, error
        self.browser.proxies.refresh()
        self.step = 10

    def _project_reopened(self, _, error):
        assert not error, error
        self.browser.proxies.refresh()
        self.step = 12

    def _key(self, suffix):
        return next(key for key, desc in self.browser.proxies.descriptors.items() if desc["path"].endswith(suffix))

    def _enabled(self, key, doc, error):
        assert not error, error
        assert doc["editable"]
        self.browser.proxies.install_vector(key, doc)
        self.enabled.add(key)

    def _replayed(self, doc, error):
        assert not error and len(doc["features"]) == 1, (doc, error)
        self.connection.call("create_vector", {"path": "测试数据/新建面.gpkg", "crs": "EPSG:4326",
                                               "geometry": "Polygon", "fields": []},
                             self._created)

    def _created(self, _, error):
        assert not error, error
        self.browser.proxies.refresh()
        self.step = 19

    def _final_save(self, _, error):
        assert not error, error
        self.connection.call("project", {"action": "create", "path": "测试数据/空项目.qgz",
                                         "discard": True}, self._final_project)

    def _final_project(self, _, error):
        assert not error, error
        assert (self.root / "shared" / "测试数据" / "空项目.qgz").exists()
        self.step = 24

    def _invalid_edit(self, result, error):
        assert result is None and error, (result, error)
        self.connection.call("vector", {"id": self.gpkg}, self._verify_rollback)

    def _verify_rollback(self, result, error):
        assert not error and result["revision"] == self.rollback_revision, (result, error)
        self._rollback_verified = True
