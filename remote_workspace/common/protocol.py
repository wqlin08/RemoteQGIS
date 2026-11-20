# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import asdict, dataclass
import hmac
import json
import re
import secrets
from typing import Any, Dict

VERSION = 1
INFO_PATH = "/api/v1/info"
EVENTS_PATH = "/api/v1/events"
MAX_MESSAGE_BYTES = 16 * 1024
HOST_FIELDS = ("qgis_version", "hostname", "platform", "project_name")
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class HostInfo:
    qgis_version: str
    hostname: str
    platform: str
    project_name: str

    def message(self, event: bool = False) -> Dict[str, Any]:
        payload = {"protocol": VERSION, "host": asdict(self)}
        if event:
            payload["type"] = "host.info"
        return payload


def decode_host_info(raw: bytes, *, event: bool = False) -> HostInfo:
    if len(raw) > MAX_MESSAGE_BYTES:
        raise ProtocolError("远端消息超出大小限制。")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("远端返回的 JSON 无效。") from exc

    if not isinstance(payload, dict) or type(payload.get("protocol")) is not int:
        raise ProtocolError("远端消息缺少协议版本。")
    if payload["protocol"] != VERSION:
        raise ProtocolError("远端协议版本与当前插件不兼容。")
    if event and payload.get("type") != "host.info":
        raise ProtocolError("远端事件类型无效。")
    host = payload.get("host")
    if not isinstance(host, dict) or any(
        not isinstance(host.get(field), str) or len(host[field]) > 512
        for field in HOST_FIELDS
    ):
        raise ProtocolError("远端主机信息不完整。")
    return HostInfo(**{field: host[field] for field in HOST_FIELDS})


def new_token() -> str:
    return secrets.token_urlsafe(32)


def validate_token(token: str) -> str:
    token = token.strip()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError("访问令牌须为 32–128 位英数字、下划线或连字符。")
    return token


def authorized(header: str, token: str) -> bool:
    scheme, _, value = header.partition(" ")
    return (
        scheme.lower() == "bearer"
        and TOKEN_PATTERN.fullmatch(value) is not None
        and hmac.compare_digest(value, token)
    )
