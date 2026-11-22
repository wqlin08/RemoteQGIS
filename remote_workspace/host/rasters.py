# SPDX-License-Identifier: GPL-3.0-or-later

import math
from pathlib import Path

import numpy as np
from osgeo import gdal

from ..common.workspace import WorkspaceError

HALF_WORLD = 20037508.342789244
TILE_SIZE = 256
MAX_ZOOM = 22


def file_stamp(path):
    paths = (Path(path), Path(str(path) + ".ovr"), Path(str(path) + ".msk"), Path(str(path) + ".aux.xml"))
    return tuple((item.stat().st_mtime_ns, item.stat().st_size) if item.exists() else (0, 0)
                 for item in paths)


def tile_bounds(z, x, y):
    step = 2 * HALF_WORLD / (1 << z)
    west, north = -HALF_WORLD + x * step, HALF_WORLD - y * step
    return west, north - step, west + step, north


def dataset(path, stamp):
    for suffix in ("", ".ovr", ".msk", ".aux.xml"):
        member = Path(path + suffix)
        if member.is_symlink() or member.resolve() != member:
            raise WorkspaceError("影像数据源或副文件路径已变更。", 409)
    if file_stamp(path) != stamp:
        raise WorkspaceError("影像已更新，请刷新图层。", 409)
    source = gdal.OpenEx(path, gdal.OF_RASTER | gdal.OF_READONLY, allowed_drivers=["GTiff"])
    if source is None:
        raise WorkspaceError("无法读取 GeoTIFF。", 422)
    return source


def categories(spec, number):
    source = dataset(spec["path"], spec["stamp"])
    if type(number) is not int or not 1 <= number <= source.RasterCount:
        raise WorkspaceError("请选择有效波段。")
    band = source.GetRasterBand(number)
    values = band.ReadAsArray(buf_xsize=min(band.XSize, 1024), buf_ysize=min(band.YSize, 1024))
    if values is None or np.iscomplexobj(values):
        raise WorkspaceError("无法采样此波段的类别。", 422)
    valid = np.isfinite(values)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= values != nodata
    if spec["nodata"] is not None:
        valid &= values != spec["nodata"]
    unique = np.unique(values[valid])
    if unique.size > 256:
        raise WorkspaceError("采样值超过 256 种。可手动填写类别，或改用连续伪彩色。", 422)
    return {"values": unique.tolist()}


def sample(spec, x, y):
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in (x, y)):
        raise WorkspaceError("像元查询坐标无效。")
    source = dataset(spec["path"], spec["stamp"])
    inverse = gdal.InvGeoTransform(source.GetGeoTransform())
    if inverse is None:
        raise WorkspaceError("影像变换矩阵无效。", 422)
    col, row = (math.floor(n) for n in gdal.ApplyGeoTransform(inverse, x, y))
    if not 0 <= col < source.RasterXSize or not 0 <= row < source.RasterYSize:
        raise WorkspaceError("坐标超出影像范围。", 404)
    values = []
    for index in range(1, source.RasterCount + 1):
        band = source.GetRasterBand(index)
        value = band.ReadAsArray(col, row, 1, 1).item()
        valid = not isinstance(value, complex) and math.isfinite(value) and value != band.GetNoDataValue()
        values.append(value if valid else None)
    return {"column": col, "row": row, "values": values}
