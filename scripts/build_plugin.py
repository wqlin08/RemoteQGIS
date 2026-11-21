# SPDX-License-Identifier: GPL-3.0-or-later

"""Build the installable plugin with Tornado's platform-independent Python sources."""

import argparse
from configparser import ConfigParser
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import tarfile
from urllib.request import urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TORNADO_URL = (
    "https://files.pythonhosted.org/packages/06/61/"
    "53d562a57b28c08eda40b258c0f975e360541943ad7c7bef897a40caafda/tornado-6.5.10.tar.gz"
)
TORNADO_SHA256 = "a6b1ccd08c04b4a06fb5aeb381be99de5ad1e5375c1785e31d78c880feb57687"


def package_files():
    source = ROOT / "remote_workspace"
    for file in sorted(source.rglob("*")):
        if file.is_file() and file.suffix in (".py", ".txt", ".png") and "_vendor" not in file.parts:
            yield file.relative_to(ROOT).as_posix(), file.read_bytes()
    for name in ("LICENSE", "README.md", "CHANGELOG.md"):
        yield f"remote_workspace/{name}", (ROOT / name).read_bytes()

    for page in sorted((ROOT / "docs").glob("*.md")):
        yield f"remote_workspace/docs/{page.name}", page.read_bytes()

    cache = ROOT / "build" / "tornado-6.5.10.tar.gz"
    dependency = cache.read_bytes() if cache.exists() else b""
    if sha256(dependency).hexdigest() != TORNADO_SHA256:
        with urlopen(TORNADO_URL, timeout=30) as response:
            dependency = response.read()
    if sha256(dependency).hexdigest() != TORNADO_SHA256:
        raise RuntimeError("Tornado source archive hash does not match the pinned release.")
    cache.parent.mkdir(exist_ok=True)
    cache.write_bytes(dependency)
    with tarfile.open(fileobj=BytesIO(dependency), mode="r:gz") as archive:
        for member in sorted(archive.getmembers(), key=lambda entry: entry.name):
            path = PurePosixPath(*PurePosixPath(member.name).parts[1:])
            if not member.isfile() or ".." in path.parts:
                continue
            if str(path) in ("LICENSE", "NOTICE"):
                yield f"remote_workspace/_vendor/tornado-{path.name}", archive.extractfile(member).read()
            if path.parts[0] == "tornado" and path.suffix == ".py" and "test" not in path.parts:
                yield f"remote_workspace/_vendor/{path}", archive.extractfile(member).read()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    config = ConfigParser()
    config.read(ROOT / "remote_workspace/metadata.txt", encoding="utf-8")
    output = args.output_dir / f"RemoteQGIS-{config['general']['version']}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    files = list(package_files())
    with ZipFile(output, "w") as archive:
        for name, data in files:
            entry = ZipInfo(name)
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    print(output)


if __name__ == "__main__":
    main()
