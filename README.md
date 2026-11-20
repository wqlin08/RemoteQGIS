# RemoteQGIS

RemoteQGIS 是面向 QGIS Desktop 3.x 的远程 GIS 工作空间插件。V0.1 同时提供工作站端（Host）和客户端（Client），可验证双端连通性，并显示工作站的 QGIS 版本、主机系统、当前项目名和 WebSocket 往返延迟。工作站内切换项目后，客户端会收到更新。

## 安装及连接

1. 在两台设备的 QGIS 3.40 或更新的 3.x 版本中，进入「插件 → 管理并安装插件 → 从 ZIP 安装」，选择同一份 `RemoteQGIS-0.1.0.zip`。
2. 在工作站打开「插件 → RemoteQGIS」，进入 **Host · 工作站**。选择一个已存在的共享目录。跨网络使用时，在监听地址输入工作站的 Tailscale IP，端口默认是 `8765`。点击「启动服务」，将访问令牌传给客户端。
3. 在客户端打开 **Client · 客户端**，填入工作站的 IP 或主机名、端口和令牌，点击「连接」。连接通过后，面板显示远端工作站信息。停止并重启 Host 服务可验证自动重连。
4. 单机试用可使用两个 QGIS 会话与 `127.0.0.1`。通过 Tailscale IP 直连时保持 HTTPS / WSS 复选框关闭，传输由 Tailscale 隧道加密。若通过提供有效 TLS 证书的反向代理访问，则启用 HTTPS / WSS。

V0.1 的共享目录配置供后续文件系统使用，本版两条接口只读取工作站和项目摘要。服务默认监听回环地址。普通 HTTP/WS 连接应通过受信任的局域网或加密网络使用。Host 停止或插件卸载后会关闭监听，令牌只保留在本次 QGIS 会话内。

## 开发与验证

```sh
python3 -m unittest discover -s tests/unit -v
python3 scripts/build_plugin.py
python3 scripts/test_desktop.py --qgis /Applications/QGIS-LTR.app/Contents/MacOS/QGIS --zip dist/RemoteQGIS-0.1.0.zip
```

构建脚本从 PyPI 下载并校验固定版本的 Tornado 源码，将其纯 Python 文件及许可证打入 ZIP。插件运行时使用 QGIS 自带 Python 与 Qt 环境，安装 ZIP 即可。

[技术选型与架构](docs/architecture.md) · [验收范围](docs/verification.md) · [变更记录](CHANGELOG.md)

RemoteQGIS 源码以 GPL-3.0-or-later 发布，ZIP 中的 Tornado 源码保留 Apache-2.0 许可。
