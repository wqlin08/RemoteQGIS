# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import (
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsFeature, QgsField, QgsMemoryProviderUtils, QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)

from qgis.PyQt.QtCore import QTimer, QVariant

from .rasters import RasterTiles
from .online import OnlineLayers
from .edits import ClientEdits
from ..common.edits import IDENTITY
from ..common.online import ONLINE_PROPERTY
from ..common.layers import (
    apply_properties, apply_style, apply_tree, opacity, read_properties, read_style, read_tree,
    retain_unloaded, set_opacity, validate_tree,
)
from ..common.vector import field_data, fields_from, geometry_from, unpack


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
    def __init__(self, connection, iface, changed, failure, rejected, *, read_only=False):
        self.connection, self.iface = connection, iface
        self.read_only = read_only
        self.canvas = canvas = iface.mapCanvas()
        self.changed, self.failure = changed, failure
        self.tiles = RasterTiles(connection, failure)
        self.edits = ClientEdits(self)
        self.online = OnlineLayers(self, rejected)
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
        self._import_focus = set()
        self._zoom_import = QTimer(canvas, interval=0, singleShot=True)
        self._zoom_import.timeout.connect(self._focus_imported_rasters)
        self._view = QTimer(canvas, interval=500, singleShot=True)
        self._view.timeout.connect(self._send_view)
        self._timer = QTimer(canvas, interval=350)
        self._timer.timeout.connect(self._sync_local)
        if not self.read_only:
            self._timer.start()
        canvas.extentsChanged.connect(self._schedule_view)
        canvas.rotationChanged.connect(self._schedule_view)
        canvas.layersChanged.connect(self._schedule_import_focus)
        self.project.cleared.connect(self._local_project_cleared)
        connection.workspace_changed.connect(self.refresh)
        connection.status_changed.connect(self._status)

    def _status(self, state, _detail):
        if state == "connected":
            self.revision = -1
            for key, layer in self._local_layers().items():
                if self.descriptors[key]["kind"] == "raster":
                    layer.triggerRepaint()
            for key in self.edits.records:
                if self.edits.working_pending(key):
                    self.edits.schedule(key)

    def refresh(self, revision=None):
        if not self._workspace_open:
            return
        if revision == self.revision:
            return
        if self._loading or self._sending:
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
        update_tree = new_session or self.group is None
        if "tree" in state:
            update_tree |= state["tree"] != self.tree
            self.tree = state["tree"]
        previous = self.descriptors.copy()
        if state["full"]:
            self.descriptors.clear()
        for key in state["removed"]:
            self.descriptors.pop(key, None)
        for descriptor in state["layers"]:
            self.descriptors[descriptor["id"]] = descriptor
        self._unavailable.intersection_update(self.descriptors)
        self.tiles.set_descriptors([desc for desc in self.descriptors.values() if desc["kind"] == "raster"])
        for identifier, local in tuple(self.layers.items()):
            if identifier not in self.descriptors or not self.project.mapLayer(local):
                self.edits.detach(identifier)
                self.layers.pop(identifier)
                if not self.online.release(identifier):
                    self.project.removeMapLayer(local)
                update_tree = True
        updated_styles = set()
        for identifier, descriptor in self.descriptors.items():
            if identifier in self.layers:
                layer = self.project.mapLayer(self.layers[identifier])
                before = previous.get(identifier, {})
                if (descriptor["kind"] == "raster" and descriptor.get("tile_version")
                        and descriptor["tile_version"] != before.get("tile_version")):
                    self.canvas.stopRendering()
                    layer.setDataSource(self.tiles.uri(descriptor), descriptor["name"], "wms")
                    layer.triggerRepaint()
                if descriptor != before:
                    self._style(layer, descriptor)
                    updated_styles.add(identifier)
                if (descriptor["kind"] == "vector" and descriptor.get("vector_version") != before.get("vector_version")
                        and not layer.isEditable() and not self.edits.working_pending(identifier)
                        and identifier not in self.pending and identifier not in self.edits.in_flight):
                    self._request_vector(identifier)
            elif identifier not in self.pending:
                if descriptor["kind"] == "online":
                    try:
                        layer = self.online.layer(descriptor)
                        self._add(identifier, layer)
                        layer.setCustomProperty(ONLINE_PROPERTY, True)
                        update_tree = True
                    except ValueError as exc:
                        self._unavailable.add(identifier)
                        self.failure(str(exc))
                elif descriptor["kind"] == "raster":
                    try:
                        self._add(identifier, self.tiles.layer(descriptor))
                        update_tree = True
                    except ValueError as exc:
                        self._unavailable.add(identifier)
                        self.failure(str(exc))
                else:
                    self._request_vector(identifier)
        if update_tree:
            self._layout()
        elif updated_styles and self._baseline is not None:
            _, settings = self._read_local()
            self._baseline[1].update({key: settings[key] for key in updated_styles if key in settings})
        if updated_styles:
            self.canvas.refresh()
        if (new_session or changed_project) and state.get("view"):
            self._initial_view(state["view"])
        self._schedule_import_focus()
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
        working, record = (document, None) if self.read_only else self.edits.restore(descriptor, document)
        fresh = vector_proxy(descriptor["name"], working)
        old_id = self.layers.get(identifier)
        old = self.project.mapLayer(old_id) if old_id is not None else None
        if old is not None and old.isEditable():
            return
        was_active = old is not None and old == self.iface.activeLayer()
        self.edits.detach(identifier)
        same_schema = (old is not None and old.wkbType() == fresh.wkbType() and old.crs() == fresh.crs()
                       and [field_data(field) for field in old.fields()] ==
                       [field_data(field) for field in fresh.fields()])
        if same_schema:
            # The canvas, legend and native digitizing tools retain the same layer ID.
            provider = old.dataProvider()
            prior_ids = old.allFeatureIds()
            features = list(fresh.getFeatures())
            ok, added = provider.addFeatures(features)
            if not ok or prior_ids and not provider.deleteFeatures(prior_ids):
                provider.deleteFeatures({feature.id() for feature in added if feature.id() not in prior_ids})
                raise ValueError("无法更新本地矢量代理。")
            old.updateExtents(True)
            self._unavailable.discard(identifier)
            layer = old
        else:
            layer = fresh
            self._add(identifier, layer)
            self._layout()
            if old is not None:
                self.project.removeMapLayer(old_id)
            if was_active:
                self.iface.setActiveLayer(layer)
        if working.get("editable") and not self.read_only:
            self.edits.attach(identifier, layer, document, record)
        self._layout()
        layer.triggerRepaint()
        self.canvas.refresh()
        self.changed()

    def reveal_imported_rasters(self, response, paths):
        imported = response.get("imported")
        self._import_focus.update(layer["id"] for layer in response["layers"] if layer["kind"] == "raster"
                                  and (layer["id"] in imported if imported is not None else layer["path"] in paths))
        self._schedule_import_focus()

    def _schedule_import_focus(self):
        if self._import_focus:
            self._zoom_import.start()

    def _focus_imported_rasters(self):
        visible = {layer.id() for layer in self.canvas.layers()}
        ready = {key for key in self._import_focus if self.layers.get(key) in visible}
        self._import_focus.difference_update(ready | self._unavailable)
        if ready:
            self.zoom_to_rasters(ready)

    def zoom_to_rasters(self, keys):
        extent = QgsRectangle()
        crs = self.canvas.mapSettings().destinationCrs()
        for key in keys:
            desc = self.descriptors[key]
            source = crs_from(desc["wkt"], desc["crs"])
            try:
                if source.isValid():
                    rect = QgsCoordinateTransform(source, crs, self.project).transformBoundingBox(
                        QgsRectangle(*desc["bounds"]))
                    if not rect.isEmpty():
                        extent.combineExtentWith(rect)
            except Exception:
                continue
        if extent.isEmpty():
            self.failure("无法定位影像，请检查工作站图层的坐标系和范围。")
            return
        extent.scale(1.05)
        self.canvas.setExtent(extent)
        self.canvas.refresh()

    @staticmethod
    def _style(layer, descriptor):
        layer.setName(descriptor["name"])
        if descriptor["kind"] == "raster" and layer.providerType() == "wms":
            # A 1.2 tile already includes the workstation layer's opacity.
            set_opacity(layer, 1.0 if "mode" in descriptor["render"] else descriptor["opacity"])
            return
        set_opacity(layer, descriptor["opacity"])
        apply_style(layer, descriptor.get("style", ""))
        apply_properties(layer, descriptor.get("properties", ""))

    def _add(self, identifier, layer):
        self._unavailable.discard(identifier)
        layer.setCustomProperty("remoteqgis/id", identifier)
        self._style(layer, self.descriptors[identifier])
        if self.project.mapLayer(layer.id()) is None:
            self.project.addMapLayer(layer, False)
        self.layers[identifier] = layer.id()

    def _local_layers(self):
        return {key: layer for key, local in self.layers.items()
                if (layer := self.project.mapLayer(local)) is not None}

    def _layout(self):
        layers = self._local_layers()
        if not self.session and not layers and not self.tree and self.group is None:
            return
        if self.group is None:
            self.group = self.project.layerTreeRoot().insertGroup(0, "RemoteQGIS")
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
        settings = {key: {"name": layer.name(), "opacity": opacity(layer), "style": read_style(layer),
                          "properties": read_properties(layer)}
                    for key, layer in layers.items()}
        for key, layer in layers.items():
            desc = self.descriptors[key]
            if desc["kind"] == "raster" and layer.providerType() == "wms":
                settings[key].update(style=desc.get("style", ""), properties=desc.get("properties", ""))
                if "mode" in desc["render"]:
                    settings[key]["opacity"] = desc["opacity"]
        return tree, settings

    def _sync_local(self):
        self.online.inspect()
        if (not self.connection.ready or self._sending or self._loading
                or self._baseline is None or self.revision < 0):
            return
        tree, settings = self._read_local()
        if (tree, settings) == self._baseline:
            return
        tree = retain_unloaded(tree, self.tree, self.descriptors.keys() - self.layers.keys())
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
        if self._refresh_pending:
            self._refresh_pending = False
            self.refresh()

    def _schedule_view(self, *args):
        if not self.read_only and self.connection.ready and self.revision >= 0:
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
        self._import_focus.clear()
        self._zoom_import.stop()
        for key in tuple(self.edits.records):
            self.edits.detach(key)
        managed = [local for key, local in self.layers.items() if not self.online.release(key)]
        self.online.clear()
        self.project.removeMapLayers(managed)
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
        self.tiles.set_descriptors([])
        self.pending.clear()
        self._unavailable.clear()

    def _local_project_cleared(self):
        # A local QGIS project switch removes proxy nodes as a side effect.
        self.group = None
        self._baseline = None
        for key in tuple(self.edits.records):
            self.edits.detach(key)
        self.layers.clear()
        self.online.clear()
        self.revision = -1
        self._view.stop()
        self._import_focus.clear()
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
        self.canvas.layersChanged.disconnect(self._schedule_import_focus)
        self.connection.workspace_changed.disconnect(self.refresh)
        self.connection.status_changed.disconnect(self._status)
        self.tiles.close()
        self.edits.close()
        self.clear()
        self._zoom_import.deleteLater()
