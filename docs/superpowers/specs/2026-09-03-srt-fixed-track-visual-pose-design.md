# SRT 固定轨迹与视觉姿态工作流设计

## 决策摘要

对含有经纬度和高度、但没有可用云台姿态的 DJI SRT，现有 `srt_sfm_fused` 不再执行“完整 SfM 后与 SRT 融合”。它改为：

1. 将 SRT 经纬度按用户确认的 CGCS2000 投影转换到 CAD；
2. 将每帧相机中心严格锁定为 SRT→CAD 的位置；
3. 使用用户填写的水平视场角建立相机内参；
4. 视觉算法只估计相机旋转，不允许优化、替换或平滑相机中心；
5. 工作台只允许对整条路线施加同一个 XYZ 平移，并允许人工微调姿态；
6. 即使自动姿态求解不完整，也先发布并显示位置轨迹，不回退到 SfM；
7. 该路线不进入三维重建工作台，不生成正式点云，也不经过质量检测环节。

兼容现有项目数据和路由时，内部 workflow key 暂时保留为 `srt_sfm_fused`，但用户界面统一显示“`SRT 轨迹 + 视觉姿态`”。产物 metadata 必须明确声明 `position_source=srt_cad_locked` 和 `orientation_source=visual_fixed_center`，避免内部旧名称造成语义误解。

## 背景与现状问题

当前 partial-SRT adapter 的实现顺序是完整运行 SfM，再用 SRT 与 SfM 轨迹估计 Sim3 并融合位置。因此它具有三个问题：

- 重建时间与普通 SfM 基本相同，没有利用已知轨迹缩小求解空间；
- SfM 会先自由估计相机中心，随后再融合，违反“位置只能来自 SRT→CAD”的约束；
- 工作台启动条件只检查 workflow、视频片段和 CAD。轨迹尚未生成时，启动逻辑会把阶段恢复为 `sfm`，所以用户仍会进入三维重建界面。

当前真实测试 SRT 共解析到 12017 条记录，每条都有相对高度和绝对高度：`rel_alt` 范围约为 84.926～91.846 米，`abs_alt` 范围约为 229.472～236.391 米；没有可用的完整云台姿态。因此该数据能够直接提供三维路径，但不能单独提供可渲染的相机方向。

## 范围与非目标

本阶段实现 partial-SRT 的固定位置、视觉姿态估计、位置优先发布和专用工作台流程。

本阶段明确不做：

- 不通过完整 SfM、pose-prior mapper 或 bundle adjustment 重新估计相机位置；
- 不以任何形式把视觉位置覆盖到 SRT 位置；
- 不保存用于重建的正式 sparse/dense point cloud；
- 不进行自由尺度、旋转或逐帧位移的 CAD 对齐；
- 不增加独立的质量检测任务或质量检测页面；
- 不把 SRT 绝对高度直接解释成 CAD 高程基准；
- 不在求解失败时静默回退到 `sfm_only`；
- 不在首版支持鱼眼、强畸变、逐帧变焦或滚动快门联合标定。

完整姿态 SRT 继续走 `srt_full_pose`：位置和姿态都来自 SRT，不运行视觉姿态求解。无 SRT 的片段继续走原有 SfM 路线。

## 工作流路由

路由以 clip capability 和用户配置为准：

| 输入能力 | 工作流 | 位置来源 | 姿态来源 |
| --- | --- | --- | --- |
| SRT 含 GPS、高度和可解释的完整云台姿态 | `srt_full_pose` | SRT→CAD | SRT |
| SRT 含 GPS、相对高度，但无完整云台姿态 | `srt_sfm_fused`（UI：SRT 轨迹 + 视觉姿态） | SRT→CAD，硬锁定 | 视觉固定中心求解 |
| 无可用 SRT 轨迹 | `sfm_only` | SfM | SfM |

若用户手动选择与 capability 不兼容的工作流，预检必须给出明确阻塞原因，不允许执行后再自动换路。partial-SRT 不能伪装成 `srt_full_pose`，也不能因视觉姿态失败转成 `sfm_only`。

## 坐标与高度契约

### XY

运行前必须已有用户确认的 CAD georeference。经纬度使用该配置投影到 CGCS2000 平面坐标，并按已确认的 CAD 轴映射、`origin_xy` 和 `cad_scale` 转为内部局部米制坐标：

```text
cad_local_x_m = (cad_raw_x - origin_x) * cad_scale
cad_local_y_m = (cad_raw_y - origin_y) * cad_scale
```

