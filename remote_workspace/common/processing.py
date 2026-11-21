# SPDX-License-Identifier: GPL-3.0-or-later

# Only these parameters are accepted. Algorithm options which forward raw shell
# switches, creation options or arbitrary input/output paths are excluded.
TOOLS = {
    "native:buffer": "INPUT DISTANCE SEGMENTS END_CAP_STYLE JOIN_STYLE MITER_LIMIT DISSOLVE SEPARATE_DISJOINT",
    "native:centroids": "INPUT ALL_PARTS",
    "native:dissolve": "INPUT FIELD SEPARATE_DISJOINT",
    "native:fixgeometries": "INPUT METHOD",
    "native:clip": "INPUT OVERLAY",
    "native:intersection": "INPUT OVERLAY INPUT_FIELDS OVERLAY_FIELDS OVERLAY_FIELDS_PREFIX GRID_SIZE",
    "native:difference": "INPUT OVERLAY GRID_SIZE",
    "native:union": "INPUT OVERLAY OVERLAY_FIELDS_PREFIX GRID_SIZE",
    "native:symmetricaldifference": "INPUT OVERLAY OVERLAY_FIELDS_PREFIX GRID_SIZE",
    "native:multiparttosingleparts": "INPUT",
    "native:reprojectlayer": "INPUT TARGET_CRS CONVERT_CURVED_GEOMETRIES",
    "native:extractbyextent": "INPUT EXTENT CLIP",
    "native:mergevectorlayers": "LAYERS CRS",
    "native:deletecolumn": "INPUT COLUMN",
    "native:fieldcalculator": "INPUT FIELD_NAME FIELD_TYPE FIELD_LENGTH FIELD_PRECISION FORMULA",
    "native:joinattributestable": "INPUT FIELD INPUT_2 FIELD_2 FIELDS_TO_COPY METHOD DISCARD_NONMATCHING PREFIX",
    "native:joinattributesbylocation": "INPUT PREDICATE JOIN JOIN_FIELDS METHOD DISCARD_NONMATCHING PREFIX",
    "native:countpointsinpolygon": "POLYGONS POINTS WEIGHT CLASSFIELD FIELD",
    "native:simplifygeometries": "INPUT METHOD TOLERANCE",
    "native:convexhull": "INPUT",
    "native:polygonstolines": "INPUT",
    "native:densifygeometriesgivenaninterval": "INPUT INTERVAL",
    "native:pointonsurface": "INPUT ALL_PARTS",
    "native:boundary": "INPUT",
    "native:zonalstatisticsfb": "INPUT INPUT_RASTER RASTER_BAND COLUMN_PREFIX STATISTICS",
    "native:rastersampling": "INPUT RASTERCOPY COLUMN_PREFIX",
    "native:rastercalc": "LAYERS EXPRESSION EXTENT CELL_SIZE CRS",
    "native:rescaleraster": "INPUT BAND MINIMUM MAXIMUM NODATA",
    "native:reclassifybytable": "INPUT_RASTER RASTER_BAND TABLE NO_DATA RANGE_BOUNDARIES NODATA_FOR_MISSING DATA_TYPE",
    "native:cellstatistics": "INPUT STATISTIC IGNORE_NODATA REFERENCE_LAYER OUTPUT_NODATA_VALUE",
    "native:fillnodata": "INPUT BAND FILL_VALUE",
    "native:rasterlayerstatistics": "INPUT BAND",
    "gdal:cliprasterbyextent": "INPUT PROJWIN OVERCRS NODATA DATA_TYPE",
    "gdal:cliprasterbymasklayer": (
        "INPUT MASK SOURCE_CRS TARGET_CRS TARGET_EXTENT NODATA ALPHA_BAND CROP_TO_CUTLINE "
        "KEEP_RESOLUTION SET_RESOLUTION X_RESOLUTION Y_RESOLUTION MULTITHREADING DATA_TYPE"
    ),
    "gdal:warpreproject": (
        "INPUT SOURCE_CRS TARGET_CRS RESAMPLING NODATA TARGET_RESOLUTION DATA_TYPE TARGET_EXTENT "
        "TARGET_EXTENT_CRS MULTITHREADING"
    ),
    "gdal:translate": "INPUT TARGET_CRS NODATA DATA_TYPE",
    "gdal:slope": "INPUT BAND SCALE AS_PERCENT COMPUTE_EDGES ZEVENBERGEN",
    "gdal:aspect": "INPUT BAND TRIG_ANGLE ZERO_FLAT COMPUTE_EDGES ZEVENBERGEN",
    "gdal:hillshade": "INPUT BAND Z_FACTOR SCALE AZIMUTH ALTITUDE COMPUTE_EDGES ZEVENBERGEN COMBINED MULTIDIRECTIONAL",
    "gdal:roughness": "INPUT BAND COMPUTE_EDGES",
    "gdal:polygonize": "INPUT BAND FIELD EIGHT_CONNECTEDNESS",
    "gdal:rasterize": "INPUT FIELD BURN USE_Z UNITS WIDTH HEIGHT EXTENT NODATA DATA_TYPE INIT INVERT",
}
RASTER = {
    "native:rastercalc", "native:rescaleraster", "native:reclassifybytable", "native:cellstatistics",
    "native:fillnodata", "gdal:cliprasterbyextent", "gdal:cliprasterbymasklayer", "gdal:warpreproject",
    "gdal:translate", "gdal:slope", "gdal:aspect", "gdal:hillshade", "gdal:roughness", "gdal:rasterize",
}
REPORT = {"native:rasterlayerstatistics"}
LAYER_TYPES = {"source", "vector", "raster", "multilayer"}
