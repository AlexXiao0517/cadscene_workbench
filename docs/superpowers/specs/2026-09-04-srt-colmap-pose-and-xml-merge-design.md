# SRT 固定轨迹 COLMAP 姿态与 Bentley XML 合并设计

## 决策摘要

本设计同时完成两个相互校验、但生产职责分离的目标：

1. 将给定 Bentley BlocksExchange XML 中每 120 帧一组的相机姿态插值到原始 DJI SRT 的全部 12017 条记录，生成一个只增加相机姿态、完全保留 SRT 时间、位置、高度和曝光字段的新 SRT，用于验证现有 `srt_full_pose` 路线；
2. 用稀疏 COLMAP 替换当前 `srt_fixed_track_visual_pose` 中无法稳定恢复绝对姿态的相邻帧 ORB/Essential Matrix 实现。COLMAP 负责恢复视觉相机位姿和稀疏点，随后用同帧 SRT 中心估计视觉坐标到 CAD 坐标的鲁棒 Sim3；最终逐帧位置仍强制回写为 SRT→CAD 中心，视觉算法只提供旋转。

这份设计取代 `2026-09-03-srt-fixed-track-visual-pose-design.md` 中“直接固定中心解旋转”的求解实现，也取代 `2026-09-03-srt-whole-video-pose-anchor-design.md` 中“默认依赖用户单锚点建立绝对姿态”的交互。两份旧文档中的整视频单场景、SRT 位置硬锁定、统一 XYZ 偏移和跳过独立质量检测等约束继续有效。

## 已验证的输入事实

### 原始 SRT

- 文件包含 12017 条字幕记录，记录索引为 0～12016，对应 DJI 帧号 1～12017；
- 时间覆盖 `00:00:00,000`～`00:03:20,491`，约 200.491 秒；
- 经纬度、相对高度和绝对高度存在，但没有可用相机/云台姿态；
- 视频约为 60 fps，因此 120 帧约等于 2 秒。

### Bentley XML

- `BlocksExchange version=3.2`，空间参考为 WGS84 / EPSG:4326；
- 图像尺寸为 3840×2160，`CameraOrientation=XRightYDown`；
- `FocalLengthPixels=3386.07800321286`，换算得到水平 FOV `59.108875...°`；
- 包含 101 个相机，图像名严格为 `frame_000000.jpg`、`frame_000120.jpg`，直至 `frame_012000.jpg`；
- 因此该结果确实按每 120 视频帧抽取一张图像；
- 每个相机同时包含 `Pose/Rotation`、优化后的 `Pose/Center` 和原始 GPS `Pose/Metadata/Center`。

### 位置一致性审计

将 XML 的帧号 `N` 映射到 SRT 的零基记录 `N` 后，101 个 XML `Metadata/Center` 与对应 SRT 的经度、纬度和绝对高度逐点完全一致，没有检测到帧偏移。

XML 优化后的 `Pose/Center` 与同帧 SRT GPS 并不相同：水平差中位数约 3.520 米、最大约 11.874 米，高度差约为 -9.389～+3.188 米。因此 XML 优化中心不能写回 SRT；它只可作为外部重建诊断，合并时唯一允许新增的是 XML 相机姿态。

## FOV 精度策略

水平 FOV 不需要用户精确输入到三位小数。三位小数来自 XML 焦距反算，可用于复现实验，但不代表无人机厂家或镜头标定本身具备这一精度。

本测试采用：

- 内部审计值：`59.108875...°`；
- 文件与测试配置值：`59.109°`；
- 面向用户的推荐显示值：`59.1°`，输入控件允许最多三位小数但不强迫补零。

`59.1°` 与 `59.109°` 对视锥和初始内参的影响可以忽略。需要避免的是使用明显不同的默认值（例如 72°）。FOV 继续作为片段设置保存，而不重复写进每一条 SRT；合并报告记录本次测试 FOV。

## XML 姿态合并产物

### 输出与不可变性

原文件保持不动，生成：

`DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.SRT`

每个原始字幕块必须逐字保留序号、时间码和已有元数据。仅在元数据文本末尾追加：

```text
[camera_yaw: ...] [camera_pitch: ...] [camera_roll: ...]
```

现有 parser 将 `camera_*` 明确解释为相机/云台姿态，因此输出会被 capability 路由为 `srt_full_pose`，同时又不会把 Bentley 的相机姿态冒充为 DJI 机身姿态。

