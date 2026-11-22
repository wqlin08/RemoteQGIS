# SPDX-License-Identifier: GPL-3.0-or-later

import math

from qgis.core import (
    Qgis, QgsColorRampShader, QgsPalettedRasterRenderer, QgsRasterDataProvider,
    QgsRasterRange, QgsRasterShader, QgsSingleBandPseudoColorRenderer,
)
from qgis.PyQt.QtGui import QColor

from ..common.workspace import WorkspaceError

INTERPOLATION = {
    "linear": Qgis.ShaderInterpolationMethod.Linear,
    "discrete": Qgis.ShaderInterpolationMethod.Discrete,
    "exact": Qgis.ShaderInterpolationMethod.Exact,
}
RESAMPLING = {
    "near": QgsRasterDataProvider.ResamplingMethod.Nearest,
    "bilinear": QgsRasterDataProvider.ResamplingMethod.Bilinear,
    "cubic": QgsRasterDataProvider.ResamplingMethod.Cubic,
    "average": QgsRasterDataProvider.ResamplingMethod.Average,
}


def color_settings(renderer):
    if isinstance(renderer, QgsPalettedRasterRenderer):
        entries = renderer.classes()
        return {"mode": "palette", "bands": [renderer.inputBand()], "colors": color_rows(entries)}
    if isinstance(renderer, QgsSingleBandPseudoColorRenderer):
        shader = renderer.shader()
        ramp = shader.rasterShaderFunction() if shader is not None else None
        if isinstance(ramp, QgsColorRampShader):
            method = next(key for key, value in INTERPOLATION.items() if value == ramp.colorRampType())
            return {"mode": "pseudocolor", "bands": [renderer.inputBand()],
                    "colors": color_rows(ramp.colorRampItemList()), "interpolation": method, "clip": ramp.clip()}
    return {}


def color_rows(entries):
    return [[entry.value if math.isfinite(entry.value) else "inf", entry.color.name(QColor.HexArgb), entry.label]
            for entry in entries]


def color_renderer(layer, mode, band, data):
    rows = data.get("colors")
    method = data.get("interpolation", "linear")
    clip = data.get("clip", False)
    if (not isinstance(rows, list) or not 1 <= len(rows) <= 4096
            or not isinstance(method, str) or method not in INTERPOLATION or type(clip) is not bool):
        raise WorkspaceError("请提供有效的色表或颜色分级。")
    entries = {}
    for row in rows:
        if not isinstance(row, list) or len(row) != 3:
            raise WorkspaceError("色表每行须提供像元值、颜色和标签。")
        value, color, label = row
        if value == "inf" and mode == "pseudocolor" and method == "discrete":
            value = float("inf")
        elif type(value) not in (int, float) or not math.isfinite(value):
            raise WorkspaceError("色表像元值无效。")
        if not isinstance(color, str) or not isinstance(label, str) or len(label) > 512 or value in entries:
            raise WorkspaceError("色表包含重复值、无效颜色或过长的标签。")
        color = QColor(color)
        if not color.isValid():
            raise WorkspaceError("色表包含无效颜色。")
        entries[value] = color, label

    provider = layer.dataProvider()
    if mode == "palette":
        return QgsPalettedRasterRenderer(provider, band, [
            QgsPalettedRasterRenderer.Class(value, color, label)
            for value, (color, label) in sorted(entries.items())
        ])
    finite = [value for value in entries if math.isfinite(value)]
    if not finite:
        raise WorkspaceError("伪彩色至少需要一个有限的分级值。")
    minimum, maximum = min(finite), max(finite)
    ramp = QgsColorRampShader(minimum, maximum, None, INTERPOLATION[method])
    ramp.setColorRampItemList([QgsColorRampShader.ColorRampItem(value, color, label)
                               for value, (color, label) in sorted(entries.items())])
    ramp.setClip(clip)
    shader = QgsRasterShader(minimum, maximum)
    shader.setRasterShaderFunction(ramp)
    renderer = QgsSingleBandPseudoColorRenderer(provider, band, shader)
    renderer.setClassificationMin(minimum)
    renderer.setClassificationMax(maximum)
    return renderer


def sampling_options(layer, resample, nodata):
    provider = layer.dataProvider()
    previous = layer.customProperty("remoteqgis/nodata", None)
    if nodata != previous:
        for band in range(1, layer.bandCount() + 1):
            ranges = [value for value in provider.userNoDataValues(band)
                      if previous is None or value.min() != previous or value.max() != previous]
            if nodata is not None:
                ranges.append(QgsRasterRange(nodata, nodata))
            provider.setUserNoDataValue(band, ranges)
    if resample != "current":
        method = RESAMPLING[resample]
        provider.setZoomedInResamplingMethod(method)
        provider.setZoomedOutResamplingMethod(method)
        layer.pipe().setResamplingStage(Qgis.RasterResamplingStage.Provider)
        layer.setCustomProperty("remoteqgis/resample", resample)
    if nodata is None:
        layer.removeCustomProperty("remoteqgis/nodata")
    else:
        layer.setCustomProperty("remoteqgis/nodata", nodata)
