# SPDX-License-Identifier: GPL-3.0-or-later

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from remote_workspace.common.protocol import (
    MAX_MESSAGE_BYTES, HostInfo, ProtocolError, authorized, decode_host_info, new_token, validate_token,
)
from remote_workspace.host.config import HostConfig


class ProtocolTests(unittest.TestCase):
    def test_http_and_event_roundtrip(self):
        info = HostInfo("3.40.4", "workstation", "Windows", "四川遥感")
        for event in (False, True):
            encoded = json.dumps(info.message(event), ensure_ascii=False).encode()
            self.assertEqual(decode_host_info(encoded, event=event), info)

    def test_invalid_messages(self):
        for raw in (b"[]", b'{"protocol": true}', b'{"protocol": 2}', b'{"protocol": 1}',
                    b"\xff", b"[" * 1200, b" " * (MAX_MESSAGE_BYTES + 1)):
            with self.subTest(prefix=raw[:30]), self.assertRaises(ProtocolError):
                decode_host_info(raw)

    def test_event_type_and_field_bounds(self):
        info = HostInfo("3.40.4", "ws", "macOS", "a" * 513)
        with self.assertRaises(ProtocolError):
            decode_host_info(json.dumps(info.message(event=True)).encode(), event=True)
        info = HostInfo("3.40.4", "ws", "macOS", "项目")
        with self.assertRaises(ProtocolError):
            decode_host_info(json.dumps(info.message()).encode(), event=True)

    def test_tokens(self):
        token = new_token()
        self.assertEqual(validate_token(token), token)
        self.assertTrue(authorized(f"Bearer {token}", token))
        for candidate in ("", token, f"Bearer {new_token()}", f"Bearer {token}?", f"Bearer {token}\n"):
            self.assertFalse(authorized(candidate, token))
        for candidate in ("short", "密" * 32, "a" * 129):
            with self.assertRaises(ValueError):
                validate_token(candidate)

    def test_host_config(self):
        with TemporaryDirectory() as directory:
            token = new_token()
            config = HostConfig.validate("::1", 8765, directory, token)
            self.assertEqual(config.shared_directory, Path(directory).resolve())
            self.assertNotIn(token, repr(config))
            for address, port, folder in (
                ("0.0.0.0", 8765, directory), ("localhost", 8765, directory),
                ("::1", 0, directory), ("127.0.0.1", 8765, ""),
                ("::1", 8765, str(Path(directory) / "missing")),
            ):
                with self.subTest(address=address, port=port, folder=folder), self.assertRaises(ValueError):
                    HostConfig.validate(address, port, folder, token)


if __name__ == "__main__":
    unittest.main()
