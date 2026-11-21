# SPDX-License-Identifier: GPL-3.0-or-later

"""Build the macOS app bundle and cross-compile the Windows GUI launcher."""

import argparse
from configparser import ConfigParser
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

from build_plugin import ROOT, package_files

MAC_NAME = "RemoteQGIS Host Manager.app"
WINDOWS_NAME = "RemoteQGIS Host Manager"


def stage_sources(source, plugin):
    for name, contents in plugin:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    for directory in ("headless_host", "host_manager"):
        shutil.copytree(ROOT / directory, source / directory, ignore=shutil.ignore_patterns("__pycache__"))


def macos(stage, output, version, plugin):
    bundle = stage / MAC_NAME / "Contents"
    for child in ("MacOS", "Resources"):
        (bundle / child).mkdir(parents=True)
    launcher = bundle / "MacOS/RemoteQGIS Host Manager"
    shutil.copy2(ROOT / "packaging/run-macos", launcher)
    launcher.chmod(0o755)
    (bundle / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleExecutable": launcher.name,
        "CFBundleName": "RemoteQGIS Host Manager",
        "CFBundleDisplayName": "RemoteQGIS Host Manager",
        "CFBundleIdentifier": "org.remoteqgis.host-manager.app",
        "CFBundleVersion": version.split("-")[0],
        "CFBundleShortVersionString": version.split("-")[0],
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
    }))
    stage_sources(bundle / "Resources/app", plugin)
    if sys.platform == "darwin":
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(bundle.parent)], check=True)
        subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(bundle.parent)], check=True)
        subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(bundle.parent), str(output)], check=True)
    else:
        raise RuntimeError("Building the macOS app requires macOS codesign and ditto.")


def windows(stage, output, go, plugin):
    target = stage / WINDOWS_NAME
    target.mkdir()
    launcher = target / (WINDOWS_NAME + ".exe")
    subprocess.run([
        go, "build", "-trimpath", "-ldflags", "-s -w -H windowsgui",
        "-o", str(launcher), str(ROOT / "packaging/windows/launcher.go"),
    ], env=dict(os.environ, GOOS="windows", GOARCH="amd64", CGO_ENABLED="0",
                GO111MODULE="off", GOTOOLCHAIN="local"), check=True)
    stage_sources(target / "app", plugin)
    shutil.copyfile(ROOT / "docs/host-manager.md", target / "使用说明.md")
    with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(target.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(stage).as_posix())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--platform", choices=("both", "macos", "windows"), default="both")
    parser.add_argument("--go", default="go")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    config = ConfigParser()
    config.read(ROOT / "remote_workspace/metadata.txt", encoding="utf-8")
    version = config["general"]["version"]
    plugin = list(package_files())
    with TemporaryDirectory(prefix="remoteqgis-build-") as directory:
        stage = Path(directory)
        if args.platform in ("both", "macos"):
            path = output / f"RemoteQGIS-Host-Manager-macOS-{version}.zip"
            macos(stage, path, version, plugin)
            print(path)
        if args.platform in ("both", "windows"):
            path = output / f"RemoteQGIS-Host-Manager-Windows-x64-{version}.zip"
            windows(stage, path, args.go, plugin)
            print(path)


if __name__ == "__main__":
    main()
