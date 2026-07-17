# Stage 4D CAD 自动导入

## 旧项目复刻结论

新实现以旧项目的以下代码为行为基线：

- `project/scripts/cad_to_json.py`：复刻 LINE、LWPOLYLINE、POLYLINE、ARC、CIRCLE、bulge 采样、ACI/BYLAYER 颜色，以及 `world_points` 与画布归一化 `points` 的双坐标输出。
- `project/cadvideo/cad/geom.py`：参考统一实体字段、SPLINE 离散化、entity handle、closed 和 bbox 约定。
- `project/cadvideo/pipeline/export_web_viewer_assets.py`：保留 `design.json.meta.coordinate_mode=cad_world`、bbox、layers 和 viewer 路径约定。
- 旧 `hygs_prepare_realign.py` 与项目文档明确将 DWG 视为“只记录、不解析”，实际处理依赖外部转换。因此新项目没有虚构内置 DWG parser。

所有逻辑已经整理到 `cadscene/`，运行时不读取或导入旧目录。

## 普通上传流程

普通模式只显示“上传视频”和“上传 CAD”。视频上传后，浏览器根据视频文件名和当前时间自动生成 dataset 与 runId。DXF 上传后自动：

1. 保存到 `data/<dataset>/raw_cad/`；
2. 解析受支持的线状实体；
3. 生成 `design.json`；
4. 生成 `cad_import_stats.json` 和中文 `cad_import_report.md`；
5. 更新 `dataset_manifest.json`；
6. 视频与 CAD 都 ready 后重新打开 viewer 并进入 SfM 阶段。

`debug=1` 时可展开高级设置，手动指定 dataset、CAD scale、origin XY，或继续导入兼容的 `design.json`/CAD assets ZIP。

## 坐标和单位

- `origin_xy` 使用 CAD bbox 左下角 `[min_x, min_y]`。
- DXF `$INSUNITS` 可识别时，`cad_scale` 为“每个 CAD 单位对应多少米”。
- `$INSUNITS` 缺失或未知时，沿用旧主线 `cad_scale=0.06`，并在 manifest/report 中要求人工确认。
- `design.json.world_points` 始终保留 CAD 原始世界坐标。

## DWG

默认环境没有内置 DWG 解码器。DWG 会安全保存到 `raw_cad/`，manifest 标记 `dwg_raw_saved`，前端提示上传 DXF 或安装转换器。

可通过环境变量 `CADSCENE_DWG_CONVERTER` 配置无 shell 的转换命令模板，例如：

```text
CADSCENE_DWG_CONVERTER="C:\tools\converter.exe" "{input}" "{output}"
```

转换器必须生成指定 DXF；成功后复用同一个 DXF importer。转换失败会写入 manifest/job status，不会标记 CAD ready。