### 插值

- XML 帧号直接对应 SRT 零基记录索引；
- 101 个 XML 姿态样本先按项目相机约定转换为单位四元数；
- 相邻样本之间使用最短弧四元数 SLERP，禁止直接分别线性插值 yaw/pitch/roll；
- 插值后再转换为现有 full-pose parser 接受的 `camera_yaw/pitch/roll`；
- 最后 XML 样本为帧 12000，剩余 12001～12016 共 16 帧、约 0.267 秒，保持最后一个 XML 姿态；
- 姿态角输出至少保留 6 位小数，避免文本往返造成不必要误差。

合并工具还输出同名 JSON 报告，记录源文件 hash、样本数、映射规则、FOV、插值方式、末尾保持帧数及姿态连续性统计。

### 验证门禁

生成文件后必须验证：

1. 仍有 12017 个字幕块，序号和时间码与原 SRT 完全一致；
2. 原有 GPS、高度和曝光字段逐记录完全一致；
3. 相机 yaw/pitch/roll 有效覆盖率为 100%；
4. capability 结果为 `srt_full_pose`；
5. XML 锚点帧处的合并姿态与 XML 等价；
6. 插值四元数有限、归一化且不存在非预期翻转；
7. 输出文件不包含 XML 的优化中心。

## 当前完整 SRT 工作流

现有 `srt_full_pose` 已是独立 adapter，命令只运行 `cadscene.cli.build_srt_full_pose`，不运行 COLMAP/SfM，也不生成点云。它的实际用户流程为：

1. 项目分析检测到 GPS、高度和相机姿态覆盖均不低于 80%，推荐 `SRT 全姿态（跳过三维重建）`；
2. 用户确认当前 CAD 地理参考，并填写水平 FOV；
3. 点击“进入工作台”后可以先打开工作台会话；若当前输入版本尚无轨迹产物，工作台处于 `workflow_start`，需要启动一个很快的 full-pose 轨迹生成任务；若已有已验证的同 fingerprint 产物，则以 `trajectory_ready` 直接显示轨迹和视锥；
4. full-pose builder 使用权威 frame map 将 SRT 同步到视频帧，将 WGS84 位置转换到已确认 CAD 坐标，并直接采用 SRT 相机姿态；
5. 进入姿态/路线检查与微调，再进入独立渲染阶段。

因此“可以进入工作台查看”是成立的，但第一次进入并不等于轨迹在进入前已经生成。现有 UI 把 `workflow_start` 通用地显示成“开始 SfM 重建”属于错误文案和阶段复用问题，本轮实现必须将 full-pose 的首次任务显示为“生成 SRT 全姿态轨迹”，不能让用户误以为它运行了 SfM。

## 不完整 SRT 的新生产路线

### 为什么替换当前实现

当前 `srt_fixed_track_visual_pose` 的算法参数为：

- 每 `0.5` 秒抽一帧，本视频约等于每 30 帧；
- ORB 最多 2000 个特征；
- 仅匹配相邻抽样帧，Hamming KNN ratio test 为 `0.75`；
- 每对最少 24 个通过 ratio test 的匹配；
- Essential Matrix 使用 RANSAC，`prob=0.999`、像素阈值 `1.0`；
- `recoverPose` 至少要求 15 个内点；
- 姿态插值最大间隙 `2.0` 秒。

这不是三维重建，也不是 COLMAP。它只积累两帧相对旋转，并尝试用单一 SRT 基线解释 `recoverPose` 的平移方向。长直线航线、近似固定俯角和重复道路纹理会使绝对世界朝向不可观，因此真实样本虽有大量相对姿态，仍可能得到 0 个可发布的绝对姿态。

### 新数据流

```text
视频 + 用户 FOV
  -> 稀疏抽帧、特征与顺序匹配
  -> COLMAP 稀疏相机位姿 + 内部稀疏点

SRT + 已确认 CAD georeference
  -> 全帧 SRT→CAD 规范中心（不可变）

COLMAP/SRT 同帧中心
  -> RANSAC 鲁棒 Sim3（视觉世界 -> CAD 世界）
  -> 变换 COLMAP 相机旋转和稀疏点
  -> 逐帧中心强制替换为 SRT→CAD 规范中心
  -> 四元数插值到全视频
  -> 工作台姿态检查与微调
  -> 独立渲染
```

