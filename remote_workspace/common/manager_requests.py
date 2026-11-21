# SPDX-License-Identifier: GPL-3.0-or-later

import json

from qgis.PyQt.QtCore import QObject, QTimer, QUrl
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest

from .protocol import validate_token
from .workspace import API


class ManagerRequests(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.network = QNetworkAccessManager(self)
        self.network.setProxy(QNetworkProxy(QNetworkProxy.NoProxy))

    def call(self, url, token, operation, data, callback):
        url = QUrl(url)
        url.setPath(API)
        request = QNetworkRequest(url)
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Authorization", ("Bearer " + validate_token(token)).encode("ascii"))
        reply = self.network.post(request, json.dumps({"operation": operation, "data": data}).encode())
        reply.setReadBufferSize(16385)
        timer = QTimer(reply, interval=3500, singleShot=True)
        timer.timeout.connect(reply.abort)
        reply.readyRead.connect(lambda: reply.abort() if reply.bytesAvailable() > 16384 else None)

        def finished():
            timer.stop()
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            data = None
            if status == 200:
                try:
                    result = json.loads(bytes(reply.readAll()))
                    if result.get("manager") == 1 and result.get("state") in {
                        "stopped", "starting", "running", "stopping", "error"
                    }:
                        data = result
                except (ValueError, AttributeError, RecursionError):
                    pass
            reply.deleteLater()
            callback(data, status)

        reply.finished.connect(finished)
        timer.start()
