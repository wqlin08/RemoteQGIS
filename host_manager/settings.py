# SPDX-License-Identifier: GPL-3.0-or-later

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

from remote_workspace.common.workspace import PROJECTS
from remote_workspace.host.config import HostConfig
from remote_workspace.host.files import SharedFiles

ACCOUNT = "workstation"
LOCAL_CONTROL = "workstation-local-control"
TITLE = "RemoteQGIS Host Manager"


def directory():
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/RemoteQGIS Host"
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "RemoteQGIS Host"


@dataclass(frozen=True)
class Settings:
    address: str = "127.0.0.1"
    port: int = 8765
    manager_port: int = 8766
    shared_directory: str = ""
    project: str = ""
    allow_preview: bool = False
    auto_start: bool = False

    @classmethod
    def read(cls, path):
        if not path.exists():
            return cls()
        return cls(**json.loads(path.read_text(encoding="utf-8")))

    def validate(self, token):
        work = HostConfig.validate(self.address, self.port, self.shared_directory, token)
        if not 1 <= self.manager_port <= 65535 or self.manager_port == self.port:
            raise ValueError("管理端口须在 1–65535 之间，并与 GIS 端口不同。")
        if self.project:
            path = SharedFiles(work.shared_directory).path(self.project)
            if path.suffix.lower() not in PROJECTS or not path.is_file():
                raise ValueError("启动项目须为共享目录内的 .qgs 或 .qgz 文件。")
        return work

    def write(self, path):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
                temp = Path(file.name)
                json.dump(asdict(self), file, ensure_ascii=False, indent=2)
            temp.replace(path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