COLMAP 的相机中心只在估计坐标系变换时作为视觉测量使用，不能成为最终位置。最终任意帧都必须满足：

```text
final_center(frame) = srt_to_cad_center(frame) + uniform_route_offset_xyz
```

不允许逐帧位置微调，也不允许用平滑后的 COLMAP 路径替代 SRT。

### 为什么允许内部点云

稀疏点云用于三角化、bundle adjustment、姿态稳定和用户诊断，允许在工作台显示。它不是位置真值，也不参与覆盖 SRT 中心。省略 dense reconstruction、网格化、纹理化和独立质量检测，仍会显著少于完整三维重建；但只省略 PLY 文件导出本身不会带来明显加速。

### 抽帧与 COLMAP 默认参数

外部 XML 的每 120 帧只是该外部项目的一次成功设置，不能作为所有视频的硬编码参数。生产路线按时间设置，以适应不同帧率：

- 首次抽帧间隔：`1.0` 秒，并强制包含首尾有效 SRT 帧；本视频约 201 张图；
- 自动补密重试：当 COLMAP 注册图像覆盖低于 80%、同帧对少于 12 个或 Sim3 失败时，改为 `0.5` 秒；本视频约 401 张图；
- 抽帧上限：800 张；超过时在保持首尾和均匀时间覆盖的前提下重新采样；
- 图像最大边：2048 像素；
- 每图最大特征：12000；
- 相机模型：首版固定 `PINHOLE`，焦距由用户水平 FOV 计算并固定，不让 COLMAP 自由漂移 FOV；
- 匹配器：sequential matcher，overlap `15`，启用 quadratic overlap；
- mapper：`init_min_tri_angle=2.0°`、`filter_min_tri_angle=1.0°`、`triangulation_min_angle=1.0°`、`min_num_matches=15`、`init_min_num_inliers=50`、`min_reg_images=10`；
- BA 延续当前轻量设置：global max 25 iterations、最多 2 次 refinement，frames/points ratio 均为 2.0；
- 后端优先复用当前 pycolmap/colmap_cli 选择与 CPU fallback；设备设置进入任务 fingerprint。

固定内参必须在实现前用当前 pycolmap 和 CLI 两个后端各做兼容 smoke。若后端 API 不能可靠锁定内参，宁可显式失败并说明版本能力，也不允许静默优化用户 FOV。

### Sim3 与姿态转换

- 只使用同时存在 COLMAP 注册中心和有效 SRT→CAD 中心的帧；
- 至少需要 3 个非共线同帧中心才能尝试 Sim3，生产门禁默认要求至少 12 个；
- 使用 RANSAC 抗 GPS 跳点和视觉坏帧，最终用内点重估；
- 输出内点数、内点率、中心残差中位数/P95/最大值和尺度；
- 尺度必须为有限正数，残差或尺度异常时不发布“姿态已就绪”；
- Sim3 的旋转部分左乘到 COLMAP 相机世界姿态；平移和尺度只用于变换诊断点云，不改变最终 SRT 中心；
- 所有矩阵/四元数约定以合成 north/east/down、绕轴旋转和 round-trip 测试固定，禁止通过 UI 肉眼试符号。

### 缺失帧与姿态微调

已注册关键帧的绝对姿态通过同一权威 PTS 时间轴做四元数 SLERP。短边界可以保持最近姿态，长内部缺口不得无条件跨越。自动解可靠时用户不是从零手调姿态，而是在已显示的相机视锥上检查并添加少量 yaw/pitch/roll 修正关键帧。

如果 COLMAP 或 Sim3 失败，仍发布 SRT 位置轨迹和明确诊断，但渲染保持禁用。人工单锚点只作为显式降级工具，不能再成为正常流程的必经步骤。

## 工作台与进度

固定轨迹路线使用专属阶段，不复用普通 SfM 右侧控件：

1. `视觉姿态计算`：显示抽帧、特征提取、匹配、稀疏注册、SRT/CAD 对齐、轨迹发布的阶段进度；
2. `姿态检查与微调`：默认显示 SRT 固定轨迹、对齐后的相机视锥和可选稀疏点云；允许统一 XYZ 路线偏移和 yaw/pitch/roll 姿态修正；
3. `标牌编辑与渲染`：复用其他工作流的渲染能力，与微调明确分开。

