# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import PurePosixPath

from qgis.core import QgsCoordinateTransform, QgsMimeDataUtils, QgsProject
from qgis.gui import QgsCustomDropHandler, QgsMapToolEmitPoint
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMenu, QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..client.proxy import Proxies, crs_from
from .vectors import VectorActions

MIME_PROVIDER = "remoteqgis"


class RemoteTree(QTreeWidget):
    def mimeData(self, items):
        uris = []
        for item in items:
            entry = item.data(0, Qt.UserRole)
            if entry["kind"] in ("raster", "vector"):
                uri = QgsMimeDataUtils.Uri()
                uri.layerType = "custom"
                uri.providerKey = MIME_PROVIDER
                uri.name = entry["name"]
                uri.uri = entry["path"]
                uris.append(uri)
        return QgsMimeDataUtils.encodeUriList(uris)


class RemoteDropHandler(QgsCustomDropHandler):
    def __init__(self, browser):
        super().__init__()
        self.browser = browser

    def customUriProviderKey(self):
        return MIME_PROVIDER

    def handleCustomUriCanvasDrop(self, uri, _canvas):
        self.browser.import_paths([uri.uri])
        return True

    def handleCustomUriDrop(self, uri):
        self.browser.import_paths([uri.uri])


class Browser(QWidget):
    def __init__(self, connection, iface):
        super().__init__()
        self.connection = connection
        self.iface = iface
        self.directory = ""
        self._layer_warning = None
        self.message = QLabel("连接工作站后可浏览共享目录。")
        self.message.setWordWrap(True)
        self.message.setTextFormat(Qt.PlainText)
        self.path = QLineEdit()
        self.path.setPlaceholderText("共享目录下的相对路径")
        self.path.returnPressed.connect(lambda: self.browse(self.path.text()))
        self.filter = QComboBox()
        self.filter.addItems(["全部", "栅格", "矢量", "项目"])
        self.filter.currentIndexChanged.connect(self._filter)
        self.tree = RemoteTree()
        self.tree.setHeaderLabels(["远端文件", "类型"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setDragEnabled(True)
        self.tree.itemDoubleClicked.connect(self._open)
        self.proxies = Proxies(connection, iface, self._refresh_layers, self.show_error, self.reject_layer)
        self.vector_actions = VectorActions(self)
        self.drop_handler = RemoteDropHandler(self)
        iface.registerCustomDropHandler(self.drop_handler)
        self.entries = []
        layout = QVBoxLayout(self)
        navigation = QHBoxLayout()
        navigation.addWidget(self._button("上一级", self._up))
        navigation.addWidget(self.path)
        navigation.addWidget(self._button("刷新", lambda: self.browse(self.path.text())))
        layout.addLayout(navigation)
        layout.addWidget(self.filter)
        layout.addWidget(self.tree)
        actions = QHBoxLayout()
        for title, slot in (("导入", self.import_selected), ("新建目录", self.mkdir),
                            ("重命名", self.rename), ("删除", self.delete)):
            actions.addWidget(self._button(title, slot))
        project_button = QPushButton("远端项目")
        menu = QMenu(project_button)
        for title, action in (("新建项目…", "create"), ("打开所选项目", "open"), ("保存项目", "save"),
                              ("另存项目…", "save_as"), ("关闭项目", "close")):
            menu.addAction(title, lambda operation=action: self.project_action(operation))
        project_button.setMenu(menu)
        actions.addWidget(project_button)
        layout.addLayout(actions)
        image = QHBoxLayout()
        image.addWidget(self._button("影像范围", self._zoom_raster))
        image.addWidget(self._button("波段与拉伸", self._render_raster))
        image.addWidget(self._button("原始像元", self._query_raster))
        display = QPushButton("显示方式")
        display_menu = QMenu(display)
        display_menu.addAction("快速瓦片预览", lambda: self._raster_display(True))
        display_menu.addAction("原始波段 / 重试", lambda: self._raster_display(False))
        display.setMenu(display_menu)
        image.addWidget(display)
        layout.addLayout(image)
        self._pixel_tool = QgsMapToolEmitPoint(iface.mapCanvas())
        self._pixel_tool.canvasClicked.connect(self._pixel_clicked)
        self._pixel_target = None
        self._previous_tool = None
        vector = QHBoxLayout()
        vector.addWidget(self._button("新建矢量", self.vector_actions.create))
        vector.addWidget(self._button("启用编辑", self.vector_actions.enable))
        vector.addWidget(self._button("保存或重试编辑", self.vector_actions.save))
        layout.addLayout(vector)
        self.layers_status = QLabel()
        layout.addWidget(self.layers_status)
        layout.addWidget(self.message)
        connection.status_changed.connect(self._connection)
        self.setEnabled(False)

    @staticmethod
    def _button(label, slot):
        button = QPushButton(label)
        button.clicked.connect(slot)
        return button

    def _connection(self, state, _detail):
        ready = state == "connected"
        self.setEnabled(ready)
        if ready:
            directory = self.directory if self.proxies.origin == self.connection.origin else ""
            self.browse(directory)
        if state in ("idle", "error") and not self.proxies.edits.has_unrecorded():
            self.proxies.clear()

    def show_error(self, message):
        self.message.setText(message)

    def reject_layer(self, message):
        self.show_error(message)
        if self._layer_warning is not None:
            self._layer_warning.setText(message)
            return
        warning = QMessageBox(QMessageBox.Warning, "图层未同步", message, QMessageBox.Ok, self)
        self._layer_warning = warning
        warning.finished.connect(self._dismiss_layer_warning)
        warning.open()

    def _dismiss_layer_warning(self, _result):
        self._layer_warning.deleteLater()
        self._layer_warning = None

    def _refresh_layers(self):
        project = self.proxies.remote_project
        name = project.get("title") or PurePosixPath(project.get("path") or "未命名项目").stem
        pending = sum(self.proxies.edits.working_pending(key) for key in self.proxies.edits.records)
        suffix = f" · {pending} 个图层待工作站确认" if pending else ""
        self.layers_status.setText(f"{name} · {len(self.proxies.layers)} 个代理图层{suffix}")

    def browse(self, path: str):
        path = path.strip().strip("/")
        self.connection.call("files", {"path": path}, lambda data, error: self._listed(path, data, error))

    def _listed(self, path, data, error):
        if error:
            self.show_error(error)
            return
        self.directory = path
        self.path.setText(path)
        self.entries = data["entries"]
        self._filter()
        self.show_error(f"{len(self.entries)} 个目录或数据文件。")

    def _filter(self):
        kinds = {0: "", 1: "raster", 2: "vector", 3: "project"}
        labels = {"directory": "目录", "raster": "栅格", "vector": "矢量", "project": "项目"}
        selected = kinds[self.filter.currentIndex()]
        self.tree.clear()
        for entry in self.entries:
            if selected and entry["kind"] not in (selected, "directory"):
                continue
            item = QTreeWidgetItem([entry["name"], labels[entry["kind"]]])
            item.setData(0, Qt.UserRole, entry)
            self.tree.addTopLevelItem(item)

    def _up(self):
        self.browse(self.directory.rpartition("/")[0])

    def _open(self, item):
        entry = item.data(0, Qt.UserRole)
        if entry["kind"] == "directory":
            self.browse(entry["path"])
        elif entry["kind"] == "project":
            self.project_action("open", entry["path"])
        elif entry["kind"] in ("vector", "raster"):
            self.import_paths([entry["path"]])

    def selected(self):
        return [item.data(0, Qt.UserRole) for item in self.tree.selectedItems()]

    def import_selected(self):
        self.import_paths([entry["path"] for entry in self.selected() if entry["kind"] in ("vector", "raster")])

    def import_paths(self, paths):
        if not paths:
            self.show_error("请选择要导入的远端栅格或矢量文件。")
            return
        self.show_error("正在工作站加载所选数据…")

        def complete(result, error):
            if error:
                self.show_error(error)
            else:
                self.show_error("工作站已导入所选数据。")
                self.proxies.apply(result)
                self.proxies.reveal_imported_rasters(result, paths)
                self.proxies.refresh()

        self.connection.call("import", {"paths": paths}, complete)

    def target(self, name):
        return f"{self.directory}/{name}" if self.directory else name

    def mkdir(self):
        name, ok = QInputDialog.getText(self, "新建远端目录", "目录名")
        if ok and name.strip():
            self._file_operation("mkdir", {"path": self.target(name.strip())})

    def rename(self):
        entries = self.selected()
        if len(entries) != 1:
            self.show_error("请选择一个文件或目录。")
            return
        entry = entries[0]
        suffix = PurePosixPath(entry["name"]).suffix if entry["kind"] != "directory" else ""
        name, ok = QInputDialog.getText(self, "重命名", "新名称", text=entry["name"])
        if ok and name.strip():
            if suffix and not name.lower().endswith(suffix.lower()):
                name += suffix
            self._file_operation("rename", {"path": entry["path"], "target": self.target(name.strip())})

    def delete(self):
        entries = self.selected()
        if len(entries) != 1:
            self.show_error("请选择一个文件或目录。")
            return
        entry = entries[0]
        if QMessageBox.question(self, "删除远端数据", f"将「{entry['name']}」移入工作站共享目录的回收区？",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes:
            self._file_operation("delete", {"path": entry["path"], "confirmed": True})

    def _file_operation(self, operation, data):
        self.connection.call(operation, data, lambda _, error: self.show_error(error) if error
                             else self.browse(self.directory))

    def project_action(self, action, path=None):
        if action == "open" and not path:
            chosen = self.selected()
            if len(chosen) != 1 or chosen[0]["kind"] != "project":
                self.show_error("请选择一个远端 QGIS 项目。")
                return
            path = chosen[0]["path"]
        if action in ("create", "save_as"):
            name, ok = QInputDialog.getText(self, "远端项目路径", "当前目录下的文件名（.qgz 或 .qgs）")
            if not ok or not name.strip():
                return
            name = name.strip()
            path = self.target(name if PurePosixPath(name).suffix else name + ".qgz")
        data = {"action": action, "path": path}
        if action in ("open", "close", "create"):
            if self.proxies.edits.has_unsaved():
                self.show_error("请先保存或处理本地矢量编辑，再切换远端项目。")
                return
            answer = QMessageBox.question(self, "切换远端项目", "继续后，工作站项目未保存的图层配置将被放弃。",
                                          QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if answer != QMessageBox.Yes:
                return
            data["discard"] = True
        self.connection.call("project", data, self._project_done)

    def _project_done(self, _, error):
        if error:
            self.show_error(error)
        else:
            self.browse(self.directory)
            self.proxies.refresh()

    def active_descriptor(self, kind):
        layer = self.iface.activeLayer()
        identifier = layer.customProperty("remoteqgis/id") if layer is not None else None
        descriptor = self.proxies.descriptors.get(identifier)
        if descriptor and descriptor["kind"] == kind:
            return descriptor
        self.show_error("请在 QGIS 图层面板中选择对应的远端图层。")
        return None

    def _raster_transform(self, descriptor):
        canvas = self.iface.mapCanvas()
        return QgsCoordinateTransform(crs_from(descriptor["wkt"], descriptor["crs"]),
                                      canvas.mapSettings().destinationCrs(), QgsProject.instance())

    def _zoom_raster(self):
        descriptor = self.active_descriptor("raster")
        if descriptor:
            self.proxies.zoom_to_rasters([descriptor["id"]])

    def _raster_display(self, preview):
        descriptor = self.active_descriptor("raster")
        if descriptor:
            self.proxies.set_raster_preview(descriptor["id"], preview)

    def _render_raster(self):
        descriptor = self.active_descriptor("raster")
        if not descriptor:
            return
        render = descriptor["render"]
        text, ok = QInputDialog.getText(
            self, "远端影像渲染", "波段（1 或 3 个，逗号分隔）",
            text=",".join(map(str, render["bands"])),
        )
        if not ok:
            return
        stretch, ok = QInputDialog.getText(
            self, "远端影像渲染", "每个波段的下限:上限，以逗号分隔。留空自动拉伸。",
            text=",".join(f"{lo}:{hi}" for lo, hi in render["stretch"]),
        )
        if not ok:
            return
        resampling = ["near", "bilinear", "cubic", "average"]
        algorithm, ok = QInputDialog.getItem(self, "远端影像渲染", "重采样", resampling,
                                             resampling.index(render["resample"]), False)
        if not ok:
            return
        nodata, ok = QInputDialog.getText(
            self, "远端影像渲染", "NoData（留空使用源数据定义）",
            text="" if render["nodata"] is None else str(render["nodata"]),
        )
        if not ok:
            return
        try:
            config = {
                "id": descriptor["id"], "bands": [int(band) for band in text.split(",")],
                "stretch": [[float(value) for value in pair.split(":")]
                            for pair in stretch.split(",")] if stretch.strip() else [],
                "nodata": float(nodata) if nodata.strip() else None, "resample": algorithm,
            }
            self.connection.call("render", config, lambda _, error: self.show_error(error)
                                 if error else self.proxies.refresh())
        except ValueError:
            self.show_error("波段、拉伸区间或 NoData 的格式无效。")

    def _query_raster(self):
        self._pixel_target = self.active_descriptor("raster")
        if self._pixel_target:
            canvas = self.iface.mapCanvas()
            if canvas.mapTool() is not self._pixel_tool:
                self._previous_tool = canvas.mapTool()
            canvas.setMapTool(self._pixel_tool)
            self.show_error("请在地图中单击要查询的像元。")

    def _restore_tool(self):
        canvas = self.iface.mapCanvas()
        if canvas.mapTool() is self._pixel_tool:
            if self._previous_tool:
                canvas.setMapTool(self._previous_tool)
            else:
                canvas.unsetMapTool(self._pixel_tool)

    def _pixel_clicked(self, point, _button):
        self._restore_tool()
        descriptor, self._pixel_target = self._pixel_target, None
        if not descriptor:
            return
        try:
            point = self._raster_transform(descriptor).transform(point, QgsCoordinateTransform.ReverseTransform)
        except Exception:
            self.show_error("无法转换像元查询坐标。")
            return
        self.connection.call("pixel", {"id": descriptor["id"], "x": point.x(), "y": point.y()},
                             self._pixel_result)

    def _pixel_result(self, data, error):
        self.show_error(error or f"原始像元（行 {data['row']}，列 {data['column']}） {data['values']}")

    def shutdown(self):
        self._restore_tool()
        self.proxies.close()
        self.iface.unregisterCustomDropHandler(self.drop_handler)
