# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from uuid import uuid4

from qgis.core import QgsApplication, QgsDefaultValue, QgsEditorWidgetSetup
from qgis.PyQt.QtCore import QTimer

from ..common.edits import IDENTITY, Recovery, edit_delta
from ..common.vector import field_data, geometry_data, pack
from ..common.workspace import MAX_FEATURES


class ClientEdits:
    def __init__(self, proxies):
        self.proxies = proxies
        self.connection = proxies.connection
        self.recovery = Recovery(Path(QgsApplication.qgisSettingsDirPath()) / "remoteqgis" / "pending")
        self.records = {}
        self.in_flight = set()
        self._scheduled = set()
        self._closed = False
        self._unjournaled = set()
        self._ids = {}

    def path(self, descriptor):
        return self.recovery.path(self.proxies.origin, descriptor)

    def restore(self, descriptor, document):
        try:
            record = self.recovery.read(self.path(descriptor))
        except (ValueError, OSError) as exc:
            self.proxies.failure(f"无法读取该图层的本地恢复记录（{type(exc).__name__}）。")
            record = None
        return record["working"] if record is not None else document, record

    def attach(self, key, layer, base, record):
        self.records[key] = (layer, base)
        index = layer.fields().indexFromName(IDENTITY)
        layer.setDefaultValueDefinition(index, QgsDefaultValue("uuid('WithoutBraces')"))
        layer.setEditorWidgetSetup(index, QgsEditorWidgetSetup("Hidden", {}))
        table_config = layer.attributeTableConfig()
        columns = table_config.columns()
        for column in columns:
            if column.name == IDENTITY:
                column.hidden = True
        table_config.setColumns(columns)
        layer.setAttributeTableConfig(table_config)
        self._ids[key] = {feature.id(): feature[index] for feature in layer.getFeatures()}
        layer.setReadOnly(record is not None)
        layer.setCustomProperty("remoteqgis/pending", record is not None)
        layer.beforeCommitChanges.connect(lambda stop: self.capture(key, layer))
        layer.afterCommitChanges.connect(lambda: self.schedule(key))
        if record is not None:
            self.schedule(key)

    def detach(self, key):
        self.records.pop(key, None)
        self._scheduled.discard(key)
        self._unjournaled.discard(key)
        self._ids.pop(key, None)

    def capture(self, key, layer):
        if self._closed or key not in self.records or self.records[key][0] is not layer:
            return
        descriptor = self.proxies.descriptors[key]
        base = self.records[key][1]
        try:
            pending = self.recovery.read(self.path(descriptor))
            if pending is not None:
                return
            field_index = layer.fields().indexFromName(IDENTITY)
            indices = [field for field in range(len(layer.fields())) if field != field_index]
            working = dict(base, fields=[field_data(layer.fields()[i]) for i in indices], features=[])
            for feature in layer.getFeatures():
                if len(working["features"]) >= MAX_FEATURES:
                    raise ValueError(f"当前代理最多保存 {MAX_FEATURES} 个要素。")
                key_text = self._ids[key].get(feature.id()) or str(uuid4())
                working["features"].append({
                    "id": key_text, "geometry": geometry_data(feature.geometry()),
                    "values": [pack(feature[i]) for i in indices],
                })
            patch = edit_delta(base, working)
            if working["fields"] == base["fields"] and not any(patch[field] for field in ("add", "update", "delete")):
                self._unjournaled.discard(key)
                return
            patch["dataset"] = [descriptor["path"], descriptor["table"]]
            self.recovery.store(self.path(descriptor), {"patch": patch, "working": working})
            self._unjournaled.discard(key)
            self.proxies.changed()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._unjournaled.add(key)
            self.proxies.failure(f"编辑仍在本地，请处理后重试。{exc}")

    def working_pending(self, key):
        descriptor = self.proxies.descriptors.get(key)
        return bool(descriptor and self.path(descriptor).exists())

    def schedule(self, key):
        if self._closed:
            return
        self._scheduled.add(key)
        QTimer.singleShot(0, self.flush)

    def flush(self):
        if self._closed:
            return
        for key in tuple(self._scheduled):
            self._scheduled.discard(key)
            self.submit(key)

    def retry(self, key):
        entry = self.records.get(key)
        if entry is None:
            self.proxies.failure("请先启用矢量编辑。")
            return
        layer, _ = entry
        if layer.isEditable():
            if not layer.commitChanges():
                self.proxies.failure("QGIS 未能保存本地编辑缓冲区。")
                return
        if key in self._unjournaled:
            self.capture(key, layer)
        self.submit(key)

    def submit(self, key):
        if self._closed or key in self.in_flight or key not in self.records or not self.working_pending(key):
            return
        layer, _ = self.records[key]
        if layer.isEditable():
            if layer.isModified():
                return
            layer.rollBack()
        layer.setReadOnly(True)
        layer.setCustomProperty("remoteqgis/pending", True)
        self.proxies.changed()
        if not self.connection.ready:
            self.proxies.failure("编辑已保存在本机，正在等待工作站重连。")
            return
        descriptor = self.proxies.descriptors[key]
        path = self.path(descriptor)
        try:
            pending = self.recovery.read(path)
            if pending is None:
                return
            payload = dict(pending["patch"], id=key)
        except (ValueError, OSError, KeyError) as exc:
            self.proxies.failure(f"本地事务记录无法读取（{type(exc).__name__}）。")
            return
        self.in_flight.add(key)
        origin = self.proxies.origin

        def completed(document, error):
            self.in_flight.discard(key)
            if error:
                if not self._closed:
                    self.proxies.failure(error)
                return
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self.proxies.failure("工作站已保存，但本地恢复记录未能删除。")
                return
            if not self._closed and self.proxies.origin == origin and key in self.proxies.descriptors:
                self.proxies.install_vector(key, document)
                self.proxies.refresh()

        self.connection.call("edit", payload, completed)

    def has_unrecorded(self):
        if self._unjournaled:
            return True
        for key, layer in self.proxies._local_layers().items():
            if key in self.records and layer.isModified() and not self.working_pending(key):
                return True
        return False

    def has_unsaved(self):
        return self.has_unrecorded() or any(self.working_pending(key) for key in self.records)

    def close(self):
        self._closed = True
        self._scheduled.clear()
