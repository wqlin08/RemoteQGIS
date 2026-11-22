# SPDX-License-Identifier: GPL-3.0-or-later

import math
from uuid import uuid4
from xml.etree import ElementTree

from qgis.core import QgsFeatureRenderer, QgsLayerTreeGroup, QgsMapLayer, QgsRasterLayer, QgsReadWriteContext, QgsVectorLayer
from qgis.PyQt.QtXml import QDomDocument

from .workspace import WorkspaceError

GROUP_ID = "remoteqgis/group"
RENDERERS = {"singleSymbol", "categorizedSymbol", "graduatedSymbol", "RuleRenderer", "nullSymbol"}
SYMBOL_LAYERS = {
    "SimpleMarker", "SimpleLine", "SimpleFill", "EllipseMarker", "FilledMarker", "MarkerLine",
    "HashedLine", "PointPatternFill", "LinePatternFill", "CentroidFill", "GradientFill",
    "ShapeburstFill", "RandomMarkerFill",
}
RASTER_RENDERERS = {"paletted", "singlebandgray", "singlebandpseudocolor", "multibandcolor",
                    "singlebandcolordata", "hillshade", "contour"}
RASTER_PROPERTIES = QgsMapLayer.Symbology | QgsMapLayer.Rendering | QgsMapLayer.Legend
VECTOR_PROPERTIES = QgsMapLayer.Labeling | QgsMapLayer.Rendering | QgsMapLayer.Legend


def canonical_xml(root):
    for node in root.iter():
        attributes = sorted(node.attrib.items())
        node.attrib.clear()
        node.attrib.update(attributes)
        if node.text and not node.text.strip():
            node.text = None
        node.tail = None
    return ElementTree.tostring(root, encoding="unicode")


def properties_categories(layer):
    if isinstance(layer, QgsRasterLayer):
        return RASTER_PROPERTIES if layer.providerType() == "gdal" else QgsMapLayer.Rendering
    return VECTOR_PROPERTIES


def read_properties(layer):
    doc = QDomDocument()
    if layer.exportNamedStyle(doc, QgsReadWriteContext(), properties_categories(layer)):
        return ""
    root = ElementTree.fromstring(doc.toString())
    for parent in root.iter():
        for node in list(parent):
            if node.tag in ("customproperties", "datasource", "providerSource", "editforminit", "attributeactions"):
                parent.remove(node)
    text = canonical_xml(root)
    try:
        validate_properties(text)
    except WorkspaceError:
        return ""
    return text


def validate_properties(text):
    if not isinstance(text, str) or len(text) > 1024 * 1024 or "<!" in text:
        raise WorkspaceError("图层属性文档过大或格式无效。")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise WorkspaceError("图层属性 XML 无效。") from exc
    if root.tag != "qgis" or any(node.tag in ("datasource", "providerSource", "editforminit",
                                            "attributeactions", "customproperties", "layer")
                                for node in root.iter()):
        raise WorkspaceError("图层属性包含数据源或插件脚本。")
    if any(item.get("type") not in RASTER_RENDERERS for item in root.iter("rasterrenderer")):
        raise WorkspaceError("栅格渲染器类型不受支持。")


def raster_qml(text, band_count):
    if not isinstance(text, str) or len(text) > 1024 * 1024 or "<!ENTITY" in text:
        raise WorkspaceError("QML 文件过大或格式无效。")
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise WorkspaceError("无法解析 QGIS 栅格样式。") from exc
    pipe = root.find("pipe")
    renderer = pipe.find("rasterrenderer") if pipe is not None else None
    if root.tag != "qgis" or renderer is None:
        raise WorkspaceError("QML 文件中没有 QGIS 栅格样式。")
    try:
        bands = [int(renderer.get(key)) for key in ("band", "grayBand", "redBand", "greenBand", "blueBand", "alphaBand")
                 if renderer.get(key) is not None]
    except ValueError as exc:
        raise WorkspaceError("QML 样式中的波段编号无效。") from exc
    if any(band < -1 or band > band_count for band in bands):
        raise WorkspaceError("QML 样式引用了当前影像不存在的波段。")
    style = ElementTree.Element("qgis", {"styleCategories": "Symbology|Rendering|Legend"})
    style.append(pipe)
    document = canonical_xml(style)
    validate_properties(document)
    return document


def apply_properties(layer, text):
    if not text or text == read_properties(layer):
        return
    validate_properties(text)
    doc = QDomDocument()
    if not doc.setContent(text)[0]:
        raise WorkspaceError("无法解析图层属性。")
    ok, message = layer.importNamedStyle(doc, properties_categories(layer))
    if not ok:
        raise WorkspaceError(message or "QGIS 无法应用图层属性。")
    layer.triggerRepaint()



def read_style(layer):
    if not isinstance(layer, QgsVectorLayer) or layer.renderer() is None:
        return ""
    doc = QDomDocument()
    doc.appendChild(layer.renderer().save(doc, QgsReadWriteContext()))
    text = canonical_xml(ElementTree.fromstring(doc.toString()))
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


def retain_unloaded(local_tree, remote_tree, unloaded):
    """Keep real remote nodes which the current client could not materialize."""
    groups = {}

    def index(nodes):
        for node in nodes:
            if "group" in node:
                groups[node["group"]] = node["children"]
                index(node["children"])

    def retain(local, remote):
        for position, node in enumerate(remote):
            if "group" not in node:
                if node["id"] in unloaded:
                    local.insert(position, node)
                continue
            target = groups.get(node["group"])
            if target is not None:
                retain(target, node["children"])
            else:
                children = []
                retain(children, node["children"])
                if children:
                    local.insert(position, dict(node, children=children))

    index(local_tree)
    retain(local_tree, remote_tree)
    return local_tree


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
        if value.get("properties"):
            validate_properties(value["properties"])
