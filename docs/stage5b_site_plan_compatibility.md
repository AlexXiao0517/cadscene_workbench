# Stage 5B：无道路中心线场景与多分辨率视频

## CAD 与道路中心线

CAD importer 读取普通二维 CAD 实体，并不要求 CAD 是道路图纸。`LINE`、`LWPOLYLINE`、`POLYLINE`、`SPLINE`、`ARC`、`CIRCLE` 等实体仍按原图层和颜色写入 `design.json`，可以用于总平面、建筑轮廓、地块边界和其他二维线条的显示与渲染。

道路中心线按以下顺序检测：

1. `road_center.json` 中至少存在一条有效折线，来源记为 `asset`；
2. `design.json` 中图层 `kind` 为 `center`，或图层名包含 `center`、`centre`、`middle`、`zhong`，来源记为 `layer`；
3. 其余情况记为 `none`。

`alignment`、`quality`、`viewer_scene` 和 `render_overlay` 不依赖道路中心线。只有 `road_surface` 需要中心线来构造道路走廊。pipeline 检测不到中心线时会把 `road_surface` 记为 `skipped`，并继续完成其余阶段。

## 无 SRT 输入

当前主流程不读取也不要求 SRT。SfM 根据视频图像恢复相机轨迹，人工关键帧与 SfM 轨迹共同确定 CAD 对齐的位置、旋转和尺度。系统不会在没有 SRT 时生成虚假的 GPS 或高程信息。

## 多分辨率显示

左侧视频区和右侧 Three.js 区仍保持固定左右布局。视频使用 `object-fit: contain`，`VideoDisplayTransform` 根据实际视频像素尺寸和固定容器尺寸统一计算缩放、可见区域和黑边偏移。

- CAD 投影仍使用实际视频像素宽高；
- overlay canvas 只覆盖视频的可见矩形，不覆盖黑边；
- `sourceToDisplay` 与 `displayToSource` 是统一坐标入口；
- 黑边内的显示坐标转换返回 `null`，不参与标定；
- `loadedmetadata` 和 `ResizeObserver` 都会重新计算变换；
- 后端渲染继续保持原视频分辨率，不使用 CSS 尺寸。

## 当前能力边界

本阶段支持二维道路设计 CAD、二维建筑总平面轮廓、地块边界和其他平面线条在航拍视频中的人工标定与投影预览。

本阶段不支持三维 BIM/IFC、建筑立面与材质、自动地形贴合、自动点云与建筑模型配准、semantic refine。
