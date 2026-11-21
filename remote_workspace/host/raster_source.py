# SPDX-License-Identifier: GPL-3.0-or-later

from functools import lru_cache
import re
from pathlib import Path
from xml.etree import ElementTree

from osgeo import gdal

from ..common.workspace import WorkspaceError
from .rasters import dataset, file_stamp

SOURCE = "/api/v3/raster"
MEMBER = r"source\.tif(?:\.ovr|\.msk)?"
RANGE_LIMIT = 16 * 1024 * 1024


@lru_cache(maxsize=64)
def raster_manifest(path, stamp, assigned_crs):
    source = dataset(path, stamp)
    vrt = gdal.Translate("", source, format="VRT")
    if vrt is None:
        raise WorkspaceError("无法读取原始栅格的波段信息。", 422)
    root = ElementTree.fromstring(vrt.GetMetadata("xml:VRT")[0])
    srs = root.find("SRS")
    if srs is None and assigned_crs:
        ElementTree.SubElement(root, "SRS").text = assigned_crs
    for filename in root.iter("SourceFilename"):
        filename.attrib["relativeToVRT"] = "0"
        filename.text = "__REMOTEQGIS_SOURCE__"
    # GDAL writes source filenames, but other free-form metadata may name local files.
    for parent in root.iter():
        for child in list(parent):
            if child.tag in ("Metadata", "Description"):
                parent.remove(child)
    return {"vrt": ElementTree.tostring(root, encoding="unicode")}


def read_range(spec, member, requested, head):
    path = Path(spec["path"] + member[len("source.tif"):])
    try:
        if path.is_symlink() or path.resolve() != path or file_stamp(spec["path"]) != spec["stamp"]:
            raise WorkspaceError("栅格文件已经修改，请刷新工作区。", 409)
        with path.open("rb") as file:
            size = file.seek(0, 2)
            headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes",
                       "Cache-Control": "private, max-age=31536000"}
            if head:
                return 200, dict(headers, **{"Content-Length": str(size)}), b""
            match = re.fullmatch(r"bytes=(\d{1,20})-(\d{1,20})", requested)
            if match:
                start, end = map(int, match.groups())
                end = min(end, size - 1)
            else:
                start, end = 0, size - 1 if not requested and size <= 65536 else -1
            if start > end or end - start + 1 > RANGE_LIMIT:
                return 416, dict(headers, **{"Content-Range": f"bytes */{size}"}), b""
            file.seek(start)
            data = file.read(end - start + 1)
            if len(data) != end - start + 1:
                raise WorkspaceError("栅格文件已经修改，请刷新工作区。", 409)
    except (OSError, RuntimeError) as exc:
        raise WorkspaceError("栅格或其概览不存在。", 404) from exc
    headers["Content-Length"] = str(len(data))
    if match:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    return (206 if match else 200), headers, data
