# SPDX-License-Identifier: GPL-3.0-or-later

import platform
import socket
import sys

from qgis.core import Qgis, QgsProject
from qgis.PyQt.QtCore import QFileInfo, PYQT_VERSION_STR, qVersion

from ..common.protocol import HostInfo


def host_info() -> HostInfo:
    project = QgsProject.instance()
    name = project.title() or QFileInfo(project.fileName()).completeBaseName() or "未命名项目"
    return HostInfo(
        Qgis.QGIS_VERSION[:512],
        socket.gethostname()[:512],
        f"{platform.system()} {platform.release()} ({platform.machine()})"[:512],
        name[:512],
    )


def environment() -> str:
    try:
        from osgeo import gdal

        gdal_version = gdal.VersionInfo("RELEASE_NAME")
    except (ImportError, OSError, RuntimeError):
        gdal_version = "不可用"
    return (
        f"QGIS {Qgis.QGIS_VERSION} · Python {platform.python_version()}\n"
        f"Qt {qVersion()} · PyQt {PYQT_VERSION_STR} · GDAL {gdal_version}"
    )


def validate_environment():
    if not 34000 <= Qgis.QGIS_VERSION_INT < 40000 or sys.version_info < (3, 9):
        raise RuntimeError("插件需要 QGIS 3.40–3.x 和 Python 3.9 或更高版本。")
