# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import PurePosixPath

from qgis.core import QgsMimeDataUtils
from qgis.gui import QgsCustomDropHandler
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..client.proxy import Proxies

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
        self.proxies = Proxies(connection, self._refresh_layers, self.show_error)
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
        layout.addLayout(actions)
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
        if state in ("idle", "error"):
            self.proxies.clear()

    def show_error(self, message):
        self.message.setText(message)

    def _refresh_layers(self):
        self.layers_status.setText(f"已加载 {len(self.proxies.layers)} 个远端代理图层")

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

        def complete(_result, error):
            if error:
                self.show_error(error)
            else:
                self.show_error("工作站已导入，正在更新代理图层。")
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

    def shutdown(self):
        self.iface.unregisterCustomDropHandler(self.drop_handler)
        self.proxies.clear()
