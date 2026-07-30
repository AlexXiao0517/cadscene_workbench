# Web Camera Viewer 使用说明

## 启动

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

请通过 HTTP 服务访问 viewer，不要直接用 `file://` 打开。视频拖动依赖 HTTP Range 请求。

`--root` 提供静态页面；`--storage-root` 提供项目的 `data/` 和 `runs/`，默认与
`--root` 相同。若部署时两者分开，存储目录必须预先存在：

```powershell
python -m cadscene.cli.serve_viewer `
  --root . `
  --storage-root D:\cadscene-storage `
  --bind 127.0.0.1 --port 8300
```

分离后服务会把存储根映射为 `/data/` 与 `/runs/`，供 Viewer 和工作流 API 使用。
`--extra-root` 只用于只读旧数据兼容，不能作为 workflow 写入位置。

推荐把 demo 数据整理到新项目的 `data/` 目录下，例如：

```text
data/<dataset>/
  video/<dataset>.mp4
  cad/design.json
  tracks/camera_track.json
```

## 使用 dataset + runId

```text
http://127.0.0.1:8300/apps/web_camera_viewer/?dataset=<dataset>&runId=<run_id>
```

viewer 会按 `runs/<dataset>/<run_id>/` 推导：

- `03_alignment/camera_track_pred.json`
- `04_quality/camera_track_pred_quality.json`
- `04_quality/quality_timeline.csv`
- `04_quality/keyframe_suggestions.json`
- `05_viewer_scene/sfm_viewer_scene.json`
- `06_road_surface/viewer_diagnostics_scene.json`

`dataset + runId` 会推导 `runs/` 输出。显式 URL 参数始终优先级最高。参数缺失且该
数据集有可读的 manifest 时，工作流脚本会用 manifest 中的视频、CAD、`cadScale` 和
`originXY` 补齐地址栏；这让门户创建的数据集可直接进入 Viewer。只有 manifest 不可读
时，Viewer 才尝试默认 `/data/<dataset>/...` 路径。若 manifest 已给出媒体 URL 但资源
已移动或失效，Viewer 不会自动退回默认路径；应修正 manifest 或传入显式 URL。

## 显式传入文件

URL 参数优先级最高，可显式传入：

- `video`
- `cad`
- `track`
- `suggestions`
- `qualityTimeline`
- `sfmScene`
- `diagnosticsScene`

显式参数优先级最高，支持 `/runs/...`、`/data/...` 这类 Web path。不要把 Windows 绝对路径直接交给浏览器。

## 临时挂载旧数据

如果暂时要使用旧目录中的数据，可以显式挂载额外根目录：

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300 --extra-root legacy=D:\zjic2026\project
```

然后在 URL 中显式传：

```text
video=/legacy/data/...
cad=/legacy/out/...
```

这是临时兼容方式，不是默认主线；viewer 不会默认生成 `/legacy` 或旧 `out/` 路径。

## 数据含义

- quality timeline 只用于显示 low / medium / high 风险色带。
- suggestions 只用于提示和跳转，不会自动添加关键帧，也不会修改相机。
- SfM 点云只读展示，RGB 按 0..255 整数解释。
- diagnostics scene 只读展示 road points、profile、keyframe residuals 和 warnings。
- 点云和 global track 只使用 global sim3；anchored path 来自分段锚定结果，二者不完全重合是正常现象。
- 已确认且一致的人工关键帧 FOV 优先于不可靠的上游 SfM 重建 FOV；Viewer 的提示会说明
  该人工覆盖，不能据此推断 SRT 或 SfM 的绝对精度。
- `pure_rotation` 是 Experimental 工作流：它固定相机中心，只恢复旋转，随后由人工全局
  放置和局部姿态校正进入渲染；不恢复平移或尺度。

## 常见问题

- 视频无法拖动：请使用 `serve_viewer`，不要用 `file://`。
- 点云颜色异常：确认 `sfm_viewer_scene.json` 中 RGB 是 0..255 整数。
- 建议帧不显示：检查 `suggestions` 参数或 `04_quality/keyframe_suggestions.json` 是否存在。
- 质量色带不显示：检查 `qualityTimeline` 参数或 `04_quality/quality_timeline.csv` 是否存在。
- anchored path 与点云不重合：点云/global track 只用 global sim3，anchored path 是分段锚定结果。
- 门户进入 Viewer 后视频或 CAD 丢失：确认 `dataset` 指向的 manifest 仍存在于同一
  `--storage-root`。若 manifest URL 指向已移动资源，修正 manifest 或传入显式 Web
  路径；旧数据也可用只读 `--extra-root` 挂载。不要期待此情形自动回退到默认路径。
- 道路诊断不显示：检查 `06_road_surface/viewer_diagnostics_scene.json`；若 run manifest
  将 `road_surface` 标为 `skipped`，通常是 CAD 没有可用道路中心线，并非 Viewer 故障。
