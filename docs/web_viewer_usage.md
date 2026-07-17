# Web Camera Viewer 使用说明

## 启动

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

请通过 HTTP 服务访问 viewer，不要直接用 `file://` 打开。视频拖动依赖 HTTP Range 请求。

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

`dataset + runId` 主要负责推导 `runs/` 输出；视频和 CAD 仍需要在 `/data/` 下存在，或通过显式 URL 参数传入。

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

## 常见问题

- 视频无法拖动：请使用 `serve_viewer`，不要用 `file://`。
- 点云颜色异常：确认 `sfm_viewer_scene.json` 中 RGB 是 0..255 整数。
- 建议帧不显示：检查 `suggestions` 参数或 `04_quality/keyframe_suggestions.json` 是否存在。
- 质量色带不显示：检查 `qualityTimeline` 参数或 `04_quality/quality_timeline.csv` 是否存在。
- anchored path 与点云不重合：点云/global track 只用 global sim3，anchored path 是分段锚定结果。
