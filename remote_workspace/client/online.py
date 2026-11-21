# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import QgsRasterLayer
from qgis.PyQt import sip

from ..common.layers import GROUP_ID, is_ancestor
from ..common.online import ONLINE_PROPERTY, online_uri, source_from_layer
from ..common.workspace import WorkspaceError


class OnlineLayers:
    def __init__(self, proxies, rejected):
        self.proxies = proxies
        self.rejected = rejected
        self.positions = {}
        self.waiting = {}
        self.adopted = {}

    def inspect(self):
        proxies = self.proxies
        group = proxies.group
        if group is None or sip.isdeleted(group):
            return
        root = proxies.project.layerTreeRoot()
        for node in root.findLayers():
            parent = node.parent()
            if not is_ancestor(group, parent):
                self.positions[node.layerId()] = (parent, parent.children().index(node))
        managed = set(proxies.layers.values())
        for node in tuple(group.findLayers()):
            identifier = node.layerId()
            if identifier in managed or identifier in self.waiting:
                continue
            layer = node.layer()
            if layer is None:
                continue
            if identifier not in self.positions:
                # QGIS inserts a newly opened layer at the current legend insertion point.
                self.restore(identifier)
                continue
            try:
                uri = source_from_layer(layer)
            except WorkspaceError as exc:
                self.restore(identifier)
                self.rejected(str(exc))
                continue
            if not proxies.connection.ready:
                self.restore(identifier)
                self.rejected("请连接工作站后再拖入在线图层。")
                continue
            parent = node.parent()
            self.waiting[identifier] = uri
            proxies.connection.call("online_add", {
                "source": uri, "name": layer.name(), "visible": node.itemVisibilityChecked(),
                "group": parent.customProperty(GROUP_ID, ""), "position": parent.children().index(node),
            }, lambda data, error, key=identifier: self.completed(key, data, error))

    def completed(self, key, state, error):
        if key not in self.waiting:
            return
        if error:
            self.waiting.pop(key)
            self.restore(key)
            self.rejected(error)
        elif self.proxies.connection.ready:
            self.proxies.apply(state)
            self.waiting.pop(key, None)
            self.proxies.refresh()

    def layer(self, descriptor):
        uri = online_uri(descriptor["online_source"])
        root = self.proxies.project.layerTreeRoot()
        for key, source in tuple(self.waiting.items()):
            node = root.findLayer(key)
            if source == uri and node is not None and node.layer() is not None:
                self.adopted[descriptor["id"]] = key
                self.waiting.pop(key)
                return node.layer()
        layer = QgsRasterLayer(uri, descriptor["name"], "wms")
        if not layer.isValid():
            raise ValueError(f"客户端无法打开在线图层「{descriptor['name']}」。")
        return layer

    def release(self, key):
        local = self.adopted.pop(key, None)
        if local is None:
            return False
        layer = self.proxies.project.mapLayer(local)
        if layer is not None:
            layer.removeCustomProperty("remoteqgis/id")
            layer.removeCustomProperty(ONLINE_PROPERTY)
            self.restore(local)
        return True

    def restore(self, identifier):
        root = self.proxies.project.layerTreeRoot()
        node = root.findLayer(identifier)
        if node is None:
            return
        parent, index = self.positions.get(identifier, (root, len(root.children())))
        if sip.isdeleted(parent) or is_ancestor(self.proxies.group, parent):
            parent, index = root, len(root.children())
        if node.parent() == parent:
            return
        parent.insertChildNode(min(index, len(parent.children())), node.clone())
        node.parent().removeChildNode(node)

    def clear(self):
        for key in tuple(self.adopted):
            self.release(key)
        for local in tuple(self.waiting):
            self.restore(local)
        self.waiting.clear()
        self.positions.clear()
