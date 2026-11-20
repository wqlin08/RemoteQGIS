# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsMemoryProviderUtils, QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)

from qgis.PyQt.QtCore import QTimer, QVariant

from .rasters import RasterTiles
from .edits import ClientEdits
from ..common.edits import IDENTITY
from ..common.layers import apply_style, apply_tree, opacity, read_style, read_tree, set_opacity, validate_tree
from ..common.vector import fields_from, geometry_from, unpack


def crs_from(wkt: str, authority: str = ""):
    crs = QgsCoordinateReferenceSystem(authority)
    if not crs.isValid() and wkt:
        crs.createFromWkt(wkt)
    return crs


def vector_proxy(name: str, document: dict):
    fields = fields_from(document["fields"])
    if document.get("editable"):
        fields.append(QgsField(IDENTITY, QVariant.String, len=36))
    layer = QgsMemoryProviderUtils.createMemoryLayer(
        name, fields, QgsWkbTypes.parseType(document["geometry"]), crs_from(document["crs"])
    )
    features = []
    for record in document["features"]:
        feature = QgsFeature(fields)
        feature.setGeometry(geometry_from(record["geometry"]))
        attrs = [unpack(value) for value in record["values"]]
        if document.get("editable"):
            attrs.append(record["id"])
        feature.setAttributes(attrs)
        features.append(feature)
    ok, _ = layer.dataProvider().addFeatures(features)
    if not ok:
        raise ValueError("无法建立矢量代理。")
    layer.updateExtents()
    layer.setReadOnly(True)
    layer.setCustomProperty("skipMemoryLayersCheck", 1)
    return layer