右侧面板显示本路线需要的信息：位置来源、姿态来源、FOV、抽帧间隔、COLMAP 注册率、Sim3 内点/残差、姿态覆盖率、统一 XYZ 偏移、点云/轨迹/视锥显示开关和姿态关键帧。隐藏普通 SfM 的自由轨迹、通用相机初值、质量检测和会破坏位置硬约束的控件。

完整 SRT 路线使用：

1. `生成 SRT 全姿态轨迹`（仅首次或输入变化时）；
2. `姿态检查与微调`；
3. `标牌编辑与渲染`。

它不显示 COLMAP 注册、稀疏点云或视觉姿态参数。

## 产物契约

固定轨迹 adapter 升级版本，至少发布：

- `02_srt_visual_pose/camera_trajectory_visual_pose.json`：最终 SRT 锁定中心与视觉姿态；
- `02_srt_visual_pose/camera_trajectory_colmap_aligned.json`：仅诊断用的对齐视觉轨迹；
- `02_srt_visual_pose/sparse_points_aligned.ply`：可选显示的对齐稀疏点；
- `02_srt_visual_pose/sim3_srt_alignment.json`：变换、同帧对应与鲁棒残差；
- `02_srt_visual_pose/orientation_diagnostics.json`；
- `02_srt_visual_pose/visual_pose_report.md`。

最终 trajectory metadata 必须声明：

- `position_source=srt_cad_locked`；
- `orientation_source=colmap_sim3_aligned`；
- `metric_scale_locked=true`；
- `route_offset_xyz_m`；
- COLMAP 后端、设备、抽帧策略、FOV、注册率、Sim3 统计和姿态覆盖率。

## 失败与状态语义

- SRT 位置、相对高度、CAD georeference 或 FOV 不满足预检：不启动任务；
- COLMAP 注册不足：按 0.5 秒自动补密重试一次；
- Sim3 同帧数/非共线性/残差不满足门禁：发布 `position_only` 和诊断，禁止渲染；
- 姿态只覆盖部分区间：发布 `orientation_partial`，只在有效帧显示视锥；
- 姿态和同步满足门禁：发布 `orientation_ready`；
- 任意视觉失败都不得自动切回旧 `srt_sfm_fused`，也不得覆盖 SRT 中心；
- 不再单设“质量检测”阶段，注册率、重投影误差、Sim3 残差和姿态覆盖率是进入渲染前的技术门禁。

## TDD 验收范围

实现计划必须先写失败测试，再按以下顺序落地：

1. XML/SRT 解析、帧号映射、四元数 SLERP、末尾保持和源字段不可变测试；
2. 真实文件合并 smoke：12017 条、100% 姿态覆盖、`srt_full_pose` 路由和 59.109° 报告；
3. full-pose 首次进入工作台的阶段/文案测试，确保不出现“开始 SfM 重建”；
4. 固定轨迹 adapter 命令与产物测试，证明运行 COLMAP、允许内部稀疏点且不运行 dense reconstruction；
5. 合成 Sim3 恢复、异常点 RANSAC、相机姿态变换和 SRT 中心逐帧硬锁定测试；
6. 1.0 秒首轮、0.5 秒补密重试、800 张上限和首尾帧保留测试；
7. COLMAP 失败/Sim3 失败发布 position-only 且禁止渲染的测试；
8. 工作台专属阶段、右侧控件、视锥、稀疏点开关、统一 XYZ 偏移及姿态关键帧测试；
9. 微调与渲染分离、无独立质量检测测试；
10. 真实三文件端到端 smoke，并与 XML 101 个姿态锚点计算角度误差和耗时；
11. `srt_full_pose`、`sfm_only`、项目库、任务恢复和渲染回归测试。

## 完成定义

- 合并 SRT 能在新项目中被自动识别为完整姿态 SRT，输入测试 FOV `59.109°` 后，生成完整轨迹并在工作台看到视锥；
- 原始 SRT 的位置、高度、时间和其他字段没有任何变化；
- 不完整 SRT 路线默认由 COLMAP 自动给出绝对姿态和可选稀疏点，用户只做检查/微调；
- 最终轨迹任意帧位置严格等于 SRT→CAD 中心加统一 XYZ 偏移；
- 微调和渲染是两个独立阶段，界面不再显示与该路线无关的 SfM 控件和质量检测；
- 外部 XML 只作为姿态合并输入与精度基准，不成为生产不完整 SRT 工作流的依赖。
