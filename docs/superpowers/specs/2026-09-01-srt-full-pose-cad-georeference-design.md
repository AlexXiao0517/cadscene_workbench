# SRT 全姿态轨迹与 CGCS2000 CAD 对齐设计

## 目标

实现现有 `srt_full_pose` 预留工作流：输入视频、DJI 风格 SRT、CAD 图纸和用户填写的水平视场角，直接生成逐帧相机轨迹，不运行 SfM，也不通过三维重建反算位置。

该路线的核心问题是将 SRT 的地理位置和云台姿态转换到 CAD/Viewer 使用的坐标与相机约定。CAD 已知为 CGCS2000 平面坐标时，系统先自动推荐投影参数，再由用户确认；不得把当前项目使用过的中央经线 `120°` 硬编码为所有项目的默认真值。

首版保留现有人工关键帧、质量检查和渲染能力，但人工步骤改为校验或小范围修正，不能重新估计会破坏米制尺度的任意 Sim3。

## 已有架构与复用边界

现有代码已经具备以下可复用能力：

- `cadscene/srt/parser.py` 增量解析 DJI 风格经纬度、高度、机身与云台姿态；
- `cadscene/srt/capability.py` 根据同一时刻的 GPS、高度和云台姿态完整度识别 `srt_full_pose`；
- `cadscene/srt/synchronization.py` 以权威 source PTS/frame map 同步视频帧和 SRT，并限制插值间隙；
- `cadscene/srt/coordinates.py` 已有 WGS84/ECEF/ENU 基础换算；
- `cadscene/projects/workflow_adapters.py` 已注册 `srt_full_pose` 版本 1，但明确标记为 interface-only；
- 轨迹 loader、人工关键帧、Viewer、质量评估与渲染已经共享兼容的 trajectory schema；
- CAD importer 已输出原始 `origin_xy` 和 `cad_scale`，Viewer 坐标遵循 `(cad_xy - origin_xy) * cad_scale`；
- Viewer 与对齐模块现有 `fov` 均表示水平视场角。

首版不修改 partial-SRT 的 `srt_sfm_fused` 路线。只有 capability 判定为 full pose、坐标配置已确认且水平 FOV 有效的片段才能运行新 adapter。

## 用户流程

1. 用户上传 CAD、视频和 SRT，分析服务继续识别片段与 SRT 能力。
2. 对 `srt_full_pose` 片段，项目页显示“CAD 坐标系确认”与“水平 FOV”设置。
3. 系统根据 SRT 经度、CAD 原始范围和轴序生成 CGCS2000 高斯—克吕格候选，并显示最佳候选及轨迹落图预览。
4. 用户确认候选，或手动修改中央经线/分带与轴序。当前项目预计推荐“CGCS2000 / 3° GK / CM 120E（EPSG:4549）”，但推荐必须来自本项目输入。
5. 用户填写一个水平 FOV 数值。首版不增加水平/垂直/对角类型选择，字段文案直接明确为“水平视场角（°）”。
6. 预检通过后直接生成 CAD 米制坐标中的逐帧相机轨迹，并进入现有工作台预览。
7. 用户检查轨迹位置、方向、高度和画面覆盖；必要时只做受约束的平移、高程偏移或姿态零偏修正。
8. 保存后继续质量检查和渲染。该路线不依赖 sparse point cloud，所有下游入口不得再硬编码 `02_sfm` 产物。

## CAD 地理参考契约

在项目 source assets 的用户配置层保存版本化 `cad_georeference`，而不是写入分析器拥有的 clip analysis。建议契约如下：

