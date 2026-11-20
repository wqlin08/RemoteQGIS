# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from remote_workspace.common.edits import Recovery, edit_delta


class EditTests(unittest.TestCase):
    def test_feature_delta_preserves_identity(self):
        a = {"id": "alpha", "geometry": None, "values": ["keep"]}
        b = {"id": "beta", "geometry": None, "values": ["removed"]}
        c = {"id": "gamma", "geometry": None, "values": ["created"]}
        base = {"revision": "source-version", "fields": [["name", 10, 0, 0, 0]], "features": [a, b]}
        working = {"fields": base["fields"], "features": [dict(a, values=["changed"]), c]}
        delta = edit_delta(base, working)
        self.assertEqual(delta["base"], "source-version")
        self.assertEqual(delta["delete"], ["beta"])
        self.assertEqual(delta["add"], [c])
        self.assertEqual(delta["update"], [dict(a, values=["changed"])])

    def test_schema_change_transmits_existing_attributes(self):
        features = [{"id": "feature", "geometry": None, "values": ["P"]}]
        base = {"revision": "v1", "fields": [["old", 10, 0, 0, 0]], "features": features}
        updated = {"fields": [["new", 10, 0, 0, 0]], "features": features}
        self.assertEqual(edit_delta(base, updated)["update"], features)

    def test_recovery_uses_dataset_and_endpoint(self):
        with TemporaryDirectory() as directory:
            directory = Path(directory) / "recovery"
            recovery = Recovery(directory)
            descriptor = {"path": "道路.gpkg", "table": "道路"}
            first = recovery.path("http://host:8765", descriptor)
            second = recovery.path("http://host2:8765", descriptor)
            self.assertNotEqual(first, second)
            self.assertIsNone(recovery.read(first))
            document = {"patch": {"base": "123"}, "working": {"values": ["未提交"]}}
            recovery.store(first, document)
            self.assertEqual(recovery.read(first), document)
            self.assertEqual(list(directory.iterdir()), [first])
