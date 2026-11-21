# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from tempfile import TemporaryDirectory

from headless_host.bootstrap import application


def run(root, app):
    from osgeo import gdal, ogr
    from qgis.PyQt.QtCore import QTimer
    from qgis.core import (
        QgsApplication, QgsProcessingAlgRunnerTask, QgsProcessingContext,
        QgsProcessingFeedback, QgsProject, QgsRasterLayer, QgsVectorLayer,
    )

    raster_path = root / "pixels.tif"
    raster = gdal.GetDriverByName("GTiff").Create(str(raster_path), 4, 4, 1, gdal.GDT_UInt16)
    raster.SetGeoTransform((0, 1, 0, 4, 0, -1))
    raster.GetRasterBand(1).Fill(1234)
    raster = None
    gpkg = root / "points.gpkg"
    dataset = ogr.GetDriverByName("GPKG").CreateDataSource(str(gpkg))
    table = dataset.CreateLayer("points", geom_type=ogr.wkbPoint)
    feature = ogr.Feature(table.GetLayerDefn())
    feature.SetGeometry(ogr.CreateGeometryFromWkt("POINT (1 1)"))
    assert table.CreateFeature(feature) == 0
    table = feature = dataset = None

    project = QgsProject.instance()
    vector = QgsVectorLayer(str(gpkg), "points", "ogr")
    image = QgsRasterLayer(str(raster_path), "pixels", "gdal")
    assert vector.isValid() and image.isValid()
    project.addMapLayers([vector, image])
    assert vector.startEditing()
    from qgis.core import QgsGeometry

    assert vector.changeGeometry(next(vector.getFeatures()).id(), QgsGeometry.fromWkt("POINT (2 2)"))
    assert vector.commitChanges()
    path = root / "workspace.qgz"
    assert project.write(str(path))
    project.clear()
    assert project.read(str(path)) and len(project.mapLayers()) == 2

    registry = QgsApplication.processingRegistry()
    from remote_workspace.common.processing import TOOLS

    assert all(registry.algorithmById(key) for key in TOOLS)
    algorithm = registry.algorithmById("native:buffer")
    context, feedback = QgsProcessingContext(), QgsProcessingFeedback()
    output = root / "buffer.gpkg"
    task = QgsProcessingAlgRunnerTask(algorithm, {"INPUT": str(gpkg), "DISTANCE": 1,
                                                    "OUTPUT": str(output)}, context, feedback)
    results = []

    def finished(success, values):
        results.append((success, values))
        app.quit()

    task.executed.connect(finished)
    QgsApplication.taskManager().addTask(task)
    QTimer.singleShot(20000, app.quit)
    app.exec()
    assert results and results[0][0], feedback.textLog()
    assert output.is_file()
    project.clear()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="remoteqgis-core-") as directory:
        root = Path(directory)
        app = application(root / "profile")
        run(root, app)
        app.exitQgis()
        print("PASS: standalone QGIS Core, TIFF, GeoPackage, QGZ and asynchronous Processing")
