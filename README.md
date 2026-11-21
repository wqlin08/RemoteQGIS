# RemoteQGIS

RemoteQGIS 是面向 QGIS Desktop 3.x 的远程 GIS 工作空间插件。工作站保存真实数据和 QGIS 主项目。客户端在本机 QGIS 浏览共享目录、显示影像、管理项目并编辑矢量代理。V0.5 的安装包同时包含 Host 与 Client 模式。

## 安装与连接

1. 在两台设备的 QGIS 3.40 或更新的 3.x 版本中，进入「插件 → 管理并安装插件 → 从 ZIP 安装」，选择同一份 `RemoteQGIS-0.5.1.zip`。
2. 在工作站打开「插件 → RemoteQGIS → Host · 工作站」，选择共享目录。跨网络使用时，将监听地址改为工作站的 Tailscale IP，端口默认是 `8765`。启动服务后，将访问令牌提供给客户端。
3. 在客户端选择 **Client · 客户端**，填写工作站的 IP 或主机名、端口和令牌。认证成功后切换到 **远程工作区**。
4. 两个本机 QGIS 会话可用 `127.0.0.1` 试用。通过 Tailscale IP 直连时保持 HTTPS / WSS 关闭，由 Tailscale 隧道提供传输加密。使用配置了有效证书的 TLS 反向代理时，启用 HTTPS / WSS。

默认监听回环地址。工作站界面可停止监听。令牌仅保留在本次 QGIS 会话中。

## 远程工作区

在共享目录内通过双击、多选导入或将文件拖到地图画布来打开 GeoTIFF、GeoPackage 和 Shapefile。选择图层后，「影像范围」缩放到原始范围，「波段与拉伸」设置 RGB 或灰度渲染，「原始像元」查询真实栅格数值。栅格代理通过 QGIS 原生 XYZ 图层按视野加载浏览瓦片。

在客户端 QGIS 图层树的 **RemoteQGIS** 分组内，可以用原生界面排列、分组、隐藏、重命名、调整透明度并配置基本矢量符号。通过插件中的「远端项目」菜单打开、创建、保存或另存远端 .qgz / .qgs。删除共享文件需确认，随后文件移入工作站共享目录下的 `.remoteqgis-trash`。

已有矢量选中后点击「启用编辑」，确认在数据中增加由插件管理的 `rq_uuid` 字段。新建矢量可指定保存目录、文件类型、几何类型、坐标系和字段。启用后可在本机使用 QGIS 原生数字化、捕捉、节点和属性工具。绘制面后右键结束，只有内部标识的图层会直接完成绘制。使用「保存图层编辑」或「保存或重试编辑」提交到工作站。工作站确认前，客户端在当前 QGIS 用户配置的 `remoteqgis/pending` 目录保留记录。连接恢复后可自动提交。

## 构建与测试

```sh
python3 -m unittest discover -s tests/unit -v
python3 scripts/build_plugin.py
python3 scripts/test_desktop.py --stage 5 --qgis /Applications/QGIS-LTR.app/Contents/MacOS/QGIS --zip dist/RemoteQGIS-0.5.1.zip
```

构建脚本从 PyPI 下载并校验固定版本的 Tornado 源码，将其纯 Python 文件及许可证打入 ZIP。插件使用 QGIS 自带的 Python、Qt 和 GDAL 环境。

[技术选型与架构](docs/architecture.md) · [V0.5 实现与限制](docs/workspace.md) · [验收记录](docs/verification.md) · [变更记录](CHANGELOG.md)

RemoteQGIS 源码以 GPL-3.0-or-later 发布。ZIP 中的 Tornado 源码保留 Apache-2.0 许可。
