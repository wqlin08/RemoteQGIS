# RemoteQGIS Host Manager

Host Manager 使用工作站已安装的 QGIS 3.40 或更高的 3.x 版本。管理窗口、无界面 GIS Host 和可选的 QGIS Desktop 展示是三个进程。关闭管理窗口后，后台服务继续运行。在管理窗口选择「退出管理服务」会停止后台 Host。

## 安装与配置

macOS 解压发行 ZIP，将 `RemoteQGIS Host Manager.app` 放入「应用程序」后打开。启动器查找 `QGIS-LTR.app` 和 `QGIS.app`。自动检测失败时可在窗口选择已安装的 QGIS。也可通过 `REMOTEQGIS_QGIS_APP` 指定应用路径。Apple Silicon 使用 Intel 版 QGIS 发行包时，由系统现有的 Rosetta 环境运行。

Windows x64 解压完整文件夹到长期保留的位置，双击 `RemoteQGIS Host Manager.exe`。启动器查找 QGIS 3.x MSI 或 OSGeo4W 安装中的 QGIS Python 启动脚本。自动检测失败时可选择 QGIS 安装目录下的 `bin/python-qgis-ltr.bat` 或 `bin/python-qgis.bat`。自定义根目录也可通过 `REMOTEQGIS_QGIS_ROOT` 指定，例如 `D:\QGIS 3.44.15`。程序使用随 QGIS 安装的 Python、Qt、GDAL 和 Processing。插件 ZIP 在客户端通过 QGIS 的「从 ZIP 安装」单独安装。

在管理窗口选择共享目录，可选共享目录中的启动项目。跨设备连接时，监听地址填写工作站自己的 Tailscale 或 VPN 地址。GIS 端口默认为 8765，管理端口默认为 8766。保存后管理服务开始侦听，可以在工作站点击「启动 Host」，也可以由客户端连接时启动。访问令牌和本地控制凭据由系统凭据库保存。常规配置及日志位于 macOS 的 `~/Library/Application Support/RemoteQGIS Host` 或 Windows 的 `%LOCALAPPDATA%\RemoteQGIS Host`。项目配置、管理日志和工作站日志均可从窗口打开目录核查。

客户端在 RemoteQGIS 的 Client 标签页填写工作站地址、GIS 端口和访问令牌，点击「保存当前地址」。连接方式选择 Host Manager 并核对管理端口。后续选择该工作站并点击「连接工作站」，插件会查询服务状态，并在 GIS 服务就绪后连接。此前部署的 Desktop Host 可选「直接连接原有 Host」，原来的手动连接按钮也可继续使用。

## 自启与远端展示

开启「登录系统时启动后台 Host」后，macOS 由当前用户的 LaunchAgent 启动，Windows 由任务计划程序的登录触发器启动。更改地址、项目或凭据前，先退出管理服务。取消自启并保存可移除登录项。Windows 若启用防火墙且需要跨设备连接，请对可信 VPN 网络开放设置中的两个 TCP 端口。管理和 GIS 服务复用现有 HTTP/WebSocket 认证机制，跨网络连接沿用加密 VPN，或在工作站前置 TLS 反向代理。

打开「允许连接用户开启只读 QGIS 展示」后，管理程序可在可用的桌面会话中启动独立的 QGIS Desktop。展示窗口跟随远端代理图层、图层样式、矢量成果和地图视野。展示进程只持有读取令牌，请求修改图层、项目、文件和执行计算时由 Host 拒绝。候选版 Desktop Host 与后台 Host 对同一个共享目录使用协作写入锁。切换至原有 Desktop Host 写入模式时，先保存后台项目并停止后台 Host，再用 QGIS Desktop 打开该项目。

## Windows 实机验收

安装 QGIS 3.x LTR 后，使用临时共享目录与临时项目。启动管理程序，核对检测到的 QGIS 版本。保存配置、启动和停止 Host，并确认端口、状态和日志相符。从 macOS 客户端按保存的工作站连接，导入 TIFF 和 GeoPackage，验证原始像元、矢量保存、Processing 输出及项目再次打开。允许只读展示后，在已登录的 Windows 桌面验证 QGIS 画布能显示同一组图层和跟随视野。设置自启后重启并登录，再验证管理服务和项目恢复。完成后取消自启并停止服务。

本候选版采用 macOS ZIP 应用包和 Windows x64 便携 ZIP。应用尚未做开发者签名或公证。后台自启发生在配置账户登录后，系统启动但尚未登录时的 Windows 服务和 macOS LaunchDaemon 部署仍待验证。共享目录锁由新版 Host 协作遵守，同一项目应始终交给一个写入端。稳定版 1.0.0-rc1 及任意外部 GIS 程序仍可直接打开其有权限访问的数据。
