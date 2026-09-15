# 虚拟相机 CAD 叠加查看器

浏览器端虚拟相机调试工具（legacy script 版本）。正式项目操作见[当前 Viewer/工作台说明](../../docs/web_viewer_usage.md)。

> 默认运行的是 legacy 版本：`index.html` + `paths.js` + `fallback.js` + `viewer_legacy.js` + `vendor/`。
> 早期 ES module 版本（`viewer.js / projection.js / cad_scene.js`）当前仓库未包含，不作为默认入口。

## 资源路径（不再写死）

路径统一由 `paths.js` 的 `resolveViewerPaths()` 解析，优先级：显式 URL 参数 > dataset 默认 > hygs 默认。

默认（`dataset=hygs`）：

```text
video  = ../data/hygs/hygs.mp4
cad    = ../data/hygs/design.json
track  = ../data/hygs/camera_track.json
camera = ../configs/camera_params.json
```

支持选择数据集：

```text
http://127.0.0.1:8300/web_camera_viewer/?dataset=hygs
```

也支持显式传参覆盖：

```text
http://127.0.0.1:8300/web_camera_viewer/?video=../data/hygs/hygs.mp4&cad=../data/hygs/design.json&track=../data/hygs/camera_track.json
```

SfM / QA 闭环还支持：

```text
suggestions      = ../out/.../keyframe_suggestions.json
qualityTimeline  = ../out/.../quality_timeline.csv（可选；未传时从 suggestions 同目录自动推导）
sfmScene         = ../out/.../sfm_viewer_scene.json
```

240f 当前示例：

```text
http://127.0.0.1:8300/web_camera_viewer/?dataset=hygs_1min&track=../out/hygs1min_sfm_align_240f_kf_faded_900m/camera_track_pred.json&suggestions=../out/hygs1min_sfm_quality_qa_240f_kf/keyframe_suggestions.json&sfmScene=../out/hygs1min_sfm_viewer_scene_v0/sfm_viewer_scene.json
```

## 打开方式

先在项目内导出 hygs viewer 资源（详见 `docs/hygs_web_viewer.md`）：

```bash
python -m cadvideo.pipeline.export_web_viewer_assets dji/hygs \
  --cad-dir out/hygs \
  --video dji/hygs/hygs.mp4 \
  --output-dir data/hygs \
  --track out/hygs_camera_track_visible_20s/camera_poses.json \
  --keyframe-step 120
```

然后**从项目根目录**启动支持 HTTP Range 的静态服务器：

```bash
python -m cadvideo.pipeline.serve_web_viewer --port 8300 --bind 127.0.0.1
```

> 不建议使用 `python -m http.server`：普通服务器对大 MP4 的 Range 支持不足，容易导致播放/跳帧失败。

在浏览器打开：

```text
http://127.0.0.1:8300/web_camera_viewer/?dataset=hygs
```

> 不要用 `file://` 直接双击 HTML 打开：浏览器会因 fetch/CORS 限制无法加载 `design.json`、视频和轨迹。
> 也务必从项目根目录（包含 `data/` 和 `web_camera_viewer/` 的目录）启动服务器，否则 `../data/...` 解析不到。

## 使用方式

左侧是主视频视图，视频上方的透明 canvas 会绘制虚拟相机投影后的 CAD 线。右侧是第三人称 Three.js 视图，可用鼠标旋转、平移、缩放，观察 CAD 地面、虚拟 UAV 相机和视锥关系。

底部控制面板会实时调整同一组虚拟相机参数：

- `x`, `y`: 相机在 CAD 世界坐标里的水平位置
- `z`: 相机高度
- `yaw`: 水平朝向
- `pitch`: 俯仰角，负数表示向下看
- `roll`: 画面滚转
- `fov`: 水平视场角

`z/yaw/pitch/roll/fov` 支持锁定。锁定后滑条禁用，右侧 TransformControls 拖动也会保留该自由度，便于只在水平 `x/y` 平面移动相机。

加载 SfM 场景后，右侧 3D 视图还会显示：

- SfM sparse point cloud；
- 原始 SfM 轨迹；
- 当前加载 track 对应的锚定后轨迹；
- 当前 `keyframe_suggestions.json` 的建议补帧标记。

## 导入和导出

点击 `Export JSON` 会下载当前相机参数，格式如下：

```json
{
  "camera": {
    "x": 501988.4482916761,
    "y": 3206398.97238183,
    "z": 120,
    "yaw": 0,
    "pitch": -45,
    "roll": 0,
    "fov": 70
  }
}
```

点击 `Import JSON` 可以导入同样结构的相机参数。`Reset` 会恢复为 CAD bbox 中心上方的初始相机。

## 关键帧轨迹

正式项目请使用[当前 Viewer/工作台说明](../../docs/web_viewer_usage.md)。项目工作台通过 API 保存兼容草稿与已发布 revision；不要再按旧的浏览器下载文件流程手工复制活动项目轨迹。下文独立静态 Viewer 的查询参数仅用于兼容 dataset/run 调试。

页面会把 `algorithm_prediction` 视为预测帧，不作为人工关键帧；上一/下一关键帧只在人工确认帧之间跳转。

## 当前限制

- 没有点选配准，也没有自动视频-CAD 匹配（后续阶段：每隔若干秒/帧或匹配失败时人工重锚）。
- 没有读取 SRT/GPS/IMU 元数据。
- 没有相机畸变模型，默认针孔相机。
- 没有遮挡、地形高程或 3D 重建，所有 CAD 都位于 `z=0` 地面平面。
- Three.js 已保存到 `web_camera_viewer/vendor/`，不再依赖 CDN。
