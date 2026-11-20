# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsFeature, QgsGeometry, QgsMemoryProviderUtils, QgsProject,
    QgsRectangle, QgsVectorLayer, QgsWkbTypes,
)

from ..common.vector import fields_from, geometry_from, unpack


def crs_from(wkt: str, authority: str = ""):
    crs = QgsCoordinateReferenceSystem(authority)
    if not crs.isValid() and wkt:
        crs.createFromWkt(wkt)
    return crs


def vector_proxy(name: str, document: dict):
    fields = fields_from(document["fields"])
    layer = QgsMemoryProviderUtils.createMemoryLayer(
        name, fields, QgsWkbTypes.parseType(document["geometry"]), crs_from(document["crs"])
    )
    features = []
    for record in document["features"]:
        feature = QgsFeature(fields)
        feature.setGeometry(geometry_from(record["geometry"]))
        feature.setAttributes([unpack(value) for value in record["values"]])
        features.append(feature)
    ok, _ = layer.dataProvider().addFeatures(features)
    if not ok:
        raise ValueError("无法建立矢量代理。")
    layer.updateExtents()
    layer.setReadOnly(True)
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    return layer


def footprint(descriptor: dict):
    layer = QgsVectorLayer("Polygon", descriptor["name"], "memory")
    layer.setCrs(crs_from(descriptor["wkt"], descriptor["crs"]))
    feature = QgsFeature()
    feature.setGeometry(QgsGeometry.fromRect(QgsRectangle(*descriptor["bounds"])))
    layer.dataProvider().addFeatures([feature])
    layer.updateExtents()
    layer.setReadOnly(True)
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    return layer


class Proxies:
    def __init__(self, connection, changed, failure):
        self.connection = connection
        self.changed = changed
        self.failure = failure
        self.project = QgsProject.instance()
        self.layers = {}
        self.descriptors = {}
        self.pending = set()
        self.group = None
        self._loading = False
        self._refresh_pending = False
        self.origin = ""
        connection.workspace_changed.connect(self.refresh)

    def refresh(self, _revision=None):
        if self._loading:
            self._refresh_pending = True
            return
        if self.connection.origin != self.origin:
            self.clear()
            self.origin = self.connection.origin
        self._loading = True
        self.connection.call("state", {}, self._state)

    def _state(self, state, error):
        self._loading = False
        if error:
            if self.connection.ready:
                self.failure(error)
        elif self.connection.origin == self.origin:
            self.apply(state)
        if self._refresh_pending:
            self._refresh_pending = False
            if self.connection.ready:
                self.refresh()

    def apply(self, state):
        self.descriptors = {layer["id"]: layer for layer in state["layers"]}
        for identifier, local in tuple(self.layers.items()):
            if identifier not in self.descriptors or not self.project.mapLayer(local):
                self.layers.pop(identifier)
                self.project.removeMapLayer(local)
        for identifier, descriptor in self.descriptors.items():
            if identifier in self.layers:
                self.project.mapLayer(self.layers[identifier]).setName(descriptor["name"])
            elif identifier not in self.pending:
                if descriptor["kind"] == "raster":
                    self._add(identifier, footprint(descriptor))
                else:
                    self.pending.add(identifier)
                    self.connection.call("vector", {"id": identifier},
                                         lambda data, error, key=identifier: self._vector(key, data, error))
        self.changed()

    def _vector(self, identifier, data, error):
        self.pending.discard(identifier)
        descriptor = self.descriptors.get(identifier)
        if descriptor is None:
            return
        if error:
            if self.connection.ready:
                self.failure(error)
            return
        try:
            self._add(identifier, vector_proxy(descriptor["name"], data))
        except (TypeError, ValueError, RuntimeError) as exc:
            self.failure(str(exc))

    def _add(self, identifier, layer):
        if self.group is None:
            self.group = self.project.layerTreeRoot().addGroup("RemoteQGIS")
            self.group.setCustomProperty("remoteqgis", 1)
        self.project.addMapLayer(layer, False)
        node = self.group.addLayer(layer)
        node.setItemVisibilityChecked(self.descriptors[identifier]["visible"])
        layer.setCustomProperty("remoteqgis/id", identifier)
        self.layers[identifier] = layer.id()
        self.changed()

    def clear(self):
        self.project.removeMapLayers(list(self.layers.values()))
        if self.group is not None:
            try:
                if self.group.parent():
                    self.group.parent().removeChildNode(self.group)
            except RuntimeError:
                pass
        self.group = None
        self.layers.clear()
        self.descriptors.clear()
        self.pending.clear()
