# SPDX-License-Identifier: GPL-3.0-or-later

from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path

from ..common.protocol import validate_token


@dataclass(frozen=True)
class HostConfig:
    address: str
    port: int
    shared_directory: Path
    token: str = field(repr=False)

    @classmethod
    def validate(cls, address: str, port: int, directory: str, token: str):
        try:
            ip = ip_address(address.strip())
        except ValueError as exc:
            raise ValueError("监听地址须为本机网卡的 IPv4 或 IPv6 地址。") from exc
        if ip.is_unspecified or ip.is_multicast:
            raise ValueError("请选择具体的本机监听地址。")
        if not 1 <= port <= 65535:
            raise ValueError("端口须在 1–65535 之间。")
        try:
            root = Path(directory).expanduser().resolve(strict=True) if directory.strip() else None
            if root is None or not root.is_dir():
                raise ValueError("请选择已存在的共享目录。")
        except (OSError, RuntimeError) as exc:
            raise ValueError("无法访问指定的共享目录。") from exc
        return cls(str(ip), port, root, validate_token(token))
