# SPDX-License-Identifier: GPL-3.0-or-later

API = "/api/v2/workspace"
TILES = "/api/v2/tiles"
MAX_DOCUMENT = 32 * 1024 * 1024
RASTERS = {".tif", ".tiff"}
VECTORS = {".shp", ".gpkg"}
PROJECTS = {".qgz", ".qgs"}
MAX_FEATURES = 20000


class WorkspaceError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
