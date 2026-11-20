# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from qgis.core import QgsProviderRegistry, QgsVectorLayer

from ..common.workspace import PROJECTS, RASTERS, VECTORS, WorkspaceError


class ProjectFiles:
    def __init__(self, files, project, canvas):
        self.files, self.project, self.canvas = files, project, canvas

    def __call__(self, data):
        action = data.get("action")
        if action in ("create", "open", "close"):
            if any(isinstance(layer, QgsVectorLayer) and layer.isEditable()
                   for layer in self.project.mapLayers().values()):
                raise WorkspaceError("请先在工作站结束矢量编辑。", 409)
            if self.project.isDirty() and data.get("discard") is not True:
                raise WorkspaceError("当前工作站项目有改动，请先保存或确认放弃。", 409)

        if action == "close":
            self.project.clear()
            return {}
        if action == "save":
            if not self.project.fileName():
                raise WorkspaceError("当前项目尚无保存路径，请使用另存为。")
            current = Path(self.project.fileName()).absolute()
            if not current.is_relative_to(self.files.root):
                raise WorkspaceError("当前项目不在共享目录内，请使用另存为。")
            path = self.path(self.files.relative(current), exists=False)
        elif action in ("create", "save_as", "open"):
            path = self.path(data.get("path"), exists=action == "open")
            if action != "open":
                self.files.ensure_free(path)
        else:
            raise WorkspaceError("项目操作无效。")
        if action == "open":
            self.validate_sources(path)
            if not self.project.read(str(path)):
                raise WorkspaceError("无法打开工作站项目。", 422)
            extent = self.project.viewSettings().defaultViewExtent()
            if not extent.isEmpty():
                self.canvas.setExtent(extent)
            self.canvas.refresh()
        else:
            if action == "create":
                self.project.clear()
            self.project.viewSettings().setDefaultViewExtent(self.default_extent())
            old_path = self.project.fileName()
            if not self.project.write(str(path)):
                self.project.setFileName(old_path)
                raise WorkspaceError("无法写入工作站项目。", 409)
        return {}

    def default_extent(self):
        from qgis.core import QgsReferencedRectangle

        return QgsReferencedRectangle(self.canvas.extent(), self.canvas.mapSettings().destinationCrs())

    def path(self, relative, exists):
        path = self.files.path(relative, exists=exists)
        if path.suffix.lower() not in PROJECTS or not path.parent.is_dir():
            raise WorkspaceError("请选择共享目录下的 .qgz 或 .qgs 项目文件。")
        return path

    def validate_sources(self, path):
        limit = 32 * 1024 * 1024
        try:
            if path.suffix.lower() == ".qgz":
                with ZipFile(path) as archive:
                    members = [member for member in archive.infolist() if member.filename.endswith(".qgs")]
                    if len(members) != 1 or members[0].file_size > limit:
                        raise ValueError
                    content = archive.read(members[0])
            else:
                with path.open("rb") as file:
                    content = file.read(limit + 1)
            if len(content) > limit or b"<!ENTITY" in content.upper():
                raise ValueError
            project = ElementTree.fromstring(content)
            if project.tag != "qgis":
                raise ValueError
        except (OSError, ValueError, ElementTree.ParseError, BadZipFile) as exc:
            raise WorkspaceError("项目文件无效或超出读取限制。") from exc
        for layer in project.findall("./projectlayers/maplayer"):
            provider, source = layer.findtext("provider"), layer.findtext("datasource")
            uri = QgsProviderRegistry.instance().decodeUri(provider or "", source or "") \
                if provider in ("ogr", "gdal") else {}
            if not uri.get("path"):
                raise WorkspaceError("远程打开的项目仅支持共享目录内的 GeoTIFF、GeoPackage 和 Shapefile。")
            candidate = Path(uri["path"])
            if not candidate.is_absolute():
                candidate = path.parent / candidate
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.files.root) or resolved.suffix.lower() not in RASTERS | VECTORS:
                raise WorkspaceError("项目引用了共享目录之外或暂不支持的数据源。")
            checked = self.files.path(self.files.relative(resolved))
            list(self.files.parts(checked))
