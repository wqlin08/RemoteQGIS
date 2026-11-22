# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise management HTTP replies and Qt teardown with the packaged runtime."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread, current_thread
import time

from qgis.PyQt.QtCore import QCoreApplication, QTimer, QUrl

from remote_workspace.common.manager_requests import MAX_REPLY, ManagerRequests
from remote_workspace.common.protocol import new_token


def main():
    token = new_token()
    owner = current_thread()
    status_document = {"manager": 1, "state": "running"}
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            operation = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["operation"]
            seen.append(self.path)
            if operation == "slow":
                time.sleep(4)
                return
            status, payload = 200, json.dumps(status_document).encode()
            if self.headers.get("Authorization") != "Bearer " + token:
                status, payload = 401, b""
            elif operation == "redirect":
                status, payload = 307, b""
            elif operation == "oversized":
                payload += b" " * MAX_REPLY
            self.send_response(status)
            if status == 307:
                self.send_header("Location", "/must-not-follow")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if payload:
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

        def log_message(self, *_args):
            pass

    app = QCoreApplication([])
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = QUrl(f"http://127.0.0.1:{server.server_port}")
            requests = ManagerRequests()

            def call(operation, key):
                values = []

                def finish(value, status):
                    values.append((value, status, current_thread()))
                    app.quit()

                watchdog = QTimer(singleShot=True, interval=5000)
                watchdog.timeout.connect(app.quit)
                watchdog.start()
                requests.call(url, key, operation, {}, finish)
                app.exec()
                assert len(values) == 1 and values[0][2] is owner, values
                return values[0][:2]

            assert call("manager_status", token) == (status_document, 200)
            assert call("manager_status", new_token()) == (None, 401)
            assert call("redirect", token) == (None, 307)
            assert call("oversized", token) == (None, 200)
            assert call("slow", token) == (None, None)
            late = []
            requests.call(url, token, "manager_status", {}, lambda *_: late.append(True))
            requests.close()
            QTimer.singleShot(200, app.quit)
            app.exec()
            assert not late and "/must-not-follow" not in seen
        finally:
            server.shutdown()
    print("PASS: management HTTP authentication, redirect, size and timeout, no callback after close", flush=True)


if __name__ == "__main__":
    main()
