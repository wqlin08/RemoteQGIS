# SPDX-License-Identifier: GPL-3.0-or-later

from qgis.core import QgsFeatureRequest, QgsWkbTypes

from ..common.vector import field_data, geometry_data, pack
from ..common.workspace import MAX_FEATURES, WorkspaceError


def snapshot(layer):
    features = []
    for feature in layer.getFeatures(QgsFeatureRequest().setLimit(MAX_FEATURES + 1)):
        if len(features) == MAX_FEATURES:
            raise WorkspaceError(f"矢量代理最多加载 {MAX_FEATURES:,} 个要素。", 413)
        features.append({
            "id": str(feature.id()), "geometry": geometry_data(feature.geometry()),
            "values": [pack(value) for value in feature.attributes()],
        })
    return {
        "fields": [field_data(field) for field in layer.fields()],
        "geometry": QgsWkbTypes.displayString(layer.wkbType()),
        "crs": layer.crs().toWkt(), "features": features,
    }
