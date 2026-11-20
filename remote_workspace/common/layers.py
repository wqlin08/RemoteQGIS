# SPDX-License-Identifier: GPL-3.0-or-later

import math
from uuid import uuid4
from xml.etree import ElementTree

from qgis.core import QgsFeatureRenderer, QgsLayerTreeGroup, QgsReadWriteContext, QgsVectorLayer
from qgis.PyQt.QtXml import QDomDocument

from .workspace import WorkspaceError

GROUP_ID = "remoteqgis/group"
RENDERERS = {"singleSymbol", "categorizedSymbol", "graduatedSymbol", "RuleRenderer", "nullSymbol"}
SYMBOL_LAYERS = {"SimpleMarker", "SimpleLine", "SimpleFill"}


def read_style(layer):
    if not isinstance(layer, QgsVectorLayer) or layer.renderer() is None:
        return ""
    doc = QDomDocument()
    doc.appendChild(layer.renderer().save(doc, QgsReadWriteContext()))
    text = doc.toString()
    try:
        validate_style(text)
    except WorkspaceError:
        return ""
    return text


def validate_style(text):
    if not isinstance(text, str) or len(text) > 262144 or "<!" in text:
        raise WorkspaceError("矢量渲染样式格式无效。")
    try:
        root = ElementTree.fromstring(text)
    except (ValueError, ElementTree.ParseError) as exc:
        raise WorkspaceError("矢量样式 XML 无效。") from exc
    if root.tag != "renderer-v2" or root.get("type") not in RENDERERS:
        raise WorkspaceError("当前阶段仅支持使用基本符号的常用矢量渲染器。")
    if any(item.get("class") not in SYMBOL_LAYERS for item in root.iter("layer")):
        raise WorkspaceError("样式包含未支持的符号图层。")


def apply_style(layer, text):
    if text and isinstance(layer, QgsVectorLayer) and text != read_style(layer):
        validate_style(text)
        doc = QDomDocument()
        if not doc.setContent(text)[0]:
            raise WorkspaceError("无法解析矢量样式。")
        renderer = QgsFeatureRenderer.load(doc.documentElement(), QgsReadWriteContext())
        if renderer is None:
            raise WorkspaceError("无法加载矢量渲染器。")
        layer.setRenderer(renderer)
        layer.triggerRepaint()


def opacity(layer):
    if isinstance(layer, QgsVectorLayer):
        return layer.opacity()
    return layer.renderer().opacity() if layer.renderer() is not None else 1.0


def set_opacity(layer, value):
    if isinstance(layer, QgsVectorLayer):
        if layer.opacity() != value:
            layer.setOpacity(value)
            layer.triggerRepaint()
    elif layer.renderer() is not None and layer.renderer().opacity() != value:
        layer.renderer().setOpacity(value)
        layer.triggerRepaint()


def read_tree(root, identifiers):
    nodes = []
    for node in root.children():
        if isinstance(node, QgsLayerTreeGroup):
            key = node.customProperty(GROUP_ID, "")
            if not key:
                key = uuid4().hex
                node.setCustomProperty(GROUP_ID, key)
            nodes.append({"group": key, "name": node.name(), "visible": node.itemVisibilityChecked(),
                          "children": read_tree(node, identifiers)})
        elif node.layerId() in identifiers and node.layer():
            nodes.append({"id": identifiers[node.layerId()], "visible": node.itemVisibilityChecked()})
    return nodes


def validate_tree(tree, known):
    layers, groups = set(), set()

    def visit(nodes, depth):
        if not isinstance(nodes, list) or depth > 32:
            raise WorkspaceError("图层分组结构无效。")
        for node in nodes:
            if not isinstance(node, dict) or type(node.get("visible")) is not bool:
                raise WorkspaceError("图层树节点无效。")
            if "group" in node:
                key, name = node["group"], node.get("name")
                if not isinstance(key, str) or len(key) != 32 or any(c not in "0123456789abcdef" for c in key) \
                        or key in groups or not isinstance(name, str) or not 1 <= len(name) <= 512:
                    raise WorkspaceError("图层组标识或名称无效。")
                groups.add(key)
                visit(node.get("children"), depth + 1)
            else:
                key = node.get("id")
                if not isinstance(key, str) or key not in known or key in layers:
                    raise WorkspaceError("图层已变更，请重新同步。", 409)
                layers.add(key)

    visit(tree, 0)
    return layers, groups


def apply_tree(root, tree, layers, manage_groups=True):
    wanted, groups = validate_tree(tree, layers)

    def move(node, parent, index):
        if node is not None and node.parent() is parent:
            old_index = parent.children().index(node)
            if old_index == index:
                return node
            if old_index < index:
                index += 1
        new = node.clone()
        parent.insertChildNode(index, new)
        node.parent().removeChildNode(node)
        return new

    def build(parent, items):
        for index, item in enumerate(items):
            if "group" in item:
                key = item["group"]
                node = next((group for group in root.findGroups(True) if group.customProperty(GROUP_ID) == key), None)
                if node is None or is_ancestor(node, parent):
                    node = parent.insertGroup(index, item["name"])
                    node.setCustomProperty(GROUP_ID, key)
                else:
                    node = move(node, parent, index)
                node.setName(item["name"])
                build(node, item["children"])
            else:
                layer = layers[item["id"]]
                node = root.findLayer(layer.id())
                node = parent.insertLayer(index, layer) if not node else move(node, parent, index)
            node.setItemVisibilityChecked(item["visible"])

    build(root, tree)
    if manage_groups:
        for group in reversed(root.findGroups(True)):
            if group.customProperty(GROUP_ID) not in groups and not group.children():
                group.parent().removeChildNode(group)
    return wanted


def is_ancestor(node, other):
    while other:
        if node is other:
            return True
        other = other.parent()
    return False


def validate_settings(settings, known):
    if not isinstance(settings, dict):
        raise WorkspaceError("图层设置无效。")
    for key, value in settings.items():
        if key not in known or not isinstance(value, dict):
            raise WorkspaceError("图层已变更，请重新同步。", 409)
        name, alpha = value.get("name"), value.get("opacity")
        if not isinstance(name, str) or not 1 <= len(name) <= 512 or type(alpha) not in (float, int) \
                or not math.isfinite(alpha) or not 0 <= alpha <= 1:
            raise WorkspaceError("图层名称或透明度无效。")
        if value.get("style"):
            validate_style(value["style"])
