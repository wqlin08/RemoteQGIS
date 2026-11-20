# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import PurePosixPath

from qgis.core import QgsProject
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QMessageBox, QPlainTextEdit,
)

FIELD_TYPES = {"text": QVariant.String, "int": QVariant.Int, "int64": QVariant.LongLong,
               "real": QVariant.Double, "date": QVariant.Date, "datetime": QVariant.DateTime,
               "bool": QVariant.Bool}


class VectorActions:
    def __init__(self, browser):
        self.browser = browser
        self.proxies = browser.proxies

    def create(self):
        browser = self.browser
        dialog = QDialog(browser)
        dialog.setWindowTitle("新建远端矢量数据")
        form = QFormLayout(dialog)
        filename = QLineEdit()
        filename.setPlaceholderText("名称.gpkg 或 名称.shp")
        shape = QComboBox()
        shape.addItems(["Point", "LineString", "Polygon", "MultiPoint", "MultiLineString", "MultiPolygon"])
        crs = QLineEdit(QgsProject.instance().crs().authid() or "EPSG:4326")
        fields = QPlainTextEdit()
        fields.setPlaceholderText("每行一个字段，例如 名称:text 或 height:real")
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        for label, field in (("远端文件名", filename), ("几何类型", shape), ("坐标系", crs), ("字段", fields)):
            form.addRow(label, field)
        form.addRow(buttons)
        if not dialog.exec():
            return
        name = filename.text().strip()
        if not name or "/" in name or "\\" in name:
            browser.show_error("请填写当前目录下的文件名。")
            return
        if not PurePosixPath(name).suffix:
            name += ".gpkg"
        definitions = []
        for row in fields.toPlainText().splitlines():
            if not row.strip():
                continue
            field, _, kind = row.strip().partition(":")
            if not field or kind.lower() not in FIELD_TYPES:
                browser.show_error("字段格式应为 名称:类型，可用类型为 " + "、".join(FIELD_TYPES))
                return
            limit = 80 if kind == "text" and name.lower().endswith(".shp") else 0
            definitions.append([field, int(FIELD_TYPES[kind.lower()]), limit, 0, 0])
        browser.connection.call("create_vector", {
            "path": browser.target(name), "geometry": shape.currentText(),
            "crs": crs.text().strip(), "fields": definitions,
        }, browser._project_done)

    def enable(self):
        descriptor = self.browser.active_descriptor("vector")
        if not descriptor:
            return
        key = descriptor["id"]
        if key in self.proxies.edits.records:
            self.browser.show_error("图层可编辑，请使用 QGIS 的切换编辑工具。")
            return
        choice = QMessageBox.question(
            self.browser, "启用远端编辑", "需在工作站数据中写入 rq_uuid 字段，以便跨编辑操作识别要素。继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if choice != QMessageBox.Yes:
            return
        self.browser.connection.call("enable_edit", {"id": key}, lambda doc, error: self.browser.show_error(error)
                                     if error else self.proxies.install_vector(key, doc))

    def save(self):
        descriptor = self.browser.active_descriptor("vector")
        if descriptor:
            self.proxies.edits.retry(descriptor["id"])
