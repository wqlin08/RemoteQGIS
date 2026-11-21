# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]


def spawn(command, log, *, stdin=None, env=None, detached=False):
    flags = {}
    if os.name == "nt":
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW
        if detached:
            flags["creationflags"] |= subprocess.CREATE_NEW_PROCESS_GROUP
    elif detached:
        flags["start_new_session"] = True
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log.open("ab") as output:
        return subprocess.Popen(command, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                stdout=output, stderr=output, env=env, cwd=SOURCE, **flags)


def python_env():
    paths = [str(SOURCE)]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    return dict(os.environ, PYTHONPATH=os.pathsep.join(paths), PYTHONUNBUFFERED="1",
                PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")


def manager_command(home):
    return [sys.executable, "-m", "host_manager", "--state-dir", str(home), "--daemon"]
