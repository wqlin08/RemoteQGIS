# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import sys


def application(profile, *, processing=True):
    """Initialize only QGIS Core, in a profile separate from Desktop."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from qgis.core import QgsApplication
    from remote_workspace.host.qgis_info import validate_environment

    validate_environment()
    prefix = Path(os.environ["QGIS_PREFIX_PATH"])
    QgsApplication.setPrefixPath(str(prefix), True)
    profile.mkdir(parents=True, exist_ok=True)
    app = QgsApplication([], False, str(profile))
    app.initQgis()
    if processing:
        for directory in (prefix / "python/plugins", prefix.parent / "Resources/python/plugins"):
            if directory.is_dir():
                sys.path.insert(0, str(directory))
        from processing.core.Processing import Processing

        Processing.initialize()
    return app
