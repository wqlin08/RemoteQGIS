# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from hashlib import sha256
import json
import math

from qgis.core import (
    QgsProject, QgsRasterLayer, QgsVectorLayer, QgsMultiBandColorRenderer,
    QgsSingleBandGrayRenderer, QgsContrastEnhancement,
    QgsProviderRegistry, QgsWkbTypes,
)

from ..common.workspace import PROJECTS, RASTERS, VECTORS, WorkspaceError
from .files import SharedFiles
from .vectors import snapshot
from .rasters import RESAMPLING, file_stamp


def bounds(rectangle):
    return [rectangle.xMinimum(), rectangle.yMinimum(), rectangle.xMaximum(), rectangle.yMaximum()]


class HostWorkspace:
    def __init__(self, root: Path, canvas):
        self.files = SharedFiles(root)
        self.project = QgsProject.instance()
        self.canvas = canvas
        self.revision = 0
        self._fingerprint = None
        self.rasters = {}

    def execute(self, operation: str, data: dict):
        operations = {
            "files": self.list_files, "import": self.import_files, "state": self.state,
            "vector": self.vector, "render": self.render, "mkdir": self.mkdir, "rename": self.rename, "delete": self.delete,
        }
        handler = operations.get(operation)
        if handler is None:
            raise WorkspaceError("工作站不支持此操作。", 404)
        try:
            return handler(data)
        except OSError as exc:
            raise WorkspaceError("工作站无法完成文件操作。", 409) from exc

    def list_files(self, data):
        return {"entries": self.files.list(data.get("path", ""))}

    def mkdir(self, data):
        path = self.files.path(data.get("path"), exists=False)
        path.mkdir()
        return {}

    def _busy(self, path):
        for layer in self.project.mapLayers().values():
            source = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source()).get("path")
            if source:
                resolved = Path(source).resolve()
                if resolved == path or resolved.is_relative_to(path):
                    raise WorkspaceError("请先从工作站项目移除正在使用的图层。", 409)
        if self.project.fileName():
            project_path = Path(self.project.fileName()).resolve()
            if project_path == path or project_path.is_relative_to(path):
                raise WorkspaceError("当前打开的项目文件正在使用。", 409)

    def rename(self, data):
        path = self.files.path(data.get("path"))
        target = self.files.path(data.get("target"), exists=False)
        if path.parent != target.parent or path.suffix.lower() != target.suffix.lower():
            raise WorkspaceError("重命名须保留目录和文件扩展名。")
        self._busy(path)
        self.files.move(path, target)
        return {}

    def delete(self, data):
        if data.get("confirmed") is not True:
            raise WorkspaceError("删除操作需要确认。")
        path = self.files.path(data.get("path"))
        self._busy(path)
        self.files.recycle(path)
        return {}

    def import_files(self, data):
        paths = data.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 32:
            raise WorkspaceError("请选择 1–32 个远端文件。")
        if any(not isinstance(path, str) for path in paths):
            raise WorkspaceError("远端路径无效。")
        layers = []
        for relative in dict.fromkeys(paths):
            path = self.files.path(relative)
            suffix = path.suffix.lower()
            if not path.is_file() or suffix not in RASTERS | VECTORS:
                raise WorkspaceError("仅可导入 GeoTIFF、GeoPackage 和 Shapefile。")
            list(self.files.parts(path))
            if suffix in RASTERS:
                new = [QgsRasterLayer(str(path), path.stem, "gdal")]
            elif suffix == ".gpkg":
                details = QgsProviderRegistry.instance().querySublayers(str(path))
                new = [
                    QgsVectorLayer(detail.uri(), detail.name(), "ogr")
                    for detail in details if detail.providerKey() == "ogr"
                ]
            else:
                new = [QgsVectorLayer(str(path), path.stem, "ogr")]
            if not new or any(not layer.isValid() for layer in new):
                raise WorkspaceError(f"无法加载数据 {path.name}。")
            layers.extend(new)
        self.project.addMapLayers(layers)
        return self.state({})

    def source(self, layer):
        if layer.providerType() not in ("ogr", "gdal"):
            return None
        uri = QgsProviderRegistry.instance().decodeUri(layer.providerType(), layer.source())
        source = uri.get("path")
        if not source:
            return None
        path = Path(source)
        suffix = path.suffix.lower()
        supported = suffix in RASTERS if isinstance(layer, QgsRasterLayer) else suffix in VECTORS
        if not supported or not path.is_absolute() or not path.is_relative_to(self.files.root):
            return None
        try:
            return self.files.path(self.files.relative(path)), uri.get("layerName", "")
        except WorkspaceError:
            return None

    def vector(self, data):
        return snapshot(self.layer(data, QgsVectorLayer))

    def layer(self, data, cls):
        identifier = data.get("id")
        layer = self.project.mapLayer(identifier) if isinstance(identifier, str) else None
        if not isinstance(layer, cls) or self.source(layer) is None:
            raise WorkspaceError("工作站图层已移除或不在共享目录内。", 404)
        return layer

    def describe(self, layer):
        source = self.source(layer)
        if source is None:
            return None
        path, table = source
        raster = isinstance(layer, QgsRasterLayer)
        node = self.project.layerTreeRoot().findLayer(layer.id())
        metadata = {
            "id": layer.id(), "name": layer.name(), "kind": "raster" if raster else "vector",
            "path": self.files.relative(path), "table": table or "", "crs": layer.crs().authid(),
            "wkt": layer.crs().toWkt(), "bounds": bounds(layer.extent()),
            "visible": node.itemVisibilityChecked() if node else True,
            "bands": layer.bandCount() if raster else 0,
            "geometry": "" if raster else QgsWkbTypes.displayString(layer.wkbType()),
            "fields": [] if raster else [field.name() for field in layer.fields()],
        }

        if raster:
            spec = self.raster_spec(layer, path)
            metadata.update(tile_version=spec["version"], render={key: spec[key] for key in
                            ("bands", "stretch", "resample", "nodata")})
        return metadata

    def raster_spec(self, layer, path):
        renderer = layer.renderer()
        bands = [1, 2, 3] if layer.bandCount() >= 3 else [1]
        stretch = []
        if isinstance(renderer, QgsMultiBandColorRenderer):
            chosen = [renderer.redBand(), renderer.greenBand(), renderer.blueBand()]
            if all(band > 0 for band in chosen):
                bands = chosen
                stretch = [renderer.redContrastEnhancement(), renderer.greenContrastEnhancement(),
                           renderer.blueContrastEnhancement()]
        elif isinstance(renderer, QgsSingleBandGrayRenderer):
            bands = [renderer.grayBand()]
            stretch = [renderer.contrastEnhancement()]
        stretch = [(item.minimumValue(), item.maximumValue()) for item in stretch] if all(stretch) else []
        if any(not math.isfinite(lo) or not math.isfinite(hi) or lo >= hi for lo, hi in stretch):
            stretch = []
        spec = {
            "path": str(path), "stamp": file_stamp(path), "bands": tuple(bands), "stretch": tuple(stretch),
            "resample": layer.customProperty("remoteqgis/resample", "bilinear"),
            "nodata": layer.customProperty("remoteqgis/nodata", None),
        }
        spec["version"] = sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:24]
        return spec

    def render(self, data):
        layer = self.layer(data, QgsRasterLayer)
        bands = data.get("bands")
        stretch = data.get("stretch", [])
        resample = data.get("resample", "bilinear")
        nodata = data.get("nodata")
        if not isinstance(bands, list) or len(bands) not in (1, 3) or any(
            type(band) is not int or not 1 <= band <= layer.bandCount() for band in bands
        ):
            raise WorkspaceError("请选择一个灰度波段或三个 RGB 波段。")
        if not isinstance(stretch, list) or stretch and (
            len(stretch) != len(bands) or any(
                not isinstance(pair, list) or len(pair) != 2
                or any(type(value) not in (int, float) or not math.isfinite(value) for value in pair)
                or pair[0] >= pair[1] for pair in stretch
            )
        ):
            raise WorkspaceError("每个波段的拉伸下限须小于上限。")
        if resample not in RESAMPLING or nodata is not None and (
            type(nodata) not in (int, float) or not math.isfinite(nodata)
        ):
            raise WorkspaceError("重采样方式或 NoData 值无效。")
        provider = layer.dataProvider()
        renderer = (QgsSingleBandGrayRenderer(provider, bands[0]) if len(bands) == 1
                    else QgsMultiBandColorRenderer(provider, *bands))
        setters = ([renderer.setContrastEnhancement] if len(bands) == 1 else [
            renderer.setRedContrastEnhancement, renderer.setGreenContrastEnhancement,
            renderer.setBlueContrastEnhancement,
        ])
        for band, pair, setter in zip(bands, stretch, setters):
            enhance = QgsContrastEnhancement(provider.dataType(band))
            enhance.setContrastEnhancementAlgorithm(QgsContrastEnhancement.StretchToMinimumMaximum)
            enhance.setMinimumValue(pair[0])
            enhance.setMaximumValue(pair[1])
            setter(enhance)
        layer.setRenderer(renderer)
        layer.setCustomProperty("remoteqgis/resample", resample)
        if nodata is None:
            layer.removeCustomProperty("remoteqgis/nodata")
        else:
            layer.setCustomProperty("remoteqgis/nodata", nodata)
        layer.triggerRepaint()
        return {}

    def state(self, data):
        layers = [self.describe(node.layer()) for node in self.project.layerTreeRoot().findLayers() if node.layer()]
        return {"layers": [layer for layer in layers if layer is not None]}

    def refresh(self):
        rasters = {}
        for layer in self.project.mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and self.source(layer):
                try:
                    rasters[layer.id()] = self.raster_spec(layer, self.source(layer)[0])
                except OSError:
                    continue
        self.rasters = rasters
        fingerprint = (
            tuple((key, spec["version"]) for key, spec in sorted(rasters.items())),
            self.project.fileName(), self.project.title(),
            tuple((node.layerId(), node.name(), node.itemVisibilityChecked())
                  for node in self.project.layerTreeRoot().findLayers()),
        )
        if fingerprint != self._fingerprint:
            self._fingerprint = fingerprint
            self.revision += 1
        return self.revision
