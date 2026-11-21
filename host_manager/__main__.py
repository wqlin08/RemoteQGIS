# SPDX-License-Identifier: GPL-3.0-or-later

import argparse
from pathlib import Path
import sys

from remote_workspace.common.credentials import load_token
from remote_workspace.common.protocol import validate_token
from remote_workspace.host.qgis_info import validate_environment

from .settings import ACCOUNT, LOCAL_CONTROL, Settings, directory


def main():
    parser = argparse.ArgumentParser(description="RemoteQGIS Host Manager")
    parser.add_argument("--state-dir", type=Path, default=directory())
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--start-worker", action="store_true")
    parser.add_argument("--test-credentials-stdin", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--smoke-test-ui", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        validate_environment()
        home = args.state_dir.expanduser().resolve()
        settings = Settings.read(home / "settings.json")
        if args.daemon:
            from qgis.PyQt.QtCore import QCoreApplication
            from .daemon import run

            app = QCoreApplication([])
            if args.test_credentials_stdin:
                token, control = (validate_token(sys.stdin.readline()) for _ in range(2))
            else:
                token, control = load_token(ACCOUNT), load_token(LOCAL_CONTROL)
            if token == control:
                raise ValueError("本地控制凭据须与访问令牌独立。")
            return run(app, home, settings, token, control, args.start_worker)
        from qgis.PyQt.QtCore import QTimer
        from qgis.PyQt.QtGui import QIcon
        from qgis.PyQt.QtWidgets import QApplication
        from .process import SOURCE
        from .window import Window

        app = QApplication([])
        logo = Path(__file__).with_name("logo.png")
        if not logo.is_file():
            logo = SOURCE / "remote_workspace/icons/logo.png"
        app.setWindowIcon(QIcon(str(logo)))
        window = Window(home, settings)
        window.show()
        if args.smoke_test_ui:
            QTimer.singleShot(700, app.quit)
        return app.exec()
    except (ImportError, RuntimeError, ValueError, OSError, TypeError) as exc:
        if not args.daemon:
            from qgis.PyQt.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(None, "RemoteQGIS Host Manager", str(exc))
        print(str(exc), file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
