# SPDX-License-Identifier: GPL-3.0-or-later

from ipaddress import ip_address
from urllib.parse import parse_qsl, urlsplit

from qgis.core import QgsProviderRegistry, QgsRasterLayer

from .workspace import WorkspaceError

ONLINE_PROPERTY = "remoteqgis/online"
WMS_KEYS = {
    "url", "type", "layers", "styles", "format", "crs", "zmin", "zmax", "tilePixelRatio",
    "dpiMode", "contextualWMSLegend", "featureCount", "tileMatrixSet", "tileDimensions",
    "IgnoreGetMapUrl", "IgnoreGetFeatureInfoUrl", "IgnoreAxisOrientation", "InvertAxisOrientation",
}
SENSITIVE = {"auth", "authcfg", "username", "password", "apikey", "key", "token", "accesstoken",
             "authorization", "secret", "signature", "sig", "xgoogsignature", "xamzsignature"}


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 8192 or any(c.isspace() or c == "\\" for c in value):
        raise WorkspaceError("在线图层需要有效的 HTTP 或 HTTPS 地址。")
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        port = url.port
        if url.scheme not in ("http", "https") or not host or url.username is not None or url.password is not None:
            raise WorkspaceError("仅支持无需登录的 HTTP 或 HTTPS 图层。")
        if url.fragment or host.endswith((".localhost", ".local", ".internal")) or host == "localhost":
            raise WorkspaceError("不能同步本机或工作站不可共享的地址。")
        try:
            address = ip_address(host)
        except ValueError:
            if "." not in host or host.isnumeric():
                raise WorkspaceError("在线图层须使用可从两端访问的域名或公网地址。")
        else:
            if not address.is_global:
                raise WorkspaceError("在线图层须使用可从两端访问的公网地址。")
        if any("".join(c for c in key.lower() if c.isalnum()) in SENSITIVE for key, _ in parse_qsl(url.query)):
            raise WorkspaceError("含有凭据的在线图层不会同步。")
    except ValueError as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError("在线图层网址无效。") from exc


def online_uri(source):
    if not isinstance(source, str) or not 1 <= len(source) <= 16384:
        raise WorkspaceError("在线图层地址无效。")
    registry = QgsProviderRegistry.instance()
    decoded = registry.decodeUri("wms", source)
    if not decoded.get("url"):
        raise WorkspaceError("仅支持 HTTP(S) 的 XYZ、WMS 和 WMTS 在线栅格。")
    if any(value and key not in WMS_KEYS for key, value in decoded.items()):
        raise WorkspaceError("此在线图层含有认证信息或尚未支持的连接参数。")
    public_url(decoded["url"])
    kind = decoded.get("type") or "wms"
    if kind not in ("xyz", "wms", "wmts") or kind != "xyz" and not decoded.get("layers"):
        raise WorkspaceError("仅支持 HTTP(S) 的 XYZ、WMS 和 WMTS 在线栅格。")
    if kind == "xyz" and not all(marker in decoded["url"] for marker in ("{z}", "{x}", "{y}")):
        raise WorkspaceError("XYZ 图层地址须含 {z}、{x}、{y} 瓦片坐标。")
    # QGIS must use the explicitly validated service URL for WMS requests.
    if kind == "wms" and not decoded.get("tileMatrixSet"):
        decoded.update(IgnoreGetMapUrl="1", IgnoreGetFeatureInfoUrl="1")
    return registry.encodeUri("wms", {key: value for key, value in decoded.items() if key in WMS_KEYS})


def source_from_layer(layer):
    if not isinstance(layer, QgsRasterLayer) or layer.providerType() != "wms":
        raise WorkspaceError("远程分组只接收在线 XYZ、WMS 或 WMTS 图层。本地数据请从工作站共享目录导入。")
    return online_uri(layer.source())