```json
{
  "schema_version": 1,
  "horizontal_datum": "CGCS2000",
  "projection_family": "gauss_kruger",
  "zone_width_deg": 3,
  "central_meridian_deg": 120.0,
  "epsg": 4549,
  "projected_axis_order": "easting_northing",
  "cad_axis_mapping": "cad_x_easting_cad_y_northing",
  "zone_prefix": false,
  "linear_unit": "metre",
  "source": "auto_confirmed",
  "confirmed": true,
  "confidence": 0.96,
  "validation": {
    "trajectory_inside_cad_ratio": 0.91,
    "median_distance_to_cad_bbox_m": 0.0
  }
}
```

字段必须明确区分投影 CRS 的规范轴序与 DXF 实际 X/Y 映射。所有地理转换使用显式 `always_xy` 语义得到 easting/northing，再按 `cad_axis_mapping` 写入 CAD 原始 X/Y，不能依赖 CRS 库默认轴序。

配置属于整个 CAD 版本。同一项目替换 CAD 后配置必须重新验证；只调整 FOV 不应使 CAD 地理参考失效。配置变更应使依赖它的 full-pose trajectory 变为 stale，但不影响原始视频、SRT 或分析 revision。

## CRS 候选与确认

候选器只负责缩小选择范围，不允许静默确认：

- 以 SRT 有效样本经度中位数生成附近的 3°分带中央经线，并补充当前位置对应的 6°分带候选；
- 同时尝试无带号与坐标值表现为带号的 CGCS2000 候选；
- 根据 CAD bbox 的数量级判断米制坐标、可能的带号以及 X/Y 互换；
- 将抽样 SRT 位置投影到候选 CRS，再按候选轴映射与 CAD bbox 比较；
- 评分使用有限、可解释的证据：有效变换比例、轨迹落入 CAD bbox 或合理缓冲区的比例、距离 bbox 的统计量、单位和带号一致性；
- 候选得分接近、轨迹完全落在图外或转换异常时，预检必须阻止执行并要求人工选择。

轨迹走向与 CAD 线条不做自动图形匹配，以免道路密集或 CAD 内容不完整时产生虚假高置信度。确认界面叠加显示 CAD bbox、抽样轨迹、起终点、中央经线、EPSG、轴映射和评分理由。

若 CAD 名义上是 CGCS2000 但存在固定地方偏移，允许增加一个已知点求 XY 平移；若实际是地方独立坐标，则回退为至少两个非重合控制点求固定尺度 SE(2)，第三个点用于残差检查。首版不自动估计自由尺度 Sim3。

### 坐标库与离线交付

使用 `pyproj`/PROJ 执行 CRS 解析和投影，不在业务代码中手写高斯—克吕格公式。开发环境已具备 `pyproj`，但项目依赖尚未声明，因此实现必须把它加入 `pyproject.toml`，并让 Windows 离线打包验证覆盖 `pyproj`、`proj.db` 与所需 PROJ 数据目录。应用启动或 full-pose 预检发现坐标库/数据缺失时，应返回明确的环境错误，不能退化为近似公式或无提示的经纬度平移。

## SRT 位置、姿态与高度转换

### 平面位置

SRT 经纬度以 WGS84 输入，通过受控 CRS 转换到已确认的 CGCS2000 投影平面。转换后先保留审计用的原始 easting/northing，再根据 `cad_axis_mapping` 得到 CAD X/Y，最后复用现有 CAD 归一化：

```text
viewer_x = (cad_x - origin_x) * cad_scale
viewer_y = (cad_y - origin_y) * cad_scale
```

轨迹文件的权威计算坐标仍使用 CAD 米制坐标；Viewer 导出阶段再做显示归一化。任何人工校正默认只允许固定 XY 平移，米制 scale 锁定为 1。

### 高度

CGCS2000 平面投影不能解决高程基准。SRT 相对高度、SRT 绝对高度、CAD Z 可能分别代表起飞点相对高度、椭球/设备海拔和 1985 国家高程或局部零点。

首版高度策略为：

