# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
import sys


def load_tornado() -> str:
    bundled = str(Path(__file__).resolve().parents[1] / "_vendor")
    sys.path.insert(0, bundled)
    try:
        import tornado

        if tornado.version_info[:3] < (6, 5, 10):
            raise RuntimeError("当前 QGIS 已加载早于 6.5.10 的 Tornado，请在独立的 QGIS 用户配置中启用 RemoteQGIS。")
        from tornado import httpserver, netutil, web, websocket
    except (ImportError, OSError) as exc:
        raise RuntimeError("无法加载 Host 服务依赖，请通过完整的 RemoteQGIS ZIP 安装插件。") from exc
    finally:
        sys.path.remove(bundled)
    return tornado.version
