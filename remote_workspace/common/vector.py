# SPDX-License-Identifier: GPL-3.0-or-later

from base64 import b64decode, b64encode
import math

from qgis.core import QgsField, QgsFields, QgsGeometry, QgsWkbTypes
from qgis.PyQt.QtCore import QByteArray, QDate, QDateTime, QTime, Qt, QVariant


def pack(value):
    if value is None or isinstance(value, QVariant) and value.isNull():
        return None
    if isinstance(value, QDateTime):
        return {"datetime": value.toString(Qt.ISODateWithMs)}
    if isinstance(value, QDate):
        return {"date": value.toString(Qt.ISODate)}
    if isinstance(value, QTime):
        return {"time": value.toString(Qt.ISODateWithMs)}
    if isinstance(value, (QByteArray, bytes)):
        return {"bytes": b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, float) and not math.isfinite(value):
        return {"float": str(value)}
    if isinstance(value, (list, tuple)):
        return [pack(item) for item in value]
    if isinstance(value, dict):
        return {"object": {str(key): pack(item) for key, item in value.items()}}
    return value


def unpack(value):
    if isinstance(value, list):
        return [unpack(item) for item in value]
    if not isinstance(value, dict):
        return value
    key, item = next(iter(value.items()))
    if key == "datetime":
        return QDateTime.fromString(item, Qt.ISODateWithMs)
    if key == "date":
        return QDate.fromString(item, Qt.ISODate)
    if key == "time":
        return QTime.fromString(item, Qt.ISODateWithMs)
    if key == "bytes":
        return QByteArray(b64decode(item, validate=True))
    if key == "float":
        return float(item)
    if key == "object":
        return {name: unpack(val) for name, val in item.items()}
    raise ValueError("字段值格式无效。")


def field_data(field):
    return [field.name(), int(field.type()), field.length(), field.precision(), int(field.subType())]


def fields_from(data):
    fields = QgsFields()
    for name, kind, length, precision, subtype in data:
        if not fields.append(QgsField(name, QVariant.Type(kind), len=length, prec=precision,
                                     subType=QVariant.Type(subtype))):
            raise ValueError("字段名称重复。")
    return fields


def geometry_data(geometry):
    return None if geometry.isNull() else b64encode(bytes(geometry.asWkb())).decode("ascii")


def geometry_from(data):
    geometry = QgsGeometry()
    if data is not None:
        geometry.fromWkb(b64decode(data, validate=True))
        if geometry.isNull():
            raise ValueError("无效的 WKB 几何。")
    return geometry
