# SPDX-License-Identifier: GPL-3.0-or-later

import json
from typing import Optional

from qgis.PyQt.QtCore import QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkProxy, QNetworkRequest

from ..common.workspace import API, MAX_DOCUMENT
from ..common.protocol import (
    EVENTS_PATH, INFO_PATH, MAX_MESSAGE_BYTES, ProtocolError, decode_host_info, validate_token,
)


def websocket_class():
    try:
        from qgis.PyQt.QtWebSockets import QWebSocket
    except ImportError:
        # Some QGIS 3.x bundles ship only the underlying PyQt5 module.
        from PyQt5.QtWebSockets import QWebSocket
    return QWebSocket


def endpoint(host: str, port: int, https: bool) -> QUrl:
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host or any(char in host for char in "/\\?#@[]%") or any(c.isspace() for c in host):
        raise ValueError("请输入主机名或 IP 地址，端口请单独填写。")
    url = QUrl()
    url.setScheme("https" if https else "http")
    url.setHost(host)
    url.setPort(port)
    if not url.isValid() or not url.host() or not 1 <= port <= 65535:
        raise ValueError("主机名、IP 地址或端口无效。")
    return url


class Connection(QObject):
    status_changed = pyqtSignal(str, str)
    info_changed = pyqtSignal(object)
    workspace_changed = pyqtSignal(int)
    latency_changed = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._network = QNetworkAccessManager(self)
        self._proxy = QNetworkProxy(QNetworkProxy.NoProxy)
        self._network.setProxy(self._proxy)
        self._deadline = QTimer(self, singleShot=True, interval=5000)
        self._deadline.timeout.connect(self._timed_out)
        self._retry = QTimer(self, singleShot=True)
        self._retry.timeout.connect(self._probe)
        self._heartbeat = QTimer(self, interval=10000)
        self._heartbeat.timeout.connect(self._ping)
        self._url: Optional[QUrl] = None
        self._token = ""
        self._requests = {}
        self._reply = None
        self._socket = None
        self._awaiting_pong = False
        self._ready = False
        self._retry_delay = 1

    @property
    def active(self) -> bool:
        return self._url is not None

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def authorization(self) -> bytes:
        return b"Bearer " + self._token.encode("ascii")

    @property
    def origin(self) -> str:
        return self._url.toString() if self._url is not None else ""

    def start(self, url: QUrl, token: str):
        token = validate_token(token)
        self._websocket = websocket_class()
        self._reset()
        self._url = QUrl(url)
        self._token = token
        self._retry_delay = 1
        self._probe()

    def stop(self):
        self._fail("已断开。", "idle")

    def _request(self, path: str, *, websocket: bool = False) -> QNetworkRequest:
        url = QUrl(self._url)
        url.setPath(path)
        if websocket:
            url.setScheme("wss" if url.scheme() == "https" else "ws")
        request = QNetworkRequest(url)
        request.setRawHeader(b"Authorization", self.authorization)
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute, QNetworkRequest.ManualRedirectPolicy)
        return request

    def call(self, operation: str, arguments: dict, callback):
        if not self._ready:
            callback(None, "工作站未连接。")
            return
        try:
            body = json.dumps({"operation": operation, "data": arguments}, ensure_ascii=False,
                              allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            callback(None, "请求包含无效参数。")
            return
        if len(body) > MAX_DOCUMENT:
            callback(None, "请求超过传输大小限制。")
            return
        request = self._request(API)
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Accept", b"application/json")
        reply = self._network.post(request, body)
        reply.setReadBufferSize(MAX_DOCUMENT + 1)
        timer = QTimer(reply, singleShot=True, interval=45000)
        self._requests[reply] = (timer, callback)
        timer.timeout.connect(self._abort_rpc)
        reply.readyRead.connect(self._check_rpc_size)
        reply.finished.connect(self._finish_rpc)
        timer.start()

    def _abort_rpc(self):
        self.sender().parent().abort()

    def _check_rpc_size(self):
        reply = self.sender()
        if reply.bytesAvailable() > MAX_DOCUMENT:
            reply.abort()

    def _finish_rpc(self):
        reply = self.sender()
        timer, callback = self._requests.pop(reply)
        timer.stop()
        reply.readyRead.disconnect(self._check_rpc_size)
        error = ""
        document = None
        try:
            document = json.loads(bytes(reply.readAll()))
            if not isinstance(document, dict):
                raise ValueError
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            if status != 200:
                error = document.get("error") or f"工作站返回 HTTP {status}。"
        except (UnicodeError, ValueError, RecursionError):
            error = reply.errorString() if reply.error() else "工作站返回了无效的响应。"
        reply.deleteLater()
        callback(None if error else document, error)

    def _probe(self):
        self.status_changed.emit("connecting", "正在验证 HTTP 连接…")
        request = self._request(INFO_PATH)
        request.setRawHeader(b"Accept", b"application/json")
        self._reply = self._network.get(request)
        self._reply.setReadBufferSize(MAX_MESSAGE_BYTES + 1)
        self._reply.readyRead.connect(self._check_size)
        self._reply.finished.connect(self._probe_finished)
        self._reply.sslErrors.connect(self._tls_failed)
        self._deadline.start()

    def _check_size(self):
        if self._reply.bytesAvailable() > MAX_MESSAGE_BYTES:
            self._fail("远端消息超出大小限制。")

    def _probe_finished(self):
        reply, self._reply = self._reply, None
        self._deadline.stop()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        error = reply.errorString() if reply.error() else ""
        reply.deleteLater()
        if status in (401, 403):
            self._fail("远端拒绝访问，请核对访问令牌和服务地址。")
        elif status in (None, 502, 503, 504):
            self._reconnect(error or "服务暂时不可用。")
        elif status != 200:
            self._fail(f"远端返回 HTTP {status}，请核对服务地址及协议版本。")
        elif error:
            self._reconnect(error)
        else:
            try:
                decode_host_info(raw)
            except ProtocolError as exc:
                self._fail(str(exc))
                return
            self._open_events()

    def _open_events(self):
        self.status_changed.emit("connecting", "HTTP 认证通过，正在连接 WebSocket…")
        socket = self._websocket(parent=self)
        self._socket = socket
        socket.setProxy(self._proxy)
        socket.setMaxAllowedIncomingFrameSize(MAX_MESSAGE_BYTES)
        socket.setMaxAllowedIncomingMessageSize(MAX_MESSAGE_BYTES)
        socket.textMessageReceived.connect(self._receive)
        socket.pong.connect(self._pong)
        socket.disconnected.connect(self._socket_disconnected)
        socket.sslErrors.connect(self._tls_failed)
        error_signal = getattr(socket, "errorOccurred", None)
        (error_signal if error_signal is not None else socket.error).connect(self._socket_error)
        self._deadline.start()
        socket.open(self._request(EVENTS_PATH, websocket=True))

    def _receive(self, message: str):
        try:
            info = decode_host_info(message.encode("utf-8"), event=True)
        except (ProtocolError, UnicodeError) as exc:
            self._fail(str(exc))
            return
        if not self._ready:
            self._deadline.stop()
            self._ready = True
            self._retry_delay = 1
            self.status_changed.emit("connected", "HTTP 和 WebSocket 均已连接。")
            self._heartbeat.start()
            self._ping()
        self.info_changed.emit(info)
        revision = json.loads(message).get("revision")
        if type(revision) is int and revision >= 0:
            self.workspace_changed.emit(revision)

    def _ping(self):
        if self._awaiting_pong:
            self._reconnect("WebSocket 心跳超时。")
        else:
            self._awaiting_pong = True
            self._socket.ping(b"rqgis")

    def _pong(self, elapsed: int, payload):
        if bytes(payload) == b"rqgis":
            self._awaiting_pong = False
            self.latency_changed.emit(elapsed)

    def _socket_disconnected(self):
        self._reconnect("远端 WebSocket 已断开。")

    def _socket_error(self, _error):
        self._reconnect(self._socket.errorString())

    def _timed_out(self):
        self._reconnect("连接请求超时。")

    def _tls_failed(self, _errors):
        self._fail("TLS 证书验证失败，请检查远端地址和证书。")

    def _reconnect(self, detail: str):
        self._clear_channels()
        delay = self._retry_delay
        self._retry_delay = min(delay * 2, 30)
        self.latency_changed.emit(-1)
        self.status_changed.emit("retry", f"{detail} {delay} 秒后重试。")
        self._retry.start(delay * 1000)

    def _clear_channels(self):
        self._deadline.stop()
        self._heartbeat.stop()
        self._awaiting_pong = False
        self._ready = False
        for channel in (self._reply, self._socket):
            if channel is not None:
                channel.blockSignals(True)
                channel.abort()
                channel.deleteLater()
        self._reply = self._socket = None

    def _reset(self):
        self._retry.stop()
        self._clear_channels()
        self._url = None
        self._token = ""
        for reply in tuple(self._requests):
            reply.abort()

    def _fail(self, detail: str, state: str = "error"):
        self._reset()
        self.latency_changed.emit(-1)
        self.status_changed.emit(state, detail)
