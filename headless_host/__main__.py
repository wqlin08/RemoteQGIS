# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
from pathlib import Path
import sys

from remote_workspace.common.protocol import validate_token
from remote_workspace.host.config import HostConfig

from .bootstrap import application


def main():
    parser = argparse.ArgumentParser(description="RemoteQGIS Headless Host")
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--project", default="")
    parser.add_argument("--viewer", action="store_true")
    args = parser.parse_args()
    try:
        token = validate_token(sys.stdin.readline())
        viewer_token = validate_token(sys.stdin.readline()) if args.viewer else ""
        if viewer_token == token:
            raise ValueError("展示令牌须独立于工作站访问令牌。")
        config = HostConfig.validate(args.address, args.port, args.directory, token)
        app = application(args.profile)
        from .runtime import serve

        try:
            return serve(app, config, args.project, viewer_token=viewer_token)
        finally:
            app.exitQgis()
    except (ImportError, RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
