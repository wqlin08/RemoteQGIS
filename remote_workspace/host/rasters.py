# SPDX-License-Identifier: GPL-3.0-or-later

from functools import lru_cache
import math
from pathlib import Path
from uuid import uuid4

import numpy as np
from osgeo import gdal

from ..common.workspace import WorkspaceError

HALF_WORLD = 20037508.342789244
TILE_SIZE = 256
MAX_ZOOM = 22
RESAMPLING = ("near", "bilinear", "cubic", "average")


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


@lru_cache(maxsize=128)
def limits(path, stamp, index):
    source = dataset(path, stamp)
    band = source.GetRasterBand(index)
    sample = band.ReadAsArray(buf_xsize=min(256, band.XSize), buf_ysize=min(256, band.YSize))
    valid = np.isfinite(sample)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        valid &= sample != nodata
    values = sample[valid]
    if not values.size:
        return 0.0, 1.0
    minimum, maximum = np.percentile(values, (2, 98))
    if minimum == maximum:
        minimum, maximum = min(0, float(minimum)), max(255, float(maximum))
    return float(minimum), float(maximum)


@lru_cache(maxsize=512)
def render(path, stamp, bands, stretch, resample, nodata, z, x, y):
    source = dataset(path, stamp)
    vrt = gdal.Translate("", source, format="VRT", bandList=list(bands))
    warped = gdal.Warp("", vrt, format="MEM", dstSRS="EPSG:3857", outputBounds=tile_bounds(z, x, y),
                       width=TILE_SIZE, height=TILE_SIZE, dstAlpha=True, outputType=gdal.GDT_Float32,
                       resampleAlg=resample, srcNodata=nodata)
    if warped is None:
        raise WorkspaceError("影像瓦片重投影失败。", 422)
    rgb = []
    valid = warped.GetRasterBand(len(bands) + 1).ReadAsArray() > 0
    for i, index in enumerate(bands):
        samples = warped.GetRasterBand(i + 1).ReadAsArray()
        valid &= np.isfinite(samples)
        lo, hi = stretch[i] if stretch else limits(path, stamp, index)
        scaled = np.nan_to_num((samples - lo) * (255 / (hi - lo)), nan=0)
        rgb.append(np.clip(scaled, 0, 255).astype(np.uint8))
    if len(rgb) == 1:
        rgb *= 3
    png = gdal.GetDriverByName("MEM").Create("", TILE_SIZE, TILE_SIZE, 4, gdal.GDT_Byte)
    for index, data in enumerate([*rgb, valid.astype(np.uint8) * 255], 1):
        png.GetRasterBand(index).WriteArray(data)
    temporary = f"/vsimem/remoteqgis-{uuid4().hex}.png"
    try:
        result = gdal.GetDriverByName("PNG").CreateCopy(temporary, png)
        if result is None:
            raise WorkspaceError("无法编码影像瓦片。", 422)
        result = None
        return bytes(gdal.VSIGetMemFileBuffer_unsafe(temporary))
    finally:
        gdal.Unlink(temporary)


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
