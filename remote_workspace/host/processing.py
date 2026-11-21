# SPDX-License-Identifier: GPL-3.0-or-later

from contextlib import suppress
from pathlib import Path
import math
import re
import shutil
from uuid import UUID

from qgis.core import (
    QgsApplication, QgsCoordinateReferenceSystem, QgsExpression, QgsProcessingAlgRunnerTask,
    QgsProcessingAlgorithm, QgsProcessingContext, QgsProcessingFeedback, QgsRasterLayer, QgsVectorLayer,
)

from ..common.processing import LAYER_TYPES, RASTER, REPORT, TOOLS
from ..common.workspace import WorkspaceError

TERMINAL = {"finished", "failed", "canceled"}
SAFE_FUNCTIONS = {
    "abs", "round", "sqrt", "floor", "ceil", "ln", "log", "log10", "exp", "min", "max",
    "sin", "cos", "tan", "asin", "acos", "atan", "atan2", "coalesce", "if", "area", "perimeter",
    "length", "to_int", "to_real", "to_string", "concat", "lower", "upper", "trim", "replace",
}
SHELL_CHARS = set("`$;&|<>^%!\n\r\"")


class RemoteProcessing:
    def __init__(self, workspace):
        self.workspace = workspace
        self.files = workspace.files
        self.jobs = {}

    @staticmethod
    def catalog(_):
        registry = QgsApplication.processingRegistry()
        return {"algorithms": [key for key in TOOLS if (algorithm := registry.algorithmById(key))
                               and not algorithm.flags() & QgsProcessingAlgorithm.FlagNoThreading]}

    def start(self, data):
        key, command, params = data.get("job"), data.get("algorithm"), data.get("parameters")
        try:
            valid = isinstance(key, str) and UUID(key).hex == key
        except ValueError:
            valid = False
        if not valid or not isinstance(command, str) or command not in TOOLS or not isinstance(params, dict):
            raise WorkspaceError("计算任务或算法无效。")
        if key in self.jobs:
            if self.jobs[key]["request"] != data:
                raise WorkspaceError("任务标识已被使用。", 409)
            return self.status({"job": key})
        if sum(job["state"] not in TERMINAL for job in self.jobs.values()) >= 2:
            raise WorkspaceError("工作站最多同时运行两个任务。", 409)
        if len(self.jobs) >= 32:
            self.jobs.pop(next(key for key, job in self.jobs.items() if job["state"] in TERMINAL))
        algorithm = QgsApplication.processingRegistry().algorithmById(command)
        if not algorithm or algorithm.flags() & QgsProcessingAlgorithm.FlagNoThreading:
            raise WorkspaceError("工作站未启用该算法。", 404)
        context = QgsProcessingContext()
        context.setTransformContext(self.workspace.project.transformContext())
        allowed = set(TOOLS[command].split())
        if params.keys() - allowed:
            raise WorkspaceError("请求含有当前算法不支持的参数。")
        definitions = {definition.name(): definition for definition in algorithm.parameterDefinitions()
                       if definition.name() in allowed}
        prepared, aliases = {}, {}
        for name, definition in definitions.items():
            if name in params:
                prepared[name] = self.parameter(definition, params[name], command, aliases)
        if command == "native:rastercalc" and isinstance(prepared.get("EXPRESSION"), str):
            expression = prepared["EXPRESSION"]
            for name, alias in aliases.items():
                expression = expression.replace(f'"{name}@', f'"{alias}@')
            prepared["EXPRESSION"] = expression
        target = None
        if command not in REPORT:
            target = self.files.path(data.get("output"), exists=False)
            suffix = ".tif" if command in RASTER else ".gpkg"
            if target.suffix.lower() != suffix or not target.parent.is_dir():
                raise WorkspaceError(f"请在共享目录内指定未使用的 {suffix} 输出。")
            self.files.ensure_free(target)
        if any(not definition.checkValueIsAcceptable(prepared.get(name, definition.defaultValue()), context)
               for name, definition in definitions.items()):
            raise WorkspaceError("参数缺失或不符合工作站算法要求。")
        staging = self.files.root / ".remoteqgis-processing"
        if staging.is_symlink() or staging.resolve() != staging:
            raise WorkspaceError("工作站临时目录无效。")
        if command.startswith("gdal:") and any(ch in SHELL_CHARS for ch in str(staging)):
            raise WorkspaceError("工作站共享目录包含 GDAL 命令行不支持的字符。")
        folder = staging / key
        folder.mkdir(parents=True)
        if target is not None:
            prepared["OUTPUT"] = str(folder / ("result" + target.suffix.lower()))
        feedback = QgsProcessingFeedback()
        task = QgsProcessingAlgRunnerTask(algorithm, prepared, context, feedback)
        job = {"request": dict(data), "state": "running", "progress": 0, "result": {},
               "log": "", "reason": "", "task": task, "context": context, "feedback": feedback}
        self.jobs[key] = job

        def finished(success, results):
            self.finished(job, success, results, folder, target)

        task.executed.connect(finished)
        QgsApplication.taskManager().addTask(task)
        return self.status({"job": key})

    def parameter(self, definition, value, command, aliases):
        kind = definition.type()
        if kind in LAYER_TYPES:
            inputs = value if kind == "multilayer" else [value]
            if not isinstance(inputs, list) or not 1 <= len(inputs) <= 32:
                raise WorkspaceError("图层输入无效。")
            ids = []
            for identifier in inputs:
                cls = {"source": QgsVectorLayer, "vector": QgsVectorLayer, "raster": QgsRasterLayer}.get(
                    kind, QgsRasterLayer if command in RASTER else QgsVectorLayer)
                layer = self.workspace.layer({"id": identifier}, cls)
                source = self.workspace.source(layer)[0]
                if command.startswith("gdal:") and any(ch in SHELL_CHARS for ch in layer.source()):
                    raise WorkspaceError("输入路径含有 GDAL 命令行不支持的字符。")
                if isinstance(layer, QgsRasterLayer):
                    if command == "native:rastercalc":
                        if layer.name() in aliases and aliases[layer.name()] != source.stem:
                            raise WorkspaceError("栅格计算的输入图层名称必须唯一。")
                        aliases[layer.name()] = source.stem
                    ids.append(str(source))
                else:
                    if layer.isEditable() or layer.subsetString():
                        raise WorkspaceError("请先保存工作站编辑，并清除输入图层的筛选条件。")
                    ids.append(layer.source())
            return ids if kind == "multilayer" else ids[0]
        if kind == "crs":
            crs = QgsCoordinateReferenceSystem(value) if isinstance(value, str) and len(value) < 512 else None
            if crs is None or not crs.isValid():
                raise WorkspaceError("目标坐标系无效。")
            return crs
        values = value if isinstance(value, list) else [value]
        if len(values) > 1024 or any(type(item) not in (str, bool, int, float) or
                                     isinstance(item, str) and (len(item) > 8192 or "\0" in item) or
                                     type(item) in (int, float) and not math.isfinite(item) for item in values):
            raise WorkspaceError("计算参数类型或长度无效。")
        if command.startswith("gdal:") and any(isinstance(item, str) and any(ch in SHELL_CHARS for ch in item)
                                                for item in values):
            raise WorkspaceError("GDAL 参数含有命令行不支持的字符。")
        if command == "native:fieldcalculator" and kind == "expression":
            if not isinstance(value, str) or QgsExpression(value).hasParserError():
                raise WorkspaceError("字段表达式无效。")
            code = re.sub(r"'[^']*'|\x22[^\x22]*\x22", "", value)
            functions = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", code.lower()))
            if functions - SAFE_FUNCTIONS:
                raise WorkspaceError("此字段计算只开放常用算术、文本及几何函数。")
        return value

    def finished(self, job, success, results, folder, target):
        feedback = job["feedback"]
        job["log"] = feedback.textLog()[-8000:]
        job["progress"] = 100.0 if success else job["progress"]
        try:
            if feedback.isCanceled() or job["state"] == "canceling":
                job["state"] = "canceled"
            elif not success:
                job["state"] = "failed"
                job["reason"] = "工作站算法失败，详见处理日志。"
            else:
                job["result"] = {key: value for key, value in results.items()
                                 if type(value) in (int, float, bool) and math.isfinite(value)}
                if target is not None:
                    source = folder / ("result" + target.suffix.lower())
                    if not source.is_file():
                        raise WorkspaceError("算法未生成有效的输出文件。")
                    self.files.ensure_free(target)
                    source.rename(target)
                    for suffix in (".ovr", ".msk", ".aux.xml"):
                        side = source.with_name(source.name + suffix)
                        if side.is_file():
                            side.rename(target.with_name(target.name + suffix))
                    relative = self.files.relative(target)
                    job["result"]["output"] = relative
                    try:
                        self.workspace.import_files({"paths": [relative]})
                    except WorkspaceError:
                        job["log"] += "\n结果已写入，请从文件浏览器导入。"
                job["state"] = "finished"
        except (OSError, WorkspaceError) as exc:
            job["state"] = "failed"
            job["reason"] = str(exc)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
            for name in ("task", "feedback", "context"):
                job.pop(name, None)

    def status(self, data):
        key = data.get("job")
        job = self.jobs.get(key) if isinstance(key, str) else None
        if job is None:
            raise WorkspaceError("工作站找不到该任务。", 404)
        if job["state"] not in TERMINAL:
            job["progress"] = job["task"].progress()
        return {name: job[name] for name in ("state", "progress", "result", "log", "reason")}

    def cancel(self, data):
        status = self.status(data)
        if status["state"] not in TERMINAL:
            job = self.jobs[data["job"]]
            job["state"] = "canceling"
            job["task"].cancel()
        return self.status(data)

    def close(self):
        for job in self.jobs.values():
            if job["state"] not in TERMINAL:
                job["state"] = "canceling"
                with suppress(RuntimeError):
                    job["task"].cancel()
