# SPDX-License-Identifier: GPL-3.0-or-later

from uuid import uuid4

from qgis.core import (
    Qgis, QgsApplication, QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingFeatureSourceDefinition,
    QgsProcessingOutputString, QgsProcessingParameterString, QgsProcessingProvider, QgsProperty,
)
from qgis.PyQt.QtCore import QEventLoop, QThread, QTimer

from ..common.processing import LAYER_TYPES, RASTER, REPORT, TOOLS

END_STATES = {"finished", "failed", "canceled"}


class RemoteToolbox(QgsProcessingProvider):
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.catalog = ()
        self.connection.status_changed.connect(self.status)
        QgsApplication.processingRegistry().addProvider(self)

    def id(self):
        return "remoteqgis"

    def name(self):
        return "RemoteQGIS 工作站"

    def loadAlgorithms(self):
        registry = QgsApplication.processingRegistry()
        for command in self.catalog:
            if (native := registry.algorithmById(command)):
                self.addAlgorithm(RemoteAlgorithm(self.connection, command, native))

    def status(self, state, _detail):
        if state == "connected":
            self.connection.call("processing_catalog", {}, self.update_tools)

    def update_tools(self, data, error):
        if not error and self.connection.ready:
            catalog = tuple(item for item in data["algorithms"] if item in TOOLS)
            if catalog != self.catalog:
                self.catalog = catalog
                self.refreshAlgorithms()

    def close(self):
        self.connection.status_changed.disconnect(self.status)
        QgsApplication.processingRegistry().removeProvider(self)


class RemoteAlgorithm(QgsProcessingAlgorithm):
    def __init__(self, connection, command, native):
        super().__init__()
        self.connection = connection
        self.command = command
        self.native = native

    def name(self):
        return self.command.replace(":", "_")

    def displayName(self):
        return self.native.displayName()

    def group(self):
        return self.native.group()

    def groupId(self):
        return self.native.provider().id() + "_" + self.native.groupId()

    def flags(self):
        return super().flags() | self.FlagNoThreading | Qgis.ProcessingAlgorithmFlag.HideFromModeler

    def shortHelpString(self):
        description = self.native.shortHelpString() or ""
        return ("选择已加载的远端代理图层。输出路径相对于工作站共享目录。"
                "服务器完成后会同步结果。\n\n" + description)

    def createInstance(self):
        return RemoteAlgorithm(self.connection, self.command, self.native)

    def initAlgorithm(self, config=None):
        allowed = set(TOOLS[self.command].split())
        for definition in self.native.parameterDefinitions():
            if definition.name() in allowed:
                cloned = definition.clone()
                cloned.setIsDynamic(False)
                self.addParameter(cloned)
        if self.command not in REPORT:
            extension = ".tif" if self.command in RASTER else ".gpkg"
            self.addParameter(QgsProcessingParameterString(
                "REMOTE_OUTPUT", f"工作站输出相对路径（{extension}）"
            ))
            self.addOutput(QgsProcessingOutputString("REMOTE_OUTPUT", "工作站保存路径"))
        self.addOutput(QgsProcessingOutputString("REMOTE_RESULTS", "计算摘要"))

    def processAlgorithm(self, parameters, context, feedback):
        if QThread.currentThread() != self.connection.thread() or not self.connection.ready:
            raise QgsProcessingException("请先连接工作站，并从 QGIS Processing 工具箱运行。")
        serialized = {}
        for definition in self.parameterDefinitions():
            name = definition.name()
            value = parameters.get(name)
            if name == "REMOTE_OUTPUT" or value is None or value == "":
                continue
            kind = definition.type()
            if kind in LAYER_TYPES:
                if isinstance(value, QgsProcessingFeatureSourceDefinition):
                    if value.selectedFeaturesOnly or value.featureLimit >= 0 or value.filterExpression:
                        raise QgsProcessingException("仅可使用完整的远端图层。")
                    parameters = dict(parameters, **{name: value.source.staticValue()})
                if kind == "multilayer":
                    layers = self.parameterAsLayerList(parameters, name, context)
                    serialized[name] = [self.layer_key(layer) for layer in layers]
                else:
                    serialized[name] = self.layer_key(self.parameterAsLayer(parameters, name, context))
            elif kind == "crs":
                crs = self.parameterAsCrs(parameters, name, context)
                if crs.isValid():
                    serialized[name] = crs.authid() or crs.toWkt()
            elif kind == "extent":
                extent = self.parameterAsExtent(parameters, name, context)
                crs = self.parameterAsExtentCrs(parameters, name, context)
                if not extent.isEmpty():
                    xmin, xmax = extent.xMinimum(), extent.xMaximum()
                    ymin, ymax = extent.yMinimum(), extent.yMaximum()
                    serialized[name] = f"{xmin},{xmax},{ymin},{ymax} [{crs.authid()}]" if crs.authid() else f"{xmin},{xmax},{ymin},{ymax}"
            elif kind == "matrix":
                serialized[name] = self.parameterAsMatrix(parameters, name, context)
            elif isinstance(value, QgsProperty):
                raise QgsProcessingException("动态参数与图层表达式参数需要在工作站运行。")
            else:
                serialized[name] = value
        job = uuid4().hex
        request = {"job": job, "algorithm": self.command, "parameters": serialized}
        if self.command not in REPORT:
            request["output"] = self.parameterAsString(parameters, "REMOTE_OUTPUT", context).strip()
        state = self.request("processing_start", request, feedback)
        while state["state"] not in END_STATES and not feedback.isCanceled():
            feedback.setProgress(state["progress"])
            delay = QEventLoop()
            QTimer.singleShot(350, delay.quit)
            delay.exec()
            if not feedback.isCanceled():
                state = self.request("processing_status", {"job": job}, feedback)
        if feedback.isCanceled():
            self.connection.call("processing_cancel", {"job": job}, lambda *_: None)
            return {}
        if state["log"]:
            feedback.pushInfo(state["log"])
        if state["state"] != "finished":
            detail = state["reason"] or "工作站取消了任务。"
            raise QgsProcessingException((detail + "\n" + state["log"][-1800:]).strip())
        results = state["result"]
        feedback.setProgress(100)
        feedback.pushInfo(str(results))
        output = {"REMOTE_RESULTS": str(results)}
        if "output" in results:
            output["REMOTE_OUTPUT"] = results["output"]
        return output

    @staticmethod
    def layer_key(layer):
        key = layer.customProperty("remoteqgis/id") if layer is not None else None
        if not key or layer.customProperty("remoteqgis/online", False):
            raise QgsProcessingException("请选择由工作站共享目录导入的远端数据。")
        return key

    def request(self, operation, arguments, feedback):
        loop = QEventLoop()
        timer = QTimer(interval=100)
        timer.timeout.connect(lambda: loop.quit() if feedback.isCanceled() else None)
        results = []

        def done(data, error):
            results.append((data, error))
            loop.quit()

        self.connection.call(operation, arguments, done)
        if not results and not feedback.isCanceled():
            timer.start()
            loop.exec()
            timer.stop()
        if feedback.isCanceled():
            return {"state": "canceling"}
        data, error = results[0]
        if error:
            raise QgsProcessingException(error)
        return data
