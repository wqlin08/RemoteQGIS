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
    diagnostics = ["-X", "faulthandler"] if os.name == "nt" else []
    return [sys.executable, *diagnostics, "-m", "host_manager", "--state-dir", str(home), "--daemon"]


def close_desktop_window(pid, log):
    if os.name != "nt":
        return
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = f"$p = Get-Process -Id {pid:d} -ErrorAction SilentlyContinue; if ($p) {{ [void]$p.CloseMainWindow() }}"
    try:
        with log.open("ab") as output:
            subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", command],
                           stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"无法向 QGIS 窗口发送关闭请求（{exc}）。", file=sys.stderr, flush=True)
