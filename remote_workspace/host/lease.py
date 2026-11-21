# SPDX-License-Identifier: GPL-3.0-or-later

"""Hold the write lease for a shared root while either Host mode is running."""

import os

from qgis.PyQt.QtCore import QLockFile, QTimer

from .processing import TERMINAL

FILENAME = ".remoteqgis-writer.lock"


def acquire(root, *, desktop=False):
    path = root / FILENAME
    if desktop and not path.exists() and not path.is_symlink() and not os.access(root, os.W_OK):
        return None
    if path.is_symlink():
        raise ValueError("共享目录中的 RemoteQGIS 写入锁无效。")
    lock = QLockFile(str(path))
    lock.setStaleLockTime(0)
    if not lock.tryLock():
        raise ValueError("共享目录已有 RemoteQGIS Host，或目录没有写入权限。")
    return lock


def release_when_idle(lock, service, processing):
    if (service.status.state in ("stopped", "error")
            and all(job["state"] in TERMINAL for job in processing.jobs.values())):
        lock.unlock()
    else:
        QTimer.singleShot(200, lambda: release_when_idle(lock, service, processing))
