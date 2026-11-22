# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point installed in the managed QGIS profile by Host Manager."""

import json
import os
from pathlib import Path
import sys
import traceback


def classFactory(iface):
    source = os.environ.get("REMOTEQGIS_HOST_SOURCE")
    if source:
        if source not in sys.path:
            sys.path.insert(0, source)
        try:
            from host_manager.desktop import DesktopHost

            return DesktopHost(iface)
        except Exception as exc:
            traceback.print_exc()
            config = json.loads(os.environ.pop("REMOTEQGIS_HOST_CONFIG", "null"))
            os.environ.pop("REMOTEQGIS_HOST_TOKEN", None)
            os.environ.pop("REMOTEQGIS_HOST_HEALTH", None)
            if config:
                Path(config["stop"]).with_suffix(".startup-error").write_text(json.dumps({
                    "detail": f"QGIS 无法加载托管插件（{type(exc).__name__}）。详见 host.log。",
                }, ensure_ascii=False), encoding="utf-8")
    return InactiveHost()


class InactiveHost:
    def initGui(self):
        pass

    def unload(self):
        pass
