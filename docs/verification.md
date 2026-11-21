# RemoteQGIS 验证记录

V0.5.0 于 2026-11-20 验证，V0.5.1 于 2026-11-21 回归验证。本机环境为 macOS 15.7.3，QGIS Desktop 3.40.4 LTR，内置 Python 3.9.5、Qt 5.15.2、PyQt 5.15.4 和 GDAL 3.3.2。

| 范围 | 验证结果 |
| --- | --- |
| 单元测试 | 覆盖消息结构、认证令牌、共享路径、Shapefile 副文件、编辑差量和本地恢复文件。 |
| V0.1 | 在两个独立 QGIS Desktop 用户配置中从 ZIP 安装，验证 HTTP、WebSocket、认证失败、请求超时、心跳、重启重连和端口释放。 |
| V0.2 | 在共享目录导入 GeoTIFF、GeoPackage 和 Shapefile，覆盖多选导入、原生拖放数据格式和自定义画布处理入口，核对工作站真实数据路径与本地矢量内容。 |
| V0.3 | 使用带概览的 4096 × 4096 三波段合成 GeoTIFF，验证原生 XYZ 提供器读取渲染瓦片、波段和拉伸切换产生新瓦片版本，以及独立原始像元数值。 |
| V0.4 | 验证图层分组、名称、透明度、符号、视野旋转，以及项目保存、关闭和重新打开后状态保持。 |
| V0.5 | 验证既有两种矢量格式、新建 GeoPackage 面图层、绘制与编辑几何、属性和字段、撤销重做、失败的 GeoPackage 事务回滚、本地断线记录、重连提交、重复事务识别，并使用 OGR 回读工作站真实文件。 |
| V0.5.1 | 在 QGIS 地图画布上验证无业务字段的面图层可用右键结束绘制，保存后本地保持图层 ID 并渲染新面，工作站 GeoPackage 保留几何。拖入当前视野外的影像后，客户端定位到影像并在画布上显示。 |
| Windows 与广域网 | 待使用真实 Windows QGIS 工作站与 macOS 客户端验收，包括文件锁、长路径、Tailscale 直连或中继、实际大型影像平移与缩放。 |

## 复现

```sh
python3 -m unittest discover -s tests/unit -v
python3 scripts/build_plugin.py
python3 scripts/test_desktop.py --stage 5 --qgis /Applications/QGIS-LTR.app/Contents/MacOS/QGIS --zip dist/RemoteQGIS-0.5.1.zip
```

集成测试使用临时 QGIS 用户配置、测试令牌和自动生成的数据。两个 QGIS Desktop 实例通过应用自身的 ZIP 插件安装器启动插件。测试使用 macOS 默认窗口后端。
