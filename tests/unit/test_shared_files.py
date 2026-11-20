# SPDX-License-Identifier: GPL-3.0-or-later

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from remote_workspace.common.workspace import WorkspaceError
from remote_workspace.host.files import SharedFiles


class SharedFilesTest(unittest.TestCase):
    def test_root_confinement(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            shared = root / "shared"
            shared.mkdir()
            (shared / "link").symlink_to(root, target_is_directory=True)
            files = SharedFiles(shared)
            for value in ("../a", "D:/a.tif", "/a", r"data\file", "a/..", "CON", "link/a"):
                with self.subTest(value=value), self.assertRaises(WorkspaceError):
                    files.path(value, exists=False)
            self.assertEqual(files.list(""), [])

    def test_shapefile_move_keeps_sidecars(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            files = SharedFiles(root)
            for suffix in ("shp", "dbf", "shx", "cpg"):
                (root / f"x.{suffix}").touch()
            files.move(root / "x.shp", root / "y.shp")
            self.assertEqual(sorted(path.name for path in root.iterdir()),
                             ["y.cpg", "y.dbf", "y.shp", "y.shx"])
            files.recycle(root / "y.shp")
            self.assertEqual(files.list(""), [])
