# SPDX-License-Identifier: GPL-3.0-or-later

from hashlib import sha256
import json
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from osgeo import gdal, ogr, osr
from qgis.core import QgsFeatureRequest, QgsVectorLayer, QgsWkbTypes
from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, QTime, Qt, QVariant

from ..common.edits import IDENTITY
from ..common.vector import field_data, geometry_data, pack, unpack
from ..common.workspace import MAX_FEATURES, VECTORS, WorkspaceError

OGR_TYPES = {
    int(QVariant.Int): ogr.OFTInteger, int(QVariant.LongLong): ogr.OFTInteger64,
    int(QVariant.Double): ogr.OFTReal, int(QVariant.String): ogr.OFTString,
    int(QVariant.Date): ogr.OFTDate, int(QVariant.DateTime): ogr.OFTDateTime,
    int(QVariant.Time): ogr.OFTTime, int(QVariant.Bool): ogr.OFTInteger,
    int(QVariant.ByteArray): ogr.OFTBinary,
}
WKB = {
    "Point": ogr.wkbPoint, "LineString": ogr.wkbLineString, "Polygon": ogr.wkbPolygon,
    "MultiPoint": ogr.wkbMultiPoint, "MultiLineString": ogr.wkbMultiLineString,
    "MultiPolygon": ogr.wkbMultiPolygon,
}
WORK = ".remoteqgis-edits"


