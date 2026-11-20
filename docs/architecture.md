# RemoteQGIS 技术选型与架构

## 开发环境及兼容性

2026-11-20 在 macOS 15.7.3（Apple Silicon）检查了 `/Applications/QGIS-LTR.app`。该应用是 x86_64 版本，内置 QGIS 3.40.4-Bratislava、Python 3.9.5、Qt 5.15.2、PyQt 5.15.4 和 GDAL 3.3.2。实测可导入 PyQGIS、Qt Network、PyQt5.QtWebSockets 及 GDAL Python 绑定。Qt WebSockets 在该发行包中由 `PyQt5.QtWebSockets` 提供，因此导入优先使用 QGIS 的 Qt 包装层，并为这类发行包保留回退入口。开发测试同时使用了实际 QGIS Desktop 进程与系统 Python，二者各自加载对应的依赖。

插件元数据允许 QGIS 3.40–3.x，运行基线为 Python 3.9 与具有 WebSockets 模块的 Qt 发行包。2026-11-20 的验证版本为 macOS QGIS 3.40.4，Windows 与其他 QGIS 3.x 发行包纳入双端验收。QGIS 负责提供 GIS 与 Qt 运行时，构建过程仅打包插件和 Host 所需的 Tornado 纯 Python 源码。插件遵循 QGIS 的标准入口、元数据与 ZIP 目录结构。 [QGIS 3.40 开发手册](https://docs.qgis.org/3.40/pdf/en/QGIS-3.40-PyQGISDeveloperCookbook-en.pdf) · [Qt 5.15 WebSockets API 源码](https://github.com/qt/qtwebsockets/blob/5.15/src/websockets/qwebsocket.h)

## 网络与服务

客户端使用 `QNetworkAccessManager` 处理 HTTP 请求，使用 `QWebSocket` 接收事件。两种连接均在 Qt 事件循环中异步执行。Host Mode 下，QGIS 主线程读取 `QgsProject` 并生成不可变项目摘要，Tornado 在后台线程中处理网络请求。项目摘要变化后，主线程将新的快照送入网络事件循环。网络线程维护连接与推送队列，整个流程与 QGIS 对象的线程归属一致。 [PyQGIS 后台任务与线程约束](https://docs.qgis.org/3.44/en/docs/pyqgis_developer_cookbook/tasks.html) · [Tornado WebSocket 文档](https://www.tornadoweb.org/en/branch6.5/websocket.html)

Tornado 6.5.10 以 Apache-2.0 发布，6.5 分支支持 Python 3.9，且 2026 年仍有维护发行。它在同一端口上提供 HTTP 与 WebSocket。部署采用一个网络线程和单进程 I/O，符合其 Windows 端适合小规模使用的约束。FastAPI（MIT）与 Uvicorn（BSD-3-Clause）均持续维护，支持 ASGI、模型验证及 WebSocket，适合将来独立部署的计算或瓦片服务。它们的当前版本及依赖需要额外处理 Python 版本与平台兼容。V0.1 消息规模较小，采用标准库数据类及 JSON 校验。 [Tornado 6.5 发布记录](https://www.tornadoweb.org/en/branch6.5/releases.html) · [Tornado 平台说明](https://www.tornadoweb.org/en/stable/) · [FastAPI](https://github.com/fastapi/fastapi) · [Uvicorn](https://github.com/Kludex/uvicorn)

Tailscale 独立于插件安装和配置。其核心仓库以 BSD-3-Clause 发布，持续维护，支持 Windows 与 macOS；连接可直连或经中继，两种传输均使用 WireGuard 加密。插件使用普通 TCP/IP 地址，同样可运行在其他可信网络。工作站默认只监听回环地址，跨网络使用时绑定加密网络对应的具体 IP。HTTPS / WSS 可连接外部 TLS 反向代理，客户端使用 Qt 的证书校验。 [Tailscale 仓库](https://github.com/tailscale/tailscale) · [Tailscale 连接方式](https://tailscale.com/docs/reference/connection-types)

## V0.1 接口与权限

| 接口 | 行为 |
| --- | --- |
| `GET /api/v1/info` | 返回协议版本与主机摘要。 |
| `GET /api/v1/events` | WebSocket 握手成功后发送 `host.info` 初始事件，随后在摘要变化时发送更新。 |

摘要包含 `qgis_version`、`hostname`、`platform` 和 `project_name`。项目名优先读取当前标题，空标题时读取文件基本名。两条接口均通过 `Authorization: Bearer` 请求头认证。认证在 WebSocket 握手前完成。带浏览器 Origin 的请求返回 403，错误或缺失令牌返回 401。令牌使用系统安全随机数生成，可手动更换，生命周期为本次 QGIS 会话。连接日志记录客户端或工作站的状态变化。服务接口的权限范围为读取项目摘要。

客户端分别对 HTTP 请求和 WebSocket 初始事件设定 5 秒期限。连接后每 10 秒发送一次 WebSocket Ping，收到 Pong 时更新往返延迟。断线后按 1 秒起步进行指数退避，间隔上限为 30 秒。认证失败、协议不兼容或 TLS 证书错误会停止当前尝试并显示原因。共享目录在启动时验证并规范化为绝对路径。V0.1 的网络接口仅提供摘要读取，文件浏览和写入权限由 V0.2 单独定义。

## GIS 组件与现有插件调查

候选项目的官方文档、仓库和许可证于 2026-11-20 核查。QGIS、GDAL、Rasterio、TiTiler、QFieldSync、NextGIS Connect 和 MapProxy 仓库均有 2026 年 11 月更新。Py-QGIS-WPS 仓库当年仍有更新，QGIS Remote Control 的公开记录停留在 2014 年。

| 项目 | 许可证与集成判断 |
| --- | --- |
| QGIS / PyQGIS | QGIS 核心为 GPL-2.0-or-later。直接使用 Desktop 提供的 PyQGIS 和 Qt。后续调查 `QgsDataItemProvider`、`QgsBrowserGuiModel` 和 `QgsCustomDropHandler` 的 Python 绑定，以确定远程文件浏览和拖放入口。[QGIS 源码](https://github.com/qgis/QGIS/tree/release-3_40) · [Browser GUI API](https://api.qgis.org/api/3.40/classQgsBrowserGuiModel.html) |
| GDAL / COG | GDAL 源码以 MIT 为主，具体构建受其依赖许可证约束。优先验证 QGIS 内置 GDAL 通过普通 GeoTIFF 的窗口与概览读取提供瓦片，COG 用于兼容已有分块与按需读取。原始像元查询通过真实栅格数据源实现。[GDAL 许可证](https://gdal.org/en/stable/license.html) · [COG 驱动](https://gdal.org/en/stable/drivers/raster/cog.html) |
| Rasterio / TiTiler | Rasterio 为 BSD-3-Clause，依赖 GDAL 二进制环境。TiTiler 为 MIT，通过 FastAPI 提供动态瓦片，依赖 Rasterio 等组件。若采用这条路线，应在独立环境中验证 Windows 原生依赖及 Python 版本。[Rasterio 许可证](https://github.com/rasterio/rasterio/blob/main/LICENSE.txt) · [TiTiler 项目](https://github.com/developmentseed/titiler) · [TiTiler 文档](https://developmentseed.org/titiler/) |
| QGIS Server / MapProxy | QGIS Server 可发布项目的 WMS、WMTS 等地图服务，需单独验证 Windows 部署条件。MapProxy 为 Apache-2.0，适合缓存和代理已有地图服务。两者均可用于后续对照测试。[QGIS Server](https://docs.qgis.org/3.40/en/docs/server_manual/index.html) · [MapProxy](https://github.com/mapproxy/mapproxy) |
| QFieldSync / NextGIS Connect | QFieldSync 仓库以 LGPL-3.0 为基础，可参考离线打包和变更同步。NextGIS Connect 为 GPL-2.0-or-later，可考察项目、样式及服务资源的映射，运行时依赖 NextGIS Web。[QFieldSync](https://github.com/opengisch/qfieldsync) · [NextGIS Connect](https://github.com/nextgis/nextgis_connect) |
| Py-QGIS-WPS / QGIS Processing | Py-QGIS-WPS 为 MPL-2.0，将 PyQGIS Processing 映射到 OGC WPS。后续可参考参数验证和隔离执行，优先以 `qgis_process` 或 QGIS 任务执行白名单中的算法。[Py-QGIS-WPS](https://github.com/3liz/py-qgis-wps) · [QGIS Processing Executor](https://docs.qgis.org/3.40/en/docs/user_manual/processing/standalone.html) |
| QGIS Remote Control / QGIS MCP Bridge | 前者是较早期的 QGIS 同步插件，仓库未提供可明确确认的许可证。后者展示了将请求调度至 QGIS 主线程的方式，其本机代码执行接口与本项目面向网络的权限范围不同。本项目采用固定接口，未引入这两个插件的代码。[QGIS Remote Control](https://plugins.qgis.org/plugins/QgsRemoteControl/) · [QGIS MCP Bridge](https://plugins.qgis.org/plugins/qgis_mcp_bridge/) |

V0.1 的原始代码选用 GPL-3.0-or-later。运行时调用系统已安装的 QGIS / PyQt，ZIP 随附 Tornado 的 Apache-2.0 原始许可和源码。Tailscale 作为独立程序部署。[QGIS 3.40 源码许可声明](https://github.com/qgis/QGIS/blob/release-3_40/src/core/project/qgsproject.h) · [FSF 许可证兼容说明](https://www.gnu.org/licenses/license-compatibility.html)

## 主项目、代理项目与后续版本

Host Plugin 持有真实的 `QgsProject`，Client Plugin 后续使用远端图层 ID 创建本地代理，Host Service 负责处理网络和 I/O。V0.1 用工作线程隔离网络持有状态。跨线程只传输数据结构。后续引入读写接口时，可通过 Qt 排队信号把项目操作送至主线程，耗时的瓦片生成和独立算法计算留在各自的工作线程或进程中。

| 版本 | 交付及进入下一阶段的验证 |
| --- | --- |
| V0.1 | 插件生命周期、环境诊断、认证、项目摘要、心跳及重连。使用两个独立 QGIS Desktop 配置验证，再进行 Windows / macOS 双端联调。 |
| V0.2 | 使用共享目录相对路径、文件类型过滤和路径权限验证，贯通 TIFF、Shapefile 和 GeoPackage 的远端导入与代理图层创建。Windows 验证大小写、重解析点和副文件行为。 |
| V0.3 | 复用 GDAL 与 QGIS 原生瓦片提供器，验证视口读取、颜色渲染、版本化缓存和独立像元查询。用大影像测试带宽和缓存表现。 |
| V0.4 | 以远端图层 ID 和项目修订号为基础，实现图层树、样式、视野及项目文件操作。 |
| V0.5 | 通过 QGIS 本地编辑缓冲区取得编辑事务，验证 GeoPackage 事务与 Shapefile 失败恢复。 |
| V0.6 | 实现 Remote Processing Provider，先打通少量明确支持的算法、输出路径与结果加载。 |
| V0.7–V1.0 | 完成广域网性能测试、Windows 与 macOS 安装测试、文档和发布验收。 |
