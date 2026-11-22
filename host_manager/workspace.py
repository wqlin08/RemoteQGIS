# SPDX-License-Identifier: GPL-3.0-or-later

from hashlib import sha256
import json
from os.path import normcase
from uuid import uuid4

from remote_workspace.common.workspace import WorkspaceError
from remote_workspace.host.files import SharedFiles
from remote_workspace.host.processing import TERMINAL
from remote_workspace.host.project import HostWorkspace


class ManagedWorkspace(HostWorkspace):
    def __init__(self, authorized, canvas, selection_file, project):
        self.authorized = SharedFiles(authorized)
        self.selection_file = selection_file
        self.selected = ""
        root = authorized
        if selection_file.is_file():
            try:
                selection = json.loads(selection_file.read_text(encoding="utf-8"))
                if selection["authorized"] == str(authorized):
                    root = self.directory(selection["path"])
                    self.selected = selection["path"]
            except (KeyError, OSError, TypeError, ValueError):
                pass
        super().__init__(root, canvas)
        if project:
            startup = self.authorized.path(project)
            if startup.is_relative_to(root):
                self.project_files({"action": "open", "path": self.files.relative(startup), "discard": True})

    def directory(self, path):
        directory = self.authorized.path(path, root=True)
        if not directory.is_dir():
            raise WorkspaceError("授权范围内请选择目录。")
        return directory

    def scope(self):
        return sha256(normcase(str(self.files.root)).encode()).hexdigest() if self.selected else ""

    def describe(self, layer):
        descriptor = super().describe(layer)
        if descriptor and descriptor["kind"] == "vector" and self.selected:
            descriptor["workspace_scope"] = self.scope()
        return descriptor

    def execute(self, operation, data):
        if operation == "workspace_directories":
            path = data.get("path", "")
            self.directory(path)
            return {"path": path, "selected": self.selected, "entries": [
                entry for entry in self.authorized.list(path) if entry["kind"] == "directory"
            ]}
        if operation == "edit" and data.get("workspace_scope", "") != self.scope():
            raise WorkspaceError("此修改属于另一工作空间，请切换回原工作空间再提交。", 409)
        if operation != "workspace_select":
            return super().execute(operation, data)
        path = data.get("path", "")
        directory = self.directory(path)
        if directory == self.files.root:
            return {"changed": False}
        if any(job["state"] not in TERMINAL for job in self.processing.jobs.values()):
            raise WorkspaceError("请等待远端处理任务结束，再切换工作空间。", 409)
        self.project_files({"action": "close"})
        temporary = self.selection_file.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps({"authorized": str(self.authorized.root), "path": path},
                                            ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.selection_file)
        except OSError as exc:
            raise WorkspaceError("无法保存工作空间选择。", 409) from exc
        finally:
            temporary.unlink(missing_ok=True)
        self.files.root = directory
        self.selected = path
        self.session = uuid4().hex
        self._document = None
        self._history.clear()
        return {"changed": True}