def digest(data):
    return sha256(json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def identifier(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def snapshot(layer):
    fields = layer.fields()
    uuid_index = fields.indexFromName(IDENTITY)
    editable = uuid_index != -1
    if editable and fields[uuid_index].type() != QVariant.String:
        raise WorkspaceError("已存在非文本类型的 rq_uuid 字段。", 409)
    primary = set(layer.dataProvider().pkAttributeIndexes()) if editable else set()
    columns = [i for i in range(len(fields)) if i != uuid_index and i not in primary]
    features, identifiers = [], set()
    for feature in layer.getFeatures(QgsFeatureRequest().setLimit(MAX_FEATURES + 1)):
        if len(features) == MAX_FEATURES:
            raise WorkspaceError(f"矢量代理最多加载 {MAX_FEATURES:,} 个要素。", 413)
        key = feature[uuid_index] if editable else str(feature.id())
        if editable and (not identifier(key) or key in identifiers):
            raise WorkspaceError("图层的 rq_uuid 字段包含空值、重复值或无效值。", 409)
        identifiers.add(key)
        features.append({"id": key, "geometry": geometry_data(feature.geometry()),
                         "values": [pack(feature[i]) for i in columns]})
    doc = {
        "fields": [field_data(fields[i]) for i in columns],
        "geometry": QgsWkbTypes.displayString(layer.wkbType()),
        "crs": layer.crs().toWkt(), "features": sorted(features, key=lambda row: row["id"]),
        "editable": editable,
    }
    doc["revision"] = digest(doc)
    return doc


def ogr_result(code):
    if code != ogr.OGRERR_NONE:
        raise WorkspaceError("数据驱动拒绝写入，事务未完成。", 409)


def checked_fields(fields, shapefile, original=None, protected=()):
    if not isinstance(fields, list) or len(fields) > 128:
        raise WorkspaceError("字段定义无效或超过 128 个字段。")
    names = set()
    for field in fields:
        if not isinstance(field, list) or len(field) != 5:
            raise WorkspaceError("字段定义无效。")
        name, kind, length, precision, subtype = field
        if (not isinstance(name, str) or not name or len(name) > 64 or name != name.strip()
                or any(ord(char) < 32 or char in '"\0' for char in name)
                or name.casefold() in names | {IDENTITY} | set(protected)
                or any(type(n) is not int or not 0 <= n <= 4096 for n in (kind, length, precision, subtype))):
            raise WorkspaceError("字段名称或类型无效。")
        names.add(name.casefold())
        if original and original.get(name) == field:
            continue
        if kind not in OGR_TYPES or shapefile and (len(name.encode("utf-8")) > 10 or kind not in {
            int(QVariant.Int), int(QVariant.LongLong), int(QVariant.Double), int(QVariant.String), int(QVariant.Date)
        } or kind == int(QVariant.String) and length > 254):
            raise WorkspaceError("字段类型或长度不受目标格式支持。")


def field_definition(definition):
    name, kind, length, precision, _ = definition
    field = ogr.FieldDefn(name, OGR_TYPES[kind])
    if kind == QVariant.Bool:
        field.SetSubType(ogr.OFSTBoolean)
    if length:
        field.SetWidth(length)
    if precision:
        field.SetPrecision(precision)
    return field


def set_field(feature, index, value):
    if value is None:
        feature.SetFieldNull(index)
    elif isinstance(value, QDateTime):
        feature.SetField(index, value.toString(Qt.ISODateWithMs))
    elif isinstance(value, (QDate, QTime)):
        feature.SetField(index, value.toString(Qt.ISODate))
    elif isinstance(value, QByteArray):
        feature.SetFieldBinaryFromHexString(index, bytes(value).hex())
    elif isinstance(value, (str, int, float)):
        feature.SetField(index, value)
    elif isinstance(value, (tuple, list)):
        feature.SetField(index, list(value))
    else:
        raise WorkspaceError("存在数据驱动不支持的字段值。")


class VectorEdits:
    def __init__(self, workspace):
        self.workspace = workspace
        self.files = workspace.files

    def source(self, data):
        layer = self.workspace.layer(data, QgsVectorLayer)
        if layer.isEditable() or layer.subsetString():
            raise WorkspaceError("请先停止工作站编辑并移除图层筛选条件。", 409)
        path, name = self.workspace.source(layer)
        list(self.files.parts(path))
        return layer, path, name

    def directory(self):
        path = self.files.root / WORK
        if path.is_symlink() or path.resolve() != path:
            raise WorkspaceError("工作站事务目录无效。")
        path.mkdir(exist_ok=True)
        return path

    def create(self, data):
        path = self.files.path(data.get("path"), exists=False)
        if path.suffix.lower() not in VECTORS or not path.parent.is_dir():
            raise WorkspaceError("请选择目录下的 .gpkg 或 .shp 文件。")
        self.files.ensure_free(path)
        shapefile = path.suffix.lower() == ".shp"
        fields, geometry, crs = data.get("fields"), data.get("geometry"), data.get("crs")
        checked_fields(fields, shapefile, protected=("fid", "geom") if not shapefile else ())
        if not isinstance(geometry, str) or geometry not in WKB or not isinstance(crs, str) or len(crs) > 100000:
            raise WorkspaceError("矢量几何类型或坐标系无效。")
        srs = osr.SpatialReference()
        if srs.SetFromUserInput(crs) != 0:
            raise WorkspaceError("无法识别指定的坐标系。")
        with TemporaryDirectory(dir=self.directory(), prefix="create-") as folder:
            target = Path(folder) / path.name
            driver = ogr.GetDriverByName("ESRI Shapefile" if shapefile else "GPKG")
            dataset = driver.CreateDataSource(str(target))
            if dataset is None:
                raise WorkspaceError("无法创建远端矢量数据。")
            layer = dataset.CreateLayer(path.stem, srs, WKB[geometry],
                                        options=["ENCODING=UTF-8"] if shapefile else [])
            if layer is None:
                raise WorkspaceError("无法创建远端矢量图层。")
            ogr_result(layer.CreateField(field_definition([IDENTITY, int(QVariant.String), 36, 0, 0])))
            for definition in fields:
                ogr_result(layer.CreateField(field_definition(definition)))
            layer = dataset = None
            written = []
            try:
                for member in Path(folder).iterdir():
                    dest = path.parent / member.name
                    if dest.exists():
                        raise WorkspaceError("目标路径已存在。", 409)
                    member.rename(dest)
                    written.append(dest)
            except (OSError, WorkspaceError):
                for member in written:
                    member.rename(Path(folder) / member.name)
                raise
        return self.workspace.import_files({"paths": [self.files.relative(path)]})

    def enable(self, data):
        layer, path, table = self.source(data)
        doc = snapshot(layer)
        if not doc["editable"]:
            def enable(ogr_layer):
                ogr_result(ogr_layer.CreateField(field_definition([IDENTITY, int(QVariant.String), 36, 0, 0])))
                features = [feature.GetFID() for feature in ogr_layer]
                for fid in features:
                    feature = ogr_layer.GetFeature(fid)
                    feature.SetField(IDENTITY, str(uuid4()))
                    ogr_result(ogr_layer.SetFeature(feature))
            self.write(path, table, enable)
            self.reload(path)
        return snapshot(layer)

    def reload(self, path):
        for layer in self.workspace.project.mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                source = self.workspace.source(layer)
                if source is not None and source[0] == path:
                    layer.reload()
                    layer.updateFields()
                    layer.updateExtents()
                    layer.triggerRepaint()

    def commit(self, data):
        layer, path, table = self.source(data)
        token = data.get("transaction")
        if not identifier(token):
            raise WorkspaceError("事务标识无效。")
        dataset_key = [self.files.relative(path), table or ""]
        if dataset_key != data.get("dataset"):
            raise WorkspaceError("事务记录与工作站数据源不一致。", 409)
        record = self.directory() / f"{token}.json"
        if record.is_symlink():
            raise WorkspaceError("工作站事务记录无效。", 409)
        signature = digest({key: value for key, value in data.items() if key != "id"})
        if record.exists():
            if record.read_text(encoding="ascii") != signature:
                raise WorkspaceError("事务标识重复。", 409)
            return snapshot(layer)
        before = snapshot(layer)
        if not before["editable"] or before["revision"] != data.get("base"):
            raise WorkspaceError("远端数据版本已变化。修改已保存在客户端，请核对后处理。", 409)
        current = {feature["id"]: feature for feature in before["features"]}
        old_fields = {field[0]: field for field in before["fields"]}
        fields = data.get("fields")
        primary = [layer.fields()[i].name().casefold() for i in layer.dataProvider().pkAttributeIndexes()]
        shapefile = path.suffix.lower() == ".shp"
        checked_fields(fields, shapefile, old_fields, primary)
        deletes, updates, additions = (data.get(key) for key in ("delete", "update", "add"))
        if any(not isinstance(rows, list) for rows in (deletes, updates, additions)):
            raise WorkspaceError("事务记录格式无效。")
        if any(not identifier(key) or key not in current for key in deletes) or len(set(deletes)) != len(deletes):
            raise WorkspaceError("删除的要素不存在或重复。", 409)
        keys = set(deletes)
        for records, existing in ((updates, True), (additions, False)):
            for row in records:
                if not isinstance(row, dict):
                    raise WorkspaceError("要素记录无效。")
                key = row.get("id")
                if not identifier(key) or key in keys or (key in current) != existing:
                    raise WorkspaceError("事务包含无效或重复的要素标识。", 409)
                keys.add(key)
                values = row.get("values")
                if not isinstance(values, list) or len(values) != len(fields):
                    raise WorkspaceError("要素属性与字段定义不一致。")
                if shapefile and any(isinstance(value, str) and len(value.encode("utf-8")) > 254 for value in values):
                    raise WorkspaceError("Shapefile 的文本字段不得超过 254 字节。")
                if "geometry" not in row or row["geometry"] is not None and not isinstance(row["geometry"], str):
                    raise WorkspaceError("要素几何格式无效。")
        if len(current) - len(deletes) + len(additions) > MAX_FEATURES:
            raise WorkspaceError("结果超出当前矢量代理要素上限。", 413)

        def apply(ogr_layer):
            self.apply(ogr_layer, old_fields, fields, deletes, updates, additions)

        temporary = record.with_suffix(".tmp")
        if temporary.is_symlink():
            raise WorkspaceError("工作站事务记录无效。", 409)
        temporary.write_text(signature, encoding="ascii")
        try:
            self.write(path, table, apply)
        except WorkspaceError:
            temporary.unlink(missing_ok=True)
            raise
        except (RuntimeError, ValueError, TypeError, OverflowError, KeyError, StopIteration, OSError) as exc:
            temporary.unlink(missing_ok=True)
            raise WorkspaceError("事务中的几何、属性或字段无法写入目标数据。", 409) from exc
        temporary.replace(record)
        self.reload(path)
        return snapshot(layer)

    @staticmethod
    def apply(layer, old_fields, fields, deletes, updates, additions):
        from base64 import b64decode

        fids = {feature.GetField(IDENTITY): feature.GetFID() for feature in layer}
        desired = {field[0]: field for field in fields}
        for name, info in old_fields.items():
            if desired.get(name) != info:
                index = layer.GetLayerDefn().GetFieldIndex(name)
                ogr_result(layer.DeleteField(index))
        for field in fields:
            if old_fields.get(field[0]) != field:
                ogr_result(layer.CreateField(field_definition(field)))
        definition = layer.GetLayerDefn()
        columns = [definition.GetFieldIndex(field[0]) for field in fields]
        if -1 in columns:
            raise WorkspaceError("工作站驱动未能保留指定字段名称。", 409)
        for key in deletes:
            ogr_result(layer.DeleteFeature(fids[key]))
        for rows, new in ((updates, False), (additions, True)):
            for row in rows:
                key = row["id"]
                feature = ogr.Feature(definition) if new else layer.GetFeature(fids[key])
                if new:
                    feature.SetField(IDENTITY, key)
                if row["geometry"] is None:
                    ogr_result(feature.SetGeometry(None))
                else:
                    geom = ogr.CreateGeometryFromWkb(b64decode(row["geometry"], validate=True))
                    if geom is None:
                        raise WorkspaceError("几何格式无效。")
                    ogr_result(feature.SetGeometry(geom))
                for index, value in zip(columns, row["values"]):
                    set_field(feature, index, unpack(value))
                ogr_result(layer.CreateFeature(feature) if new else layer.SetFeature(feature))

    def write(self, path, table, function):
        shapefile = path.suffix.lower() == ".shp"
        if not shapefile:
            self.run(path, table, function, transaction=True)
            return
        backup = self.directory() / uuid4().hex
        backup.mkdir()
        with TemporaryDirectory(dir=self.directory(), prefix="shp-") as folder:
            staging = Path(folder)
            originals = list(self.files.parts(path))
            for member in originals:
                copy2(member, staging / member.name)
                copy2(member, backup / member.name)
            self.run(staging / path.name, table, function, transaction=False)
            if set(member.name for member in self.files.parts(path)) != {member.name for member in originals}:
                raise WorkspaceError("写入期间 Shapefile 文件组发生变化。", 409)
            replaced, created = [], []
            try:
                outputs = {item.name: item for item in staging.iterdir()}
                for member in originals:
                    output = outputs.pop(member.name, None)
                    if output:
                        output.replace(member)
                    else:
                        member.unlink()
                    replaced.append(member)
                for name, output in outputs.items():
                    target = path.parent / name
                    if target.exists():
                        raise WorkspaceError("Shapefile 副文件冲突。", 409)
                    output.rename(target)
                    created.append(target)
            except (OSError, WorkspaceError):
                for target in created:
                    target.unlink()
                for member in replaced:
                    copy2(backup / member.name, member)
                raise

    @staticmethod
    def run(path, table, function, transaction):
        driver = "GPKG" if transaction else "ESRI Shapefile"
        dataset = gdal.OpenEx(str(path), gdal.OF_VECTOR | gdal.OF_UPDATE, allowed_drivers=[driver])
        if dataset is None:
            raise WorkspaceError("工作站无法以写入模式打开矢量数据。", 409)
        layer = dataset.GetLayerByName(table) if table else dataset.GetLayer(0)
        if layer is None:
            raise WorkspaceError("指定矢量图层不存在。", 404)
        if transaction:
            ogr_result(dataset.StartTransaction())
        try:
            function(layer)
            ogr_result(layer.SyncToDisk())
            if transaction:
                ogr_result(dataset.CommitTransaction())
        except Exception:
            if transaction:
                dataset.RollbackTransaction()
            raise
        finally:
            layer = dataset = None