- 优先采用稳定的 SRT 相对高度保持轨迹的垂直变化；
- 由用户在预览中确认一个 `cad_z_offset_m`，将相对高度放到 CAD 高程；
- 若只存在绝对高度，必须将 `height_source` 和“绝对高程未经基准验证”写入 warning；
- 没有可确认的高度来源时不宣传绝对三维对齐，也不静默假设 CAD Z=海拔。

### 姿态

DJI 云台姿态先按显式 profile 解释为地理 NED 参考，再转换到项目相机约定：相机 X 向右、Y 向下、Z 向前，yaw 0 指北且顺时针为正。profile 必须记录使用了绝对云台 yaw 还是机身相对 yaw；字段缺失、参考系不明确或数值跳变时预检失败。

实现必须用合成基准验证正北、正东、俯视和带 roll 四种姿态，并在真实预览中显示相机视锥。不能只根据字段名假定所有 DJI SRT 固件具有同一姿态参考。

## 水平 FOV 与内参

每个 full-pose 片段要求一个用户填写的 `horizontal_fov_deg`。同镜头的多个片段可以在项目页批量填写相同值，但最终值保存在片段用户配置中并进入 job fingerprint，以支持不同镜头或不同变焦状态。

首版约束：

- 只接收单个水平 FOV 数值，不增加 FOV 类型；
- 推荐 UI 范围为 10°～170°，后端硬校验为有限且 `1° < FOV < 179°`；
- 根据实际视频宽高计算 `f = width / (2 * tan(horizontal_fov / 2))`，输出 `PINHOLE` 内参 `fx=fy=f, cx=width/2, cy=height/2`；
- trajectory 同时保存原始 `horizontal_fov_deg`、`fov_source=user` 和派生内参；
- 用户值优先于 SRT 元数据、默认 70°或任何不存在的 SfM 内参；
- FOV 在预览中可调整，保存后生成新的配置 revision 并使旧轨迹 stale。

FOV 不能表达镜头畸变。首版只支持普通透视或机内已校正视频；明显鱼眼、强畸变或录制中连续变焦只提示不支持，不在首版引入畸变模型。

## Adapter、产物与下游解耦

启用 `srt_full_pose` adapter 新版本，命令只包含 full-pose trajectory builder，不包含 `cadscene.cli.run_sfm`。输入至少包括：视频、SRT、权威 source PTS 区间和 frame map、视频尺寸、确认后的 `cad_georeference`、CAD `origin_xy/cad_scale`、水平 FOV 和高度/姿态 profile。

建议新增：

- `cadscene/srt/georeference.py`：配置校验、CGCS2000 候选生成、评分与投影；
- `cadscene/srt/full_pose.py`：同步样本转完整相机 pose、内参和诊断；
- `cadscene/cli/build_srt_full_pose.py`：原子写出轨迹与报告；
- `configs/pipelines/srt_full_pose_overlay.yaml`：不要求 sparse PLY 的下游 pipeline。

不可变 attempt 产物：

- `02_srt_full_pose/camera_trajectory_full_pose.json`；
- `02_srt_full_pose/camera_path_full_pose.csv`；
- `02_srt_full_pose/georeference_diagnostics.json`；
- `02_srt_full_pose/full_pose_report.md`。

trajectory 保持现有 loader 所需的 `fps/width/height/intrinsics/poses`，顶层 `meta` 追加 workflow、SRT 同步、CRS、轴映射、CAD 归一化、高度、姿态 profile、FOV 来源和 warning。每个 pose 保留 `frame_index/registered/center/cam_from_world_quat_wxyz`，并追加 source PTS、原始经纬度/高度、投影坐标、插值标记和置信度。

ProjectService、工作台和 job runner 应通过 adapter 输出键解析 `trajectory` 与可选 `sparse_points`，不得再把所有非 pure-rotation 路线映射成 `sfm_only`，也不得硬编码 `02_sfm/camera_trajectory.json`。Viewer scene 已能输出空点云；full-pose pipeline 应正式允许 point cloud 缺失，并跳过依赖 sparse geometry 的 road-surface 分析。

