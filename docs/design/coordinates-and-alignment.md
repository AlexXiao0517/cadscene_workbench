# 坐标系与 SfM-CAD 对齐

本页解释系统如何把视频重建结果放到 CAD 参考中。它描述的是当前实现的坐标转换、约束和拒绝条件，不把普通 SRT 或实验性输出宣传为测量真值。

## 坐标框架

| 名称 | 来源与用途 | 关键边界 |
| --- | --- | --- |
| SfM world | SfM 从视频估计的相机中心、旋转和稀疏点。 | 原点、方向和尺度由重建决定，本身不等于 CAD 或地理坐标。 |
| CAD meters | 对齐模块使用的本地 CAD 米制坐标。 | 由人工关键帧的 Web 坐标通过 `origin_xy` 和 `cad_scale` 转换得到。 |
| Web `cad_world` | 查看器保存/读取的相机坐标。 | 用于界面和下载/导入兼容；它不是自动地理参考坐标。 |
| local ENU | partial-SRT CLI 将经纬度变换得到的东、北、天局部坐标。 | East/North 基于 WGS84 本地 ENU；Up 是单独记录的相对高度，不宣称为 CAD 绝对高程。 |
| CGCS2000 projected / CAD raw | full-pose 将 WGS84 SRT 位置按用户确认的 EPSG/高斯—克吕格参数投影，并按 CAD 轴序映射。 | 候选绑定当前 CAD 指纹；中央经线、3°/6°分带、带号和 X/Y 轴序都不能跨项目猜测。 |
| CAD local metres | full-pose 通过 `origin_xy` 与 `cad_scale` 把 CAD raw/projected 坐标变为内部局部米制轨迹。 | 轨迹标记 `metric_scale_locked=true`，不得进入自由尺度 Sim3。 |

Web 到 CAD meters 的位置换算为：

```text
cad_x_m = (web_x - origin_x) × cad_scale
cad_y_m = (web_y - origin_y) × cad_scale
cad_z_m = web_z × cad_scale
```

反向导出时使用相反换算。为兼容查看器，前端 `pitch` 与后端 Python `pitch` 符号相反；`yaw`、`roll` 和 `FOV` 原样传递。不要手工混用两种 pitch 约定。

## 人工关键帧与 Sim3

人工或已确认关键帧提供同一视频帧的 Web 相机位置、姿态和 FOV。对齐时，系统排除 `algorithm_prediction` 项，把每个保留关键帧与 SfM 轨迹同帧的相机中心和旋转配对。路线拟合至少需要两个这类锚点；增加分散且经过现场/CAD 复核的锚点通常比重复标记相邻画面更有价值。

全局相似变换 Sim3 将 SfM world 映射到 CAD meters：

```text
p_cad = scale × R × p_sfm + t
```

它同时处理旋转 `R`、平移 `t` 和尺度 `scale`。实现使用关键帧的相机姿态约束旋转，而不是把位置点当成唯一信息来源；随后用关键帧间距离估计尺度。正因如此，两个位置不同的锚点有专用的 two-anchor 分支：两条基线确定尺度和方向，关键帧姿态再约束绕基线的转角。两个端点任一基线退化时，该分支会拒绝结果。

全局 Sim3 是基线，不是对每一帧的强制替换。系统计算每个锚点相对全局结果的位置和角度残差，并按帧号在相邻锚点间插值，形成分段锚定路径。查看器的点云/全局轨迹只使用 global Sim3，而相机路径可使用分段残差，因此两者不完全重合是预期现象。

`srt_full_pose` 不使用上述自由 Sim3。投影后的轨迹已经位于 CAD local metres，因而
对齐固定单位旋转和 `scale=1.0`；无人工修正时直接发布，存在人工锚点时只稳健估计一个
固定 XYZ 平移以及独立的 wrapped yaw/pitch/roll 零偏。彼此矛盾的修正会被拒绝。该路线
允许 viewer scene 没有 sparse PLY，且不运行依赖点云的道路表面诊断。

## FOV 优先级

FOV（视场角）影响相机内参解释，但不是 SfM 一定准确的保证。当前优先级是：

1. 所有已确认关键帧都有有限、合法且彼此相差不超过 0.1° 的手动 FOV 时，使用它们的平均值；这会覆盖不可靠的 SfM 重建 FOV。
2. 否则，若对齐配置明确指定 FOV，使用配置值。
3. 否则使用 SfM 轨迹的水平 FOV；轨迹没有可用值时才回退配置默认值。

手动 FOV 缺失、越界或彼此不一致时不会作为可信覆盖值。系统会检查异常内参；若焦距比例等内参异常且没有可信手动/配置 FOV，会拒绝对齐并要求先确认 FOV。完成后仍应在关键帧、CAD 和现场资料之间人工复核，而不是把单个 FOV 数值视为精度证明。

## 纯旋转与可观测性

若人工锚点位置没有可分辨的平移基线，对齐进入 `rotation_only`：相机位置保持人工锚点位置的分段插值，姿态仍结合 SfM 旋转和人工姿态约束。此时相对 CAD 的尺度不可观测，`scale = 1.0` 只是保持 Sim3 输出协议的回退值，不能解释为真实比例。

`pure_rotation` 是正式支持的固定相机中心工作流，不恢复平移或尺度。项目视频分析会根据短视频中持续、无矛盾的旋转证据自动推荐该路线，但当前分类精度仍不成熟，推荐必须结合现场确认，项目页也允许人工覆盖。它依赖固定版本外部 OpenGV 后端；其可观测性决定了结果不适用于推断沿路线行进距离。

## partial-SRT 的 ENU 边界

独立的 partial-SRT core（Experimental CLI）可把 SfM 中心与 SRT 的局部 ENU 做稳健 Sim3，采用 PTS 优先的时间同步和 RANSAC/Umeyama 拟合。至少需要三个空间上独立的约束；质量门控、约束不足、RANSAC 内点不足或无效尺度都会拒绝融合并要求回退 `sfm_only`。

该 partial-SRT 核心尚未接入正式 JobRunner，`srt_sfm_fused` 仍为 Interface only。
`srt_full_pose` 是另一条已接通的 metric-direct 路线，只在完整云台姿态、精确 frame map、
当前 CAD 投影和水平 FOV 均明确时执行；它不使用 ENU/自由 Sim3，也不使未确认的普通
SRT 成为精确位置或 CAD 高程真值。

## 验证与拒绝

对齐输出会保存锚点、Sim3、残差、FOV 来源、警告和指标。常规（尺度可观测）结果至少通过以下检查：

- 少于两个可用关键帧、SfM 或 CAD 锚点中心退化时拒绝；
- 全局锚点最大残差超过 5 m 时拒绝；
- two-anchor 情形中基线方向误差超过 0.1° 时拒绝；
- 异常 SfM 内参没有可信的手动或配置 FOV 时拒绝。

在 `rotation_only` 中，平移尺度本来不可观测，不能用“通过”替代现场验证。应检查 `alignment.json` 的 `alignment_mode`、`scale_observable`、残差、FOV 来源和警告，结合关键帧画面与 CAD 复核后再使用结果。
