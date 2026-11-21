# SPDX-License-Identifier: GPL-3.0-or-later

import os
from pathlib import Path
import plistlib
import subprocess
import sys

LABEL = "org.remoteqgis.host-manager"


def launch_agent(launcher, home):
    return {"Label": LABEL, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "LimitLoadToSessionType": "Aqua", "ThrottleInterval": 15,
            "ProgramArguments": [str(launcher), "--daemon", "--start-worker"],
            "StandardOutPath": str(home / "manager.log"), "StandardErrorPath": str(home / "manager.log")}


def configure(enabled, home):
    launcher = Path(os.environ["REMOTEQGIS_MANAGER_LAUNCHER"])
    if not launcher.is_file():
        raise ValueError("请从安装目录运行 Host Manager，再配置自动启动。")
    if sys.platform == "darwin":
        path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
        if enabled:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps(launch_agent(launcher, home)))
        else:
            path.unlink(missing_ok=True)
        return
    if sys.platform != "win32":
        raise ValueError("当前系统不支持 Host Manager 自动启动。")
    script = '''
$ErrorActionPreference = 'Stop'
$name = 'RemoteQGIS Host Manager - ' + [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
if ($env:REMOTEQGIS_AUTOSTART -eq '0') {
    if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false
    }
} else {
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $action = New-ScheduledTaskAction -Execute $env:REMOTEQGIS_MANAGER_LAUNCHER -Argument '--daemon --start-worker'
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 `
        -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}
'''
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        env=dict(os.environ, REMOTEQGIS_AUTOSTART="1" if enabled else "0"),
        creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True, timeout=20,
    )
    if result.returncode:
        raise OSError("Windows 任务计划程序无法更新登录自启设置。")


def remove_macos_loaded():
    if sys.platform == "darwin":
        subprocess.run(["/bin/launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True, timeout=10)