中央经线支持十进制度和度分输入，例如 `118°50′ = 118.833333...°`。候选预览必须验证抽样轨迹落入 CAD 范围；只有用户确认的候选可以成为 trajectory job 输入。

### Z

本阶段采用已确认的唯一规则：

```text
canonical_z_m(frame) = srt_rel_alt_m(frame)
display_z_m(frame) = canonical_z_m(frame) + route_offset_z_m
```

- `rel_alt` 是主高度来源，用于保持路线的真实相对起伏；
- `abs_alt` 仅写入诊断产物，帮助人工判断数据，不直接作为 CAD Z；
- SRT 缺少 `rel_alt` 时，本阶段阻止三维固定轨迹任务，并明确提示缺少相对高度；不允许自动或手动切换为 `abs_alt`；
- CAD 与 SRT 的竖向基准差只通过全路线统一的 `route_offset_z_m` 校正；
- 不允许逐帧修改 Z，不允许视觉算法优化 Z。

当前测试 SRT 的 `rel_alt` 与 `abs_alt` 都存在，因此必须选用 `rel_alt`。

### 路线统一偏移

原始规范轨迹 `canonical_center_xyz` 永不改写。工作台保存一个版本化的：

```json
{"route_offset_xyz_m": [0.0, 0.0, 0.0]}
```

最终显示和渲染位置为 `canonical_center_xyz + route_offset_xyz_m`。该偏移对所有帧完全一致；前端和后端都不得提供单帧 position keyframe、拖拽相机中心或非刚性的路线修形。

## 视频、SRT 与帧同步

继续复用唯一的 SRT parser、权威 source PTS/frame map 和 bounded interpolation：

- 视频帧使用 source PTS 与 SRT cue 时间关联；
- 只在小于等于 `max_interpolation_gap_sec` 的合法相邻 SRT 记录之间插值；
- 覆盖范围外或长时间空洞不外推；
- 每个 pose 保留 source PTS、前后 SRT entry、插值状态和原始经纬度/高度；
- 没有有效位置的帧标记为 position unavailable，不能由视觉轨迹补齐。

视觉求解抽帧和最终导出帧都必须引用同一份 frame map，禁止分别按 `frame_index / fps` 建立两套时间轴。

## 水平 FOV 与相机内参

用户为每个片段输入一个水平视场角，普通 DJI 镜头默认按水平 FOV 解释，不再增加水平/垂直/对角类型选择。后端校验有限数且 `1° < FOV < 179°`，并按真实视频宽高生成固定 PINHOLE 内参：

```text
fx = fy = width / (2 * tan(horizontal_fov_deg / 2))
cx = width / 2
cy = height / 2
```

FOV、视频尺寸和派生内参进入 job fingerprint。修改 FOV 会使旧姿态产物 stale，但不会修改规范位置轨迹。用户未填写有效 FOV 时可以预览 CAD 和 SRT 路线，但不能启动视觉姿态求解。

## 固定中心视觉姿态求解

### 硬约束

设第 `i` 帧的固定相机中心为 `C_i`，待求变量只有世界到相机旋转 `R_i`：

```text
camera pose variables = {R_i}
camera center C_i = SRT→CAD(frame i) + route_offset_xyz
```

求解器的数据结构和优化参数块中不得注册 `C_i`。任何临时三维点只用于姿态约束，不作为项目点云发布；清理临时点也不能影响最终轨迹。

COLMAP 的 pose prior 属于软位置约束，完整 SfM 后替换中心也仍然自由估计过位置，因此两种方式都不满足本设计。

### 求解阶段

1. 按运动距离和时间均匀抽取关键帧，保留首尾帧，并跳过无有效位置的区间。
2. 使用固定内参提取局部特征，匹配相邻帧和有限的短距离回环帧。
3. 利用匹配、已知基线 `C_j-C_i` 和固定内参估计相邻旋转约束；用 RANSAC 排除动态物体和误匹配。
4. 构建旋转图，只优化各帧旋转，并用鲁棒损失联合最小化多帧几何残差。
5. 仅在有足够视差时允许创建临时 landmark 作为重投影约束；landmark 可优化，但所有相机中心仍固定。
6. 将可靠关键帧旋转按时间做四元数 SLERP 插值到有效位置帧；不得跨超过配置上限的未约束区间自动插值。
7. 输出每帧旋转状态、支持匹配数、残差和失败原因，不把低置信解标成已完成。

### 可观测性与人工锚点

长直线、低纹理、重复纹理、近纯旋转、夜间或强动态画面可能无法仅凭固定中心轨迹稳定确定全部姿态，尤其可能存在绝对朝向或局部 roll/pitch 歧义。系统必须诚实地保留 `orientation_unavailable`，不能猜测姿态或移动路线来吸收误差。

