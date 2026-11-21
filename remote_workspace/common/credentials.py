# SPDX-License-Identifier: GPL-3.0-or-later

"""Store access tokens with the operating system, under the current user."""

import ctypes as c
import os
import re
import subprocess
import sys

from .protocol import validate_token

SERVICE = "RemoteQGIS"


def load_token(account):
    credential = _keychain(account) if sys.platform == "darwin" else _windows(account)
    if credential is None:
        raise ValueError("系统凭据库中找不到此访问令牌。")
    return validate_token(credential)


def save_token(account, token):
    token = validate_token(token)
    if sys.platform == "darwin":
        _keychain(account, token)
    else:
        _windows(account, token)


def delete_token(account):
    if sys.platform == "darwin":
        _keychain(account, delete=True)
    else:
        _windows(account, delete=True)


def _keychain(account, token=None, *, delete=False):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", account):
        raise ValueError("凭据名称无效。")
    flags = ["-s", SERVICE, "-a", account]

    def security(args, *, document=None):
        try:
            return subprocess.run(
                ["/usr/bin/security", *args], input=document, text=True, capture_output=True,
                env=dict(os.environ, LC_ALL="C"), timeout=20,
            )
        except subprocess.TimeoutExpired:
            raise OSError("访问 macOS 钥匙串超时。") from None

    if delete:
        result = security(["delete-generic-password", *flags])
        if result.returncode not in (0, 44):
            raise OSError(f"无法删除 macOS 钥匙串凭据（{result.returncode}）。")
        return None
    if token is not None:
        # Interactive input keeps the secret out of the process command line.
        command = " ".join(["add-generic-password", "-U", *flags, "-w", token, "-T", "/usr/bin/security"])
        result = security(["-i"], document=command + "\n")
        if result.returncode or result.stderr.strip():
            raise OSError("无法保存访问令牌到 macOS 钥匙串。")
    result = security(["find-generic-password", *flags, "-w"])
    if result.returncode == 44 and token is None:
        return None
    if result.returncode or (token is not None and result.stdout.strip() != token):
        raise OSError(f"无法读取 macOS 钥匙串凭据（{result.returncode}）。")
    return result.stdout.strip()


def _windows(account, token=None, *, delete=False):
    if sys.platform != "win32":
        raise OSError("当前系统无 RemoteQGIS 支持的凭据库。")
    from ctypes import wintypes as w

    class Credential(c.Structure):
        _fields_ = [
            ("Flags", w.DWORD), ("Type", w.DWORD), ("TargetName", w.LPWSTR), ("Comment", w.LPWSTR),
            ("LastWritten", w.FILETIME), ("BlobSize", w.DWORD), ("Blob", c.c_void_p),
            ("Persist", w.DWORD), ("AttributeCount", w.DWORD), ("Attributes", c.c_void_p),
            ("TargetAlias", w.LPWSTR), ("UserName", w.LPWSTR),
        ]

    api = c.WinDLL("Advapi32", use_last_error=True)
    record = c.POINTER(Credential)()
    api.CredReadW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.POINTER(type(record))]
    api.CredWriteW.argtypes = [type(record), w.DWORD]
    api.CredDeleteW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD]
    api.CredFree.argtypes = [c.c_void_p]
    api.CredFree.restype = None
    target = f"{SERVICE}/{account}"
    if delete:
        if api.CredDeleteW(target, 1, 0) or c.get_last_error() == 1168:
            return None
    elif token is None:
        if api.CredReadW(target, 1, 0, c.byref(record)):
            try:
                return c.string_at(record.contents.Blob, record.contents.BlobSize).decode("ascii")
            finally:
                api.CredFree(record)
        if c.get_last_error() == 1168:
            return None
    else:
        secret = c.create_string_buffer(token.encode("ascii"))
        value = Credential(Type=1, TargetName=target, BlobSize=len(token),
                           Blob=c.cast(secret, c.c_void_p), Persist=2, UserName=SERVICE)
        if api.CredWriteW(c.byref(value), 0):
            return None
    raise OSError(f"无法访问 Windows 凭据管理器（{c.get_last_error()}）。")
