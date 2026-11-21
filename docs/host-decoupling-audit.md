# Host 解耦审查

稳定基线为 `v1.0.0-rc1`，对应 `0b05130934d33a8e376bd919afa6df81ccad1e81`。开发前工作区干净。基线通过 10 项单元测试及 QGIS 3.40.4 的两进程 Desktop 阶段 5 回归。稳定 ZIP 可从原标签独立构建。

## 启动与线程

`classFactory → RemoteWorkspacePlugin._show_workspace → WorkspacePanel._start_host` 在 Desktop 主线程创建 `HostWorkspace`、`Dispatcher` 和 `HostService`。工作区在 QGIS 主线程操作项目和图层。`HostService._run` 启动独立 Tornado asyncio 线程并创建 `HostAPI`。HTTP 业务请求经 `Dispatcher.submit` 转化为带 Future 的 Qt 排队信号，由 `Dispatcher._run` 在主线程执行。Panel 的 200 ms 计时器及 `Dispatcher.changed` 发布项目摘要、修订号及栅格描述。原始栅格文件和像元查询由网络侧 GDAL 工作线程处理。Processing 框架管理算法任务，完成回调处理输出文件并在项目中加载图层。

## 依赖分类

| 类别 | 文件与调用点 | 复用与回归风险 |
| --- | --- | --- |
| A | `host/config.py`、`files.py`、`rasters.py`、`raster_source.py`、`service.py`、`api.py` 与 `common/protocol.py`、`workspace.py`、`edits.py` | 复用配置、路径检查、GDAL 读取、HTTP Range、认证和 WebSocket。保留路径、端口及原始数据行为。 |
| B | `host/project.py` 的图层导入、样式、图层树和状态修订；`project_files.py` 的 QGS/QGZ 读写与数据源校验；`vectors.py` 的快照和事务；`processing.py` 的目录、任务和状态 | 复用 QGIS Core 和已白名单化的算法。Qt 主线程及事件循环是正确调用的前提。 |
| C | `HostWorkspace.current_view`、`view`；`ProjectFiles.__call__`、`default_extent`；`Dispatcher._run`、`WorkspacePanel._publish_snapshot` | 适配画布和独立项目视野。Dispatcher 本身基于 QtCore，独立进程可重用。视野变换、项目读写和关闭时后台任务收尾是主要兼容风险。 |
| D | `plugin.py` 的菜单及 Dock；`ui/panel.py`、`ui/browser.py`、`ui/vectors.py` 的 Desktop 操作；`client/proxy.py` 的画布、活动图层与图层树；`client/edits.py`、`client/online.py` 的编辑与拖拽；`client/processing.py` 的工具箱 | 保留原界面生命周期、矢量编辑缓冲区、图层代理和工具箱。展示端可以在独立 Desktop 配置中使用代理层，写入权限由服务端控制。 |

`client/connection.py` 的 Qt Network 和 Qt WebSockets 可继续连接现有协议。`client/raster_sources.py` 使用回环 HTTP 中转和 GDAL VRT。`common/layers.py`、`vector.py` 与 `processing.py` 保持现有序列化和参数映射。

## 验证顺序

先用独立 PyQGIS 验证 GeoTIFF、GeoPackage、项目文件及任务回调，再接入相同 Host 请求调度和网络服务。现有 Desktop 测试覆盖栅格显示、原始像元、矢量编辑、断线恢复、Processing、在线图层同步与端口释放。增加对项目默认视野的直接断言后，以原实现和适配后的实现各运行一次。

管理程序与 QGIS Desktop 使用各自进程。展示端通过带只读权限的连接订阅后台项目，工作站数据由后台 Host 管理。已有双端 Desktop 工作流通过独立插件 ZIP 继续交付。
