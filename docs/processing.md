# 远程 Processing 功能清单

客户端连接工作站后，在 QGIS 的「处理工具箱」中展开 **RemoteQGIS 工作站**。列表取下表与双端已启用 Processing 算法的交集。选择工作站共享目录中已导入的远端代理作为输入，输出填写相对共享目录的路径，例如 `成果/缓冲区.gpkg` 或 `成果/裁剪.tif`。

算法由工作站执行。矢量结果写为 GeoPackage，栅格结果写为 GeoTIFF。统计算法返回数值摘要。输出目录须已存在，输出名称须可用。工作站写入成功后将结果加入主项目，并在客户端同步显示。进度、日志和取消请求通过 Processing 对话框传递。

## 已接入的 42 项算法

| 类别 | 功能 | QGIS 算法标识 |
| --- | --- | --- |
| 矢量几何 | 缓冲区、质心、面内点、凸包 | `native:buffer`、`native:centroids`、`native:pointonsurface`、`native:convexhull` |
| 矢量几何 | 融合、修复几何、多部件转单部件、简化、按间距加密、边界、面转线 | `native:dissolve`、`native:fixgeometries`、`native:multiparttosingleparts`、`native:simplifygeometries`、`native:densifygeometriesgivenaninterval`、`native:boundary`、`native:polygonstolines` |
| 叠加与提取 | 裁剪、相交、差集、联合、对称差、按范围提取 | `native:clip`、`native:intersection`、`native:difference`、`native:union`、`native:symmetricaldifference`、`native:extractbyextent` |
| 矢量转换 | 重投影、合并图层、删除字段 | `native:reprojectlayer`、`native:mergevectorlayers`、`native:deletecolumn` |
| 属性与空间统计 | 字段计算、按字段连接、空间连接、统计面内点 | `native:fieldcalculator`、`native:joinattributestable`、`native:joinattributesbylocation`、`native:countpointsinpolygon` |
| 栅格计算 | 原生栅格计算器、线性重缩放、查表重分类、逐像元统计、常数填补 NoData | `native:rastercalc`、`native:rescaleraster`、`native:reclassifybytable`、`native:cellstatistics`、`native:fillnodata` |
| 栅格与矢量统计 | 波段统计、分区统计、栅格采样 | `native:rasterlayerstatistics`、`native:zonalstatisticsfb`、`native:rastersampling` |
| 栅格转换 | 按范围裁剪、按矢量掩模裁剪、重投影及重采样、参数转换另存 | `gdal:cliprasterbyextent`、`gdal:cliprasterbymasklayer`、`gdal:warpreproject`、`gdal:translate` |
| 地形分析 | 坡度、坡向、山体阴影、粗糙度 | `gdal:slope`、`gdal:aspect`、`gdal:hillshade`、`gdal:roughness` |
| 栅矢转换 | 栅格转面、矢量栅格化 | `gdal:polygonize`、`gdal:rasterize` |

在 QGIS 3.40.4 上对 **缓冲区、栅格计算器、按范围裁剪** 完成了从客户端 Processing 注册表调用、工作站执行、客户端同步图层和输出文件回读的双端验证。其余算法通过共同可用性筛选，受各自输入类型及工作站 Provider 版本约束。

## 暂未接入的算法与参数

- 上表之外的 QGIS、GRASS、SAGA、OTB、第三方插件、自定义 Python 算法及外部命令。
- 工作流模型器、跨任务模型链、原生批量任务、跨工作站重启恢复，以及后台任务历史持久化。
- 直接使用本地数据、在线底图、客户端选中要素、表达式筛选后的子集或临时图层作为工作站输入。
- 任意 GDAL 附加命令行参数和驱动选项，动态数据定义参数，复杂字段表达式函数及额外副输出。
- 直接将计算结果输出到 Shapefile、COG、VRT、网络数据库或本机目录。当前输出 GeoPackage、GeoTIFF，以及栅格统计数值摘要。
