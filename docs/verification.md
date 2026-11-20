# RemoteQGIS V0.1 验证记录

2026-11-20。平台为 macOS 15.7.3，QGIS Desktop 3.40.4 LTR，内置 Python 3.9.5、Qt 5.15.2、PyQt 5.15.4 和 GDAL 3.3.2。

| 验证类别 | 结果 |
| --- | --- |
| 独立单元测试 | 五项通过，覆盖 HTTP 与事件消息、消息结构和长度校验、访问令牌及 Host 配置。 |
| QGIS 原生 ZIP 安装 | 两个独立的 QGIS Desktop 实例分别通过 QGIS 插件安装器安装同一份 ZIP，均可从插件菜单打开面板。 |
| 本机双端连接 | 使用回环网络，验证 HTTP 项目摘要、拒绝错误令牌、WebSocket 握手鉴权、拒绝浏览器 Origin、显示心跳延迟及项目变化推送。 |
| 故障与恢复 | 使用不响应的 TCP 监听器验证客户端请求超时。验证工作站端口占用后的重新启动、停止期间的客户端提示、重启后的自动重连，以及卸载后端口释放。 |
| Windows 实际环境及广域网 | 待使用 Windows QGIS 工作站与 macOS 客户端验收。 |
| 远程文件与 GIS 操作 | 远程目录、数据导入、遥感影像分块访问、项目保存、编辑事务和 Processing 属于后续阶段。 |

## 复现

```sh
python3 -m unittest discover -s tests/unit -v
python3 scripts/build_plugin.py
python3 scripts/test_desktop.py --qgis /Applications/QGIS-LTR.app/Contents/MacOS/QGIS --zip dist/RemoteQGIS-0.1.0.zip
```

集成测试使用临时 QGIS 用户配置和测试令牌。该 macOS 发行包运行桌面测试时使用默认窗口后端，Qt offscreen 后端无法初始化桌面 OpenGL。

## Windows 与 macOS 实机验收

在 Windows QGIS 使用同一个 ZIP 安装并打开 Host 面板，绑定工作站的 Tailscale IP，选择共享目录并启动。macOS 在同一 tailnet 内填入 Windows IP、端口和令牌，检查远端系统、版本及项目名。依次验证拒绝错误令牌、更新 Windows 项目标题和重启 Host 后的状态变化。验收时记录 Windows 的 QGIS、Qt 和 Python 版本，以及连接使用的 Tailscale 直连或中继方式。
