# SPDX-License-Identifier: GPL-3.0-or-later

"""Report that the managed QGIS process entered its embedded Python runtime."""


def remoteqgis_record_python_startup():
    from datetime import datetime
    import json
    import os
    from pathlib import Path

    try:
        config = json.loads(os.environ.get("REMOTEQGIS_HOST_CONFIG", "null"))
        if config:
            launch = Path(config["stop"]).with_suffix(".launch")
            if launch.is_file():
                launch.with_suffix(".python").touch()
                stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                print(f"{stamp} pid={os.getpid()} RemoteQGIS embedded Python initialized", flush=True)
    except (OSError, ValueError, KeyError):
        pass


remoteqgis_record_python_startup()