工作台允许用户创建姿态关键帧，只编辑 yaw/pitch/roll；关键帧间仍使用四元数插值。人工姿态锚点可作为视觉旋转图的硬/高权重旋转约束重新求解，但不引入位置变量。

## 位置优先的产物状态

adapter 只要 SRT→CAD 位置校验成功，就应成功发布轨迹产物；自动姿态覆盖不足属于 warning，不应使整个 job 失败。顶层状态为：

- `position_only`：有效位置已经发布，没有可用旋转；
- `orientation_partial`：部分帧具有可靠旋转；
- `orientation_ready`：满足渲染所需的旋转覆盖和连续性。

工作台对三种状态都可打开，并始终绘制位置折线。只有存在有效旋转的帧才绘制视锥。位置轨迹不得依赖姿态 job 成功后才 materialize。

渲染就绪采用技术完整性门禁，而不是独立质量检测流程。首版默认要求：

- 目标渲染区间中有效 orientation 覆盖率不低于 80%；
- 未求解且未人工锚定的连续缺口不超过 `max_orientation_interpolation_gap_sec`；
- 每个参与渲染的 pose 四元数有限、归一化且旋转方向约定有效。

不满足时前端说明缺失区间并引导添加姿态关键帧；仍允许查看和保存位置轨迹。

## 产物与数据契约

workflow adapter 版本必须提升，输出目录改为具有真实语义的新目录，例如：

- `02_srt_visual_pose/camera_trajectory_visual_pose.json`；
- `02_srt_visual_pose/camera_path_srt_locked.csv`；
- `02_srt_visual_pose/orientation_diagnostics.json`；
- `02_srt_visual_pose/visual_pose_report.md`。

trajectory 保持现有 loader 的 `fps/width/height/intrinsics/poses` 主结构，并扩展：

```json
{
  "meta": {
    "workflow": "srt_sfm_fused",
    "workflow_label": "SRT 轨迹 + 视觉姿态",
    "trajectory_status": "orientation_partial",
    "metric_scale_locked": true,
    "position_source": "srt_cad_locked",
    "orientation_source": "visual_fixed_center",
    "height_source": "srt_rel_alt",
    "absolute_height_usage": "diagnostic_only",
    "route_offset_xyz_m": [0.0, 0.0, 0.0]
  },
  "poses": [
    {
      "frame_index": 0,
      "registered": false,
      "position_available": true,
      "orientation_available": false,
      "canonical_center": [0.0, 0.0, 84.929],
      "center": [0.0, 0.0, 84.929]
    }
  ]
}
```

`registered` 继续表示该帧是否具备完整可投影 pose；位置可用但姿态不可用时必须是 `false`。loader 和 Viewer 需要接受没有 `cam_from_world_quat_wxyz` 的 position-only pose，不应丢弃其 `center`。

报告记录：同步覆盖率、位置有效帧数、姿态关键帧数、姿态覆盖率、不可插值缺口、特征/匹配耗时、旋转求解耗时、总耗时和全部 warning。正式输出不包含 sparse PLY。

## 项目服务与任务生命周期

### 预检

视觉姿态任务的必要条件为：

- clip capability 至少包含有效 GPS 和相对高度；
- CAD georeference 已确认，轨迹落图验证通过；
- 视频尺寸、SRT、权威 frame map 可用；
- 水平 FOV 有效；
- route offset 数值有限。

完整姿态字段不是 partial-SRT 的必要条件。

### 打开工作台

“进入工作台”不再以“存在视频和 CAD”作为充分条件：

1. 未确认 CAD georeference 或未填写 FOV：打开配置弹窗，并准确显示阻塞项；
2. 配置完整但没有当前版本 trajectory：创建 trajectory job，显示“解析 SRT → 投影到 CAD → 提取视觉约束 → 估计姿态 → 准备工作台”的进度；
3. position artifact 发布后自动进入专用工作台，即使姿态只有 partial 或 unavailable；
4. 已有匹配 fingerprint 的 position artifact：直接恢复专用工作台；
5. 任何条件下都不把该 workflow 的恢复阶段设为 `sfm`。

job 输入必须包含源文件 hash、clip revision、frame-map revision、CAD revision/georeference、FOV、route offset、solver 配置和人工姿态锚点 revision。运行中输入发生变化时，旧 attempt 标记 stale，不成为 active output。

## 工作台体验

该路线的阶段栏只显示：