class Proxies:
    def __init__(self, connection, canvas, changed, failure):
        self.connection, self.canvas = connection, canvas
        self.changed, self.failure = changed, failure
        self.tiles = RasterTiles(connection, canvas)
        self.edits = ClientEdits(self)
        self.project = QgsProject.instance()
        self.layers, self.descriptors = {}, {}
        self.pending = set()
        self._unavailable = set()
        self.group = None
        self.tree = []
        self.session, self.revision, self.origin = "", -1, ""
        self.remote_project = {}
        self._baseline = None
        self._loading = self._refresh_pending = self._sending = False
        self._workspace_open = True
        self._view = QTimer(canvas, interval=500, singleShot=True)
        self._view.timeout.connect(self._send_view)
        self._timer = QTimer(canvas, interval=350)
        self._timer.timeout.connect(self._sync_local)
        self._timer.start()
        canvas.extentsChanged.connect(self._schedule_view)
        canvas.rotationChanged.connect(self._schedule_view)
        self.project.cleared.connect(self._local_project_cleared)
        connection.workspace_changed.connect(self.refresh)
        connection.status_changed.connect(self._status)

    def _status(self, state, _detail):
        if state == "connected":
            self.revision = -1
            for key in self.edits.records:
                if self.edits.working_pending(key):
                    self.edits.schedule(key)

    def refresh(self, revision=None):
        if not self._workspace_open:
            return
        if revision == self.revision:
            return
        if self._loading:
            self._refresh_pending = True
            return
        if self.connection.origin != self.origin:
            if self.edits.has_unrecorded():
                self.failure("请先提交或撤销本地尚未保存的编辑。")
                return
            self.clear()
            self.origin = self.connection.origin
        if not self.connection.ready:
            return
        self._loading = True
        self.connection.call("state", {"session": self.session, "since": self.revision}, self._state)

    def _state(self, state, error):
        self._loading = False
        if error:
            if self.connection.ready:
                self.failure(error)
        elif self.connection.ready and self.connection.origin == self.origin:
            self.apply(state)
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def apply(self, state):
        if state["session"] == self.session and state["revision"] < self.revision:
            return
        new_session = state["session"] != self.session
        if new_session:
            if self.edits.has_unrecorded():
                self.failure("工作站项目已重连。请先保存或撤销本地编辑后刷新。")
                return
            self.clear()
        self.session, self.revision = state["session"], state["revision"]
        changed_project = state["project"].get("path") != self.remote_project.get("path")
        self.remote_project = state["project"]
        if "tree" in state:
            self.tree = state["tree"]
        previous = self.descriptors.copy()
        if state["full"]:
            self.descriptors.clear()
        for key in state["removed"]:
            self.descriptors.pop(key, None)
        for descriptor in state["layers"]:
            self.descriptors[descriptor["id"]] = descriptor
        self._unavailable.intersection_update(self.descriptors)
        self.tiles.descriptors = [desc for desc in self.descriptors.values() if desc["kind"] == "raster"]
        for identifier, local in tuple(self.layers.items()):
            if identifier not in self.descriptors or not self.project.mapLayer(local):
                self.edits.detach(identifier)
                self.layers.pop(identifier)
                self.project.removeMapLayer(local)
        for identifier, descriptor in self.descriptors.items():
            if identifier in self.layers:
                layer = self.project.mapLayer(self.layers[identifier])
                if descriptor.get("tile_version") and descriptor.get("tile_version") != previous.get(identifier, {}).get("tile_version"):
                    layer.setDataSource(self.tiles.uri(descriptor), descriptor["name"], "wms")
                self._style(layer, descriptor)
                before = previous.get(identifier, {})
                if (descriptor["kind"] == "vector" and descriptor.get("vector_version") != before.get("vector_version")
                        and not layer.isEditable() and not self.edits.working_pending(identifier)
                        and identifier not in self.pending and identifier not in self.edits.in_flight):
                    self._request_vector(identifier)
            elif identifier not in self.pending:
                if descriptor["kind"] == "raster":
                    try:
                        self._add(identifier, self.tiles.layer(descriptor))
                    except ValueError as exc:
                        self._unavailable.add(identifier)
                        self.failure(str(exc))
                else:
                    self._request_vector(identifier)
        self._layout()
        if (new_session or changed_project) and state.get("view"):
            self._initial_view(state["view"])
        self.changed()

    def _request_vector(self, key):
        self.pending.add(key)
        self.connection.call("vector", {"id": key},
                             lambda data, error, session=self.session: self._vector(key, session, data, error))

    def _vector(self, identifier, session, data, error):
        if session != self.session:
            return
        self.pending.discard(identifier)
        descriptor = self.descriptors.get(identifier)
        if descriptor is None:
            return
        if error:
            self._unavailable.add(identifier)
            if self.connection.ready:
                self.failure(error)
            return
        try:
            self.install_vector(identifier, data)
        except (TypeError, ValueError, RuntimeError) as exc:
            self._unavailable.add(identifier)
            self.failure(str(exc))

    def install_vector(self, identifier, document):
        descriptor = self.descriptors[identifier]
        working, record = self.edits.restore(descriptor, document)
        layer = vector_proxy(descriptor["name"], working)
        old = self.layers.get(identifier)
        self.edits.detach(identifier)
        self._add(identifier, layer)
        self._layout()
        if old is not None:
            self.project.removeMapLayer(old)
        if working.get("editable"):
            self.edits.attach(identifier, layer, document, record)
        self._layout()
        self.changed()

    @staticmethod
    def _style(layer, descriptor):
        layer.setName(descriptor["name"])
        set_opacity(layer, descriptor["opacity"])
        apply_style(layer, descriptor.get("style", ""))

    def _add(self, identifier, layer):
        self._unavailable.discard(identifier)
        layer.setCustomProperty("remoteqgis/id", identifier)
        self._style(layer, self.descriptors[identifier])
        self.project.addMapLayer(layer, False)
        self.layers[identifier] = layer.id()

    def _local_layers(self):
        return {key: layer for key, local in self.layers.items()
                if (layer := self.project.mapLayer(local)) is not None}

    def _layout(self):
        layers = self._local_layers()
        if not layers and not self.tree and self.group is None:
            return
        if self.group is None:
            self.group = self.project.layerTreeRoot().addGroup("RemoteQGIS")
            self.group.setCustomProperty("remoteqgis", 1)

        def partial(nodes):
            return [dict(node, children=partial(node["children"])) if "group" in node else node
                    for node in nodes if "group" in node or node.get("id") in layers]

        apply_tree(self.group, partial(self.tree), layers)
        self._baseline = self._read_local()

    def _read_local(self):
        layers = self._local_layers()
        try:
            tree = read_tree(self.group, {layer.id(): key for key, layer in layers.items()}) if self.group else []
        except RuntimeError:
            self.group = None
            tree = []
        settings = {key: {"name": layer.name(), "opacity": opacity(layer), "style": read_style(layer)}
                    for key, layer in layers.items()}
        return tree, settings

    def _sync_local(self):
        if (not self.connection.ready or self._sending or self._loading or self.pending
                or self._baseline is None or self.revision < 0 or self._unavailable):
            return
        tree, settings = self._read_local()
        if (tree, settings) == self._baseline:
            return
        included, _ = validate_tree(tree, self.descriptors)
        if self._local_layers().keys() - included:
            self.failure("请在 RemoteQGIS 分组内排列远端图层。")
            return
        for key in self.descriptors.keys() - included:
            if self.edits.working_pending(key):
                self.failure("还有编辑等待确认，已保留远端图层。")
                self._request_vector(key)
                return
        changed = {key: value for key, value in settings.items()
                   if key in included and value != self._baseline[1].get(key)}
        self._sending = True
        self.connection.call(
            "layout", {"session": self.session, "base": self.revision, "tree": tree, "settings": changed},
            self._layout_saved,
        )

    def _layout_saved(self, result, error):
        self._sending = False
        if not self.connection.ready:
            return
        if error:
            self.failure(error)
            self.revision = -1
            self.refresh()
        else:
            self.apply(result)

    def _schedule_view(self, *args):
        if self.connection.ready and self.revision >= 0:
            self._view.start()

    def _initial_view(self, view):
        crs = self.canvas.mapSettings().destinationCrs()
        source = crs_from("", view["crs"])
        try:
            extent = QgsCoordinateTransform(source, crs, self.project).transformBoundingBox(QgsRectangle(*view["extent"]))
            if not extent.isEmpty():
                self.canvas.setExtent(extent)
                self.canvas.setRotation(view["rotation"])
                self.canvas.refresh()
        except Exception:
            pass
        self._view.stop()

    def _send_view(self):
        rect = self.canvas.extent()
        if self.connection.ready and not rect.isEmpty():
            crs = self.canvas.mapSettings().destinationCrs()
            self.connection.call("view", {
                "crs": crs.authid() or crs.toWkt(),
                "extent": [rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum()],
                "rotation": self.canvas.rotation(),
            }, lambda *_: None)

    def clear(self):
        self._baseline = None
        for key in tuple(self.edits.records):
            self.edits.detach(key)
        self.project.removeMapLayers(list(self.layers.values()))
        if self.group is not None:
            try:
                if self.group.parent():
                    self.group.parent().removeChildNode(self.group)
            except RuntimeError:
                pass
        self.group = None
        self.session, self.revision = "", -1
        self.tree = []
        self.remote_project = {}
        self.layers.clear()
        self.descriptors.clear()
        self.tiles.descriptors = []
        self.pending.clear()
        self._unavailable.clear()

    def _local_project_cleared(self):
        # A local QGIS project switch removes proxy nodes as a side effect.
        self.group = None
        self._baseline = None
        for key in tuple(self.edits.records):
            self.edits.detach(key)
        self.layers.clear()
        self.revision = -1
        self._view.stop()
        QTimer.singleShot(100, self.refresh)

    def close(self):
        self._workspace_open = False
        self.project.cleared.disconnect(self._local_project_cleared)
        self._timer.stop()
        self._view.stop()
        self._timer.deleteLater()
        self._view.deleteLater()
        self.canvas.extentsChanged.disconnect(self._schedule_view)
        self.canvas.rotationChanged.disconnect(self._schedule_view)
        self.connection.workspace_changed.disconnect(self.refresh)
        self.connection.status_changed.disconnect(self._status)
        self.tiles.close()
        self.edits.close()
        self.clear()
