# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path, PureWindowsPath
from uuid import uuid4

from ..common.workspace import PROJECTS, RASTERS, VECTORS, WorkspaceError

TRASH = ".remoteqgis-trash"
SHAPEFILE_PARTS = (".shp", ".shx", ".dbf", ".prj", ".qpj", ".cpg", ".qix", ".sbn", ".sbx", ".shp.xml")
AUXILIARY_PARTS = (".aux.xml", ".ovr", ".msk", "-wal", "-shm")


class SharedFiles:
    def __init__(self, root: Path):
        self.root = root

    def path(self, relative: str, *, exists: bool = True, root: bool = False) -> Path:
        if not isinstance(relative, str) or len(relative) > 4096:
            raise WorkspaceError("远端路径无效。")
        if relative == "" and root:
            return self.root
        parts = relative.split("/")
        if any(
            part in ("", ".", "..") or part.startswith(".") or part.endswith((" ", "."))
            or any(ord(c) < 32 or c in '\\:*?"<>|' for c in part)
            or PureWindowsPath(part).is_reserved()
            for part in parts
        ):
            raise WorkspaceError("请输入共享目录下的相对路径。")
        try:
            path = self.root
            for part in parts:
                path = path / part
                if path.is_symlink() or path.resolve().parent != path.parent:
                    raise WorkspaceError("远端路径包含符号链接或目录重解析点。")
            if not path.resolve().is_relative_to(self.root) or path.resolve() != path:
                raise WorkspaceError("远端路径超出共享目录。")
        except (OSError, RuntimeError) as exc:
            raise WorkspaceError("无法解析远端路径。") from exc
        if exists and not path.exists():
            raise WorkspaceError("远端路径不存在。", 404)
        return path

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def list(self, relative: str):
        directory = self.path(relative, root=True)
        if not directory.is_dir():
            raise WorkspaceError("请选择远端目录。")
        entries = []
        for entry in directory.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                self.path(self.relative(entry))
            except WorkspaceError:
                continue
            suffix = entry.suffix.lower()
            if entry.is_dir():
                kind = "directory"
            elif entry.is_file() and suffix in RASTERS | VECTORS | PROJECTS:
                kind = "raster" if suffix in RASTERS else "vector" if suffix in VECTORS else "project"
            else:
                continue
            entries.append({"name": entry.name, "kind": kind, "path": self.relative(entry)})
        return sorted(entries, key=lambda entry: (entry["kind"] != "directory", entry["name"].casefold()))

    def parts(self, path: Path):
        """Include GDAL/QGIS companions in checks, backups and file moves."""
        for item in path.parent.iterdir():
            if path.suffix.lower() == ".shp":
                matches = any(item.name.casefold() == path.stem.casefold() + part for part in SHAPEFILE_PARTS)
            else:
                matches = item == path or any(
                    item.name.casefold() == path.name.casefold() + part for part in AUXILIARY_PARTS
                )
            if matches:
                yield self.path(self.relative(item))

    def ensure_free(self, path: Path):
        if path.exists() or any(self.parts(path)):
            raise WorkspaceError("目标路径已存在。", 409)

    def recycle(self, path: Path):
        trash = self.root / TRASH
        if trash.is_symlink() or trash.resolve() != trash:
            raise WorkspaceError("共享目录内的回收目录无效。")
        output = trash / uuid4().hex
        output.mkdir(parents=True)
        self.move(path, output / path.name)

    def move(self, path: Path, target: Path):
        if path.is_dir():
            self.ensure_free(target)
            path.rename(target)
            return
        members = list(self.parts(path))
        moves = []
        for part in members:
            if part == path:
                new = target
            elif path.suffix.lower() == ".shp":
                new = target.with_name(target.stem + part.name[len(path.stem):])
            else:
                new = target.with_name(target.name + part.name[len(path.name):])
            if new.exists():
                raise WorkspaceError("目标路径已存在。", 409)
            moves.append((part, new))
        completed = []
        try:
            for old, new in moves:
                old.rename(new)
                completed.append((old, new))
        except OSError:
            for old, new in reversed(completed):
                new.rename(old)
            raise