## API、状态与并发

新增用户配置更新 API 必须带 clips/project revision，使用现有乐观并发语义。snapshot 对 full-pose 片段返回：

- 当前 georeference 配置、候选列表、确认状态与预览摘要；
- `horizontal_fov_deg` 与来源；
- `cad_z_offset_m`、height source、attitude profile；
- `can_run_trajectory` 和具体阻塞原因。

预检阻塞条件至少包括：SRT full-pose 完整度不足、精确 frame map 缺失、CRS 未确认、轨迹不落图、FOV 无效、视频尺寸不可得、姿态参考不明确。任务运行时输入发生变化则候选 attempt 标记 stale，不发布为 active output。

## 失败处理与安全边界

- 当前项目确认 120°不影响其他项目；每个 CAD 版本独立候选和确认。
- 禁止从 Omap/国内底图上未标注坐标类型的显示经纬度直接假定为 WGS84。
- CRS 候选存在歧义时禁止自动执行。
- 米制轨迹禁止进入自由 scale Sim3。
- SRT 插值仅使用现有 bounded-gap 规则；长缺口 pose 标记未注册，不跨缺口外推。
- GPS 跳点、不可达速度、姿态瞬跳和高度突变进入诊断并降低置信度；超过阈值则拒绝发布。
- 用户修正只保存显式参数及其来源，不能覆盖原始 SRT 或 CAD。

## TDD 与验收

测试至少覆盖：

- 经度接近 120°时推荐 EPSG:4549，但其他经度推荐对应中央经线而非沿用 120°；
- 3°/6°分带、带号/无带号、CAD X/Y 互换和异常坐标范围；
- 候选接近或轨迹落图失败时必须人工确认；
- 已知 WGS84 点投影到 CGCS2000 后的数值与可信基准一致；
- `origin_xy/cad_scale` 显示归一化不改变权威 CAD 米制坐标；
- 水平 FOV 到 PINHOLE 焦距的横竖屏计算和非法值拒绝；
- 权威 PTS 同步、bounded interpolation 和 VFR frame map；
- 北/东/俯视/roll 合成姿态转换；
- 相对高度加 `cad_z_offset_m`，绝对高度未验证时产生 warning；
- full-pose adapter 命令中不存在 SfM，输出兼容现有 trajectory loader；
- 项目工作台不再把 `srt_full_pose` 映射成 `sfm_only`；
- 下游在没有 sparse PLY 时仍可打开轨迹、保存校正、质量检查和渲染；
- wheel 元数据声明 `pyproj`，离线包内可以脱离开发 checkout 加载 PROJ 数据并完成 EPSG:4549 smoke；
- 配置 revision 变化使旧 trajectory stale，旧分析和源文件保持不变；
- 聚焦单元/集成测试、全量测试及一个隔离项目副本 smoke 通过。

## 首版明确不做

- 不支持鱼眼或通用畸变标定；
- 不支持逐帧变化的 FOV/连续变焦；
- 不把 CGCS2000 平面坐标自动宣传为已解决绝对高程；
- 不自动进行 CAD 线条与视频图像的视觉匹配；
- 不让 full-pose 路线回退运行 SfM；
- 不为地方独立坐标自动估计无约束三维 Sim3。

## 参考依据

- EPSG:4549：CGCS2000 / 3-degree Gauss-Kruger CM 120E，中央经线 120°，适用经度约 118.5°E～121.5°E：<https://epsg.io/4549>
- OmapCAD 将已知 CGCS2000 投影参数与未知参数时的 CAD/经纬度关联点作为两类配置方式：<https://www.ovital.com/134467-2/>
- DJI 云台姿态采用地理 NED 约定，绝对 yaw 以真北为零、顺时针为正：<https://developer.dji.com/doc/payload-sdk-tutorial/en/function-overview/advanced-function/gimbal-management.html>
