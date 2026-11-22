# SPDX-License-Identifier: GPL-3.0-or-later

import faulthandler
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile


_CRASH_LOG = None
if sys.platform == "win32":
    try:
        _CRASH_LOG = (Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) /
                      "RemoteQGIS Host" / "manager-crash.log")
        _CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        _CRASH_LOG = _CRASH_LOG.open("a", encoding="utf-8")
        faulthandler.enable(file=_CRASH_LOG, all_threads=True)
    except OSError:
        _CRASH_LOG = None


if __name__ == "__main__":
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    args = json.loads(os.environ.pop("REMOTEQGIS_WINDOWS_ARGS")) or []
    if args[:1] == ["--python"]:
        sys.argv = args[1:]
        sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
        runpy.run_path(sys.argv[0], run_name="__main__")
    else:
        from host_manager.__main__ import main

        sys.argv[1:] = args
        if "--daemon" in args and "--test-credentials-stdin" not in args:
            from host_manager.settings import directory

            home = directory()
            home.mkdir(parents=True, exist_ok=True)
            with (home / "manager.log").open("a", encoding="utf-8") as log:
                sys.stdout = sys.stderr = log
                result = main()
            sys.exit(result)
        sys.exit(main())