1. `SRT 轨迹`：位置解析、投影、轨迹统计；
2. `视觉姿态`：自动旋转覆盖、缺口和人工姿态关键帧；
3. `微调与渲染`：全路线 XYZ 偏移、姿态微调、渲染设置。

隐藏 SfM 重建、稀疏点云、三维重建质量检测和自由 CAD alignment 控件。三维视图始终显示 CAD 与 SRT 路线，按状态区分：位置有效但姿态缺失、自动姿态、人工姿态、插值姿态。前端文案不得继续显示“SRT + 三维重建”。

全路线 XYZ 偏移的修改应即时更新预览，但保存时创建配置 revision，并按现有并发规则检查 project/clip revision。FOV 或 georeference 变化需要重新求解姿态；只修改全路线平移时不需要重新提取特征或重新估计相对姿态。

## 错误与降级语义

- SRT 无 GPS/相对高度、CAD 投影未确认或落图失败：预检阻塞，不创建空工作台；
- FOV 无效：允许看位置预览，阻止视觉姿态任务；
- 特征不足或旋转图退化：发布 `position_only` 或 `orientation_partial`，展示诊断；
- 单段 SRT 空洞：保留其他位置段，空洞不由视觉位置填补；
- 自动姿态异常：仅撤销该姿态 attempt，不影响已经发布的规范位置；
- 临时视觉几何失败：不得触发完整 SfM；
- 绝对高度与相对高度不一致：记录 warning，仍遵循 `rel_alt + route_offset_z_m`；
- CAD georeference 或源文件变化：旧 trajectory stale，禁止渲染旧结果。

## 性能验证

速度收益必须通过同一片段、同一抽帧策略和同一硬件的实测给出，不预先承诺倍数。报告分别记录：解码、特征提取、匹配、固定中心旋转求解、插值和产物写出耗时，并与当前完整 SfM + fusion adapter 的 wall time 和峰值内存比较。

预期优势来自不执行增量位置注册、自由相机位姿 BA、正式三角化和点云输出；若实测没有明显收益，仍不能以放松固定位置约束换速度或成功率。

## TDD 验收

实现时测试至少覆盖：

1. 路由：full-pose、partial-SRT 和 no-SRT 分别进入正确 workflow；partial-SRT 页面不出现“SRT + 三维重建”；
2. 工作台启动：缺配置打开配置页，配置完整创建 trajectory job，position artifact 出现后进入专用工作台，绝不恢复到 `sfm`；
3. 高度：真实/合成 `rel_alt` 全部进入 Z，`abs_alt` 只进诊断；统一 Z 偏移对所有帧相同；
4. 坐标：确认后的 `118°50′` 自定义中央经线能让测试轨迹落入 CAD，并正确经过 axis mapping、origin 和 scale；
5. 硬约束：合成序列运行前后所有相机中心逐帧完全等于 SRT→CAD 中心加统一偏移；求解器中不存在相机中心参数块；
6. 姿态：已知中心和 FOV 的合成特征序列可恢复旋转，旋转误差满足阈值；异常点不会移动相机中心；
7. 退化：直线低纹理、纯旋转、重复纹理和长空洞输出明确 warning/partial 状态，不回退 SfM；
8. schema：position-only pose 可被 loader、ProjectService 和 Viewer 接收，路线可见而视锥隐藏；
9. 人工微调：只能编辑统一 XYZ 偏移和姿态关键帧，API 拒绝逐帧 position edit；
10. 渲染门禁：姿态覆盖不足时仍可保存/查看路线，但不可渲染；人工锚点补足后可进入渲染；
11. 产物：adapter 命令不包含 `run_sfm`、`fuse_srt_sfm` 或正式 sparse point cloud 输出；
12. stale/concurrency：FOV、georeference、源文件、frame map 和姿态锚点变化正确使旧输出失效；纯统一平移不重跑视觉求解；
13. 回归：`srt_full_pose`、`sfm_only`、项目库、任务恢复和现有渲染测试通过；
14. 真实 smoke：当前项目的 12017 条 SRT 记录生成落在 CAD 内的三维路线，Z 保持约 84.926～91.846 米的相对变化，未求得姿态时路线仍可在工作台查看。

## 验收完成定义

用户从项目库新建项目并上传 CAD、视频、SRT 后，可以填写水平 FOV、确认 CAD 投影、看到完整生成进度并进入专用工作台。工作台首先可靠显示 SRT→CAD 三维路线；自动视觉姿态能求多少就明确显示多少，其余允许用姿态关键帧补齐。全过程中每帧位置只可能等于规范 SRT→CAD 位置加同一个 XYZ 偏移，且不会出现 SfM 工作流、自由位置估计或质量检测环节。
