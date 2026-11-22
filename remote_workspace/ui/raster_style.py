# SPDX-License-Identifier: GPL-3.0-or-later

import math
from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..common.layers import raster_qml

MODES = (
    ("gray", "单波段灰度"), ("rgb", "多波段 RGB"),
    ("palette", "离散类别色表"), ("pseudocolor", "单波段伪彩色"),
)
RESAMPLING = (("current", "沿用工作站设置"), ("near", "最邻近"), ("bilinear", "双线性"),
              ("cubic", "三次卷积"), ("average", "平均"))
INTERPOLATION = (("linear", "线性插值"), ("discrete", "离散区间"), ("exact", "精确匹配"))
DEFAULT_COLORS = ("#208349", "#3c74bd", "#edb848", "#c95752", "#8c5cb0", "#56abb5")


class RasterStyleDialog(QDialog):
    def __init__(self, parent, connection, descriptor):
        super().__init__(parent)
        self.connection, self.descriptor = connection, descriptor
        self.payload = None
        self.setWindowTitle(f"远端栅格符号化 · {descriptor['name']}")
        self.resize(570, 440)
        current = descriptor["render"]
        self.mode = self.combo(MODES + (("current", "保持当前渲染器"),), current["mode"])
        self.bands = QLineEdit(", ".join(map(str, current["bands"])))
        self.bands.setPlaceholderText(f"1–{descriptor['bands']}，RGB 填写三个波段")
        self.stretch = QLineEdit(", ".join(f"{lo:g}:{hi:g}" for lo, hi in current["stretch"]))
        self.stretch.setPlaceholderText("下限:上限，留空由工作站自动拉伸")
        self.resample = self.combo(RESAMPLING, current["configured_resample"])
        self.nodata = QLineEdit("" if current["nodata"] is None else str(current["nodata"]))
        self.nodata.setPlaceholderText("留空使用数据源设置")
        self.alpha = QDoubleSpinBox()
        self.alpha.setRange(0, 100)
        self.alpha.setSuffix(" %")
        self.alpha.setValue(descriptor["opacity"] * 100)
        self.interpolation = self.combo(INTERPOLATION, current.get("interpolation", "linear"))
        self.clip = QCheckBox("裁切色表范围外的值")
        self.clip.setChecked(current.get("clip", False))

        self.form = QFormLayout()
        for title, widget in (("渲染方式", self.mode), ("波段", self.bands), ("拉伸区间", self.stretch),
                              ("颜色插值", self.interpolation), ("", self.clip),
                              ("重采样", self.resample), ("NoData", self.nodata), ("不透明度", self.alpha)):
            self.form.addRow(title, widget)

        self.colors = QTableWidget(0, 3)
        self.colors.setHorizontalHeaderLabels(["像元值 / 分级上限", "颜色", "标签"])
        self.colors.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.colors.cellDoubleClicked.connect(self.select_color)
        self.colors.itemChanged.connect(self.color_swatch)
        for value, color, label in current.get("colors", ()):
            self.add_color(value, color, label)
        color_buttons = QHBoxLayout()
        add = QPushButton("添加颜色")
        add.clicked.connect(lambda: self.add_color(edit=True))
        remove = QPushButton("删除所选")
        remove.clicked.connect(self.remove_colors)
        self.sample = QPushButton("采样类别")
        self.sample.setToolTip("由工作站采样当前波段，手动添加采样遗漏的类别")
        self.sample.clicked.connect(self.sample_categories)
        for button in (add, remove, self.sample):
            color_buttons.addWidget(button)
        color_layout = QVBoxLayout()
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.addWidget(self.colors)
        color_layout.addLayout(color_buttons)
        self.color_section = QWidget()
        self.color_section.setLayout(color_layout)

        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("应用到工作站")
        load_qml = buttons.addButton("应用 QML 样式…", QDialogButtonBox.ActionRole)
        load_qml.clicked.connect(self.load_qml)
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(self.form)
        layout.addWidget(self.color_section)
        layout.addWidget(self.message)
        layout.addWidget(buttons)
        self.mode.currentIndexChanged.connect(self.update_fields)
        self.update_fields()

    @staticmethod
    def combo(values, current):
        box = QComboBox()
        for key, name in values:
            box.addItem(name, key)
        index = box.findData(current)
        if index >= 0:
            box.setCurrentIndex(index)
        return box

    def update_fields(self):
        mode = self.mode.currentData()
        self.color_section.setVisible(mode in ("palette", "pseudocolor"))
        self.sample.setVisible(mode == "palette")
        for widget, enabled in ((self.bands, mode != "current"), (self.stretch, mode in ("gray", "rgb")),
                                (self.resample, mode != "palette"), (self.interpolation, mode == "pseudocolor"),
                                (self.clip, mode == "pseudocolor")):
            widget.setEnabled(enabled)

    def add_color(self, value="", color=None, label="", *, edit=False):
        row = self.colors.rowCount()
        self.colors.insertRow(row)
        color = color or DEFAULT_COLORS[row % len(DEFAULT_COLORS)]
        for column, text in enumerate((value, color, label)):
            self.colors.setItem(row, column, QTableWidgetItem(str(text)))
        if edit:
            self.colors.setCurrentCell(row, 0)
            self.colors.editItem(self.colors.item(row, 0))

    def remove_colors(self):
        for row in sorted({item.row() for item in self.colors.selectedItems()}, reverse=True):
            self.colors.removeRow(row)

    def select_color(self, row, column):
        if column == 1:
            item = self.colors.item(row, column)
            color = QColorDialog.getColor(QColor(item.text()), self, "选择颜色", QColorDialog.ShowAlphaChannel)
            if color.isValid():
                item.setText(color.name(QColor.HexArgb))

    @staticmethod
    def color_swatch(item):
        if item.column() == 1:
            color = QColor(item.text())
            decoration = color if color.isValid() else None
            if item.data(Qt.DecorationRole) != decoration:
                item.setData(Qt.DecorationRole, decoration)

    def selected_bands(self):
        mode = self.mode.currentData()
        if mode == "current":
            return self.descriptor["render"]["bands"]
        bands = [int(value.strip()) for value in self.bands.text().split(",")]
        if len(bands) != (3 if mode == "rgb" else 1) or any(
            not 1 <= band <= self.descriptor["bands"] for band in bands
        ):
            raise ValueError("请选择有效波段。RGB 需要三个波段，其他方式需要一个。")
        return bands

    def sample_categories(self):
        try:
            band, = self.selected_bands()
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.sample.setEnabled(False)
        self.message.setText("正在工作站采样类别…")

        def completed(result, error):
            self.sample.setEnabled(True)
            if not self.isVisible():
                return
            if error or self.mode.currentData() != "palette" or self.bands.text().strip() != str(band):
                self.message.setText(error or "波段已更改，请重新采样。")
                return
            existing = set()
            for row in range(self.colors.rowCount()):
                try:
                    existing.add(self.number(self.colors.item(row, 0).text()))
                except ValueError:
                    continue
            for value in result["values"]:
                if value not in existing:
                    label = str(value)
                    self.add_color(label, label=label)
            self.message.setText(f"工作站采样得到 {len(result['values'])} 个类别。")

        self.connection.call("raster_categories", {"id": self.descriptor["id"], "band": band}, completed)

    @staticmethod
    def number(value):
        number = float(value.strip())
        if not math.isfinite(number):
            raise ValueError("请输入有限的数值。")
        return number

    def load_qml(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 QGIS 栅格样式", "", "QGIS 样式 (*.qml)")
        if not path:
            return
        try:
            with Path(path).open(encoding="utf-8-sig") as file:
                style = raster_qml(file.read(1024 * 1024 + 1), self.descriptor["bands"])
        except (OSError, ValueError) as exc:
            self.message.setText(str(exc))
            return
        self.payload = {"id": self.descriptor["id"], "base": self.descriptor["tile_version"], "qml": style}
        self.accept()

    def submit(self):
        try:
            mode = self.mode.currentData()
            data = {"id": self.descriptor["id"], "base": self.descriptor["tile_version"], "mode": mode,
                    "bands": self.selected_bands(), "resample": self.resample.currentData(),
                    "nodata": self.number(self.nodata.text()) if self.nodata.text().strip() else None,
                    "opacity": self.alpha.value() / 100}
            if mode in ("gray", "rgb") and self.stretch.text().strip():
                intervals = [[self.number(value) for value in pair.split(":")]
                             for pair in self.stretch.text().split(",")]
                if len(intervals) != len(data["bands"]) or any(len(pair) != 2 or pair[0] >= pair[1] for pair in intervals):
                    raise ValueError("每个波段需要一组有效的下限:上限。")
                data["stretch"] = intervals
            if mode in ("palette", "pseudocolor"):
                data["interpolation"] = self.interpolation.currentData()
                data["clip"] = self.clip.isChecked()
                colors = []
                for row in range(self.colors.rowCount()):
                    value, color, label = (self.colors.item(row, column).text().strip() for column in range(3))
                    upper_open = value.lower() == "inf" and mode == "pseudocolor" and data["interpolation"] == "discrete"
                    value = "inf" if upper_open else self.number(value)
                    if not QColor(color).isValid():
                        raise ValueError(f"第 {row + 1} 行的颜色无效。双击颜色单元格可选色。")
                    colors.append([value, QColor(color).name(QColor.HexArgb), label])
                if not colors:
                    raise ValueError("请先添加色表条目。")
                if len({row[0] for row in colors}) != len(colors):
                    raise ValueError("色表的像元值或分级上限须唯一。")
                data["colors"] = colors
        except ValueError as exc:
            self.message.setText(str(exc))
            return
        self.payload = data
        self.accept()
