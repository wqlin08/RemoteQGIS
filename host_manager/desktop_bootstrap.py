# SPDX-License-Identifier: GPL-3.0-or-later

"""Entry point installed in the managed QGIS profile by Host Manager."""

import os
import sys


def classFactory(iface):
    source = os.environ.get("REMOTEQGIS_HOST_SOURCE")
    if source:
        if source not in sys.path:
            sys.path.insert(0, source)
        from host_manager.desktop import DesktopHost

        return DesktopHost(iface)
    return InactiveHost()


class InactiveHost:
    def initGui(self):
        pass

    def unload(self):
        pass
