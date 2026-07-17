# Stage 6B-1：Partial-SRT 斜视场景轨迹融合核心设计

## 目标与边界

本阶段为 `srt_sfm_fused` 提供离线后端融合核心和独立 CLI。输入为视频、partial-SRT（时间、GPS、相对或绝对高度）和 COLMAP SfM 轨迹；输出为保留 SfM 相机旋转、位置受 SRT 低频约束的局部 ENU 轨迹。该轨迹随后继续交给现有人工关键帧和 `align_to_cad` 完成 CAD 对齐。

不实现 SRT 姿态驱动、SRT/COLMAP Bundle Adjustment、GPS 到 CAD 自动投影、正式 Job Runner 接入或前端大改。原始 `02_sfm/camera_trajectory.json` 永不覆盖；no-SRT 工作流不改变。

## 模块职责

| 模块 | 职责 |
| --- | --- |
| `cadscene/srt/synchronization.py` | 以视频 PTS 优先、恒定帧率 `frame_index / fps` 回退的时间映射；SRT 线性插值、覆盖与空洞判定。预留 `time_scale`、`time_offset_sec`。 |
| `cadscene/srt/coordinates.py` | WGS84 ECEF 到本地 ENU；高度来源选择与相对化。无需新增运行依赖。 |
| `cadscene/srt/trajectory.py` | SRT entry、帧采样和局部 ENU 轨迹的强类型数据结构、CSV/JSON 序列化。 |
| `cadscene/srt/quality.py` | 覆盖率、重复 GPS、跳点、速度、高度突变、空间 baseline、退化与样本质量统计。 |
| `cadscene/srt/fusion.py` | 时间均匀采样、空间去重、确定性 RANSAC Sim3、残差统计、按时间滑动中位数融合、置信度和四元数坐标变换。 |
| `cadscene/cli/fuse_srt_sfm.py` | 显式参数优先、manifest 路径回退、产物写入、中文控制台摘要及明确失败码。 |

Stage 6A 的 parser/capability 模块是唯一 SRT 文本解析入口；6B-1 不创建第二套 DJI SRT parser。

## 输入、同步与高度策略

每个 SfM 注册 pose 使用 `frame_index` 对应视频时间。优先级如下：

1. 若可从视频解封装获得该帧 PTS，`frame_time_sec = pts_sec * time_scale + time_offset_sec`；
2. 仅在视频为恒定帧率且 PTS 不可用时，使用 `frame_index / video_fps * time_scale + time_offset_sec`；
3. `time_scale` 默认 `1.0`，`time_offset_sec` 默认 `0.0`；本阶段不自动估计它们。

SRT 使用 cue start/end 时间而非记录序号。位置和高度在合法相邻 entry 之间线性插值；覆盖区间外、空洞超过阈值、缺字段或重复时间的采样标记为无效，绝不外推为有效 GPS。

高度字段始终保留 `rel_alt`、`abs_alt`、`selected_height`、`height_source`：默认 `auto` 优先 `rel_alt`，否则使用 `abs_alt - first_valid_abs_alt`，两者都不可用时不伪造高度。高度只表示局部变化，绝不等同于 CAD 的 `z=0`。

## 坐标与数学约定

有效 WGS84 `(lat, lon, selected_height)` 先转 ECEF，再以首个有效 GPS 为 ENU 原点，得到 `(east_m, north_m, up_m)`；禁止直接把经纬度差视为米。

对共同有效帧估计：

`P_srt ≈ s · R_sim3 · P_sfm + t`

其中 `P_sfm`、`P_srt` 为列向量，`s > 0`，`R_sim3` 是从 SfM 世界坐标到 ENU 世界坐标的主动旋转。估计前必须：

- 按时间均匀采样；
- 按最小 ENU 位移空间去重；
- 使用固定 RANSAC 随机种子；
- 检查去重后点集的 rank、中心化奇异值、XY/3D baseline、线性与平面退化。

退化检查不能只看样本数。若轨迹近静止、近纯旋转、有效空间 baseline 太短，或奇异值表明约束不能稳定确定所需自由度，CLI 必须失败，不能输出虚假的高置信 Sim3 或 fused trajectory。

Sim3 拟合按 `vertical_weight` 加权：XY 残差与 Z 残差分别统计，同时将高度分量乘以该权重进入 RANSAC 评分和残差计算。默认 `vertical_weight=0.5`，避免低变化或高噪声高度主导估计。

### 相机旋转约定

输入 `cam_from_world_quat_wxyz` 表示世界坐标向相机坐标的主动旋转矩阵 `R_cw_old`，四元数顺序为 `(w, x, y, z)`。世界点变换为 `P_new = s · R_sim3 · P_old + t` 后，保持相机坐标不变的等价关系为：

`R_cw_new = R_cw_old · R_sim3^T`

实现使用项目现有矩阵/四元数转换函数，不手写不同约定的重复代码。必须以非单位 `R_sim3` 的投影等价性测试验证：对同一世界点，旧世界/旧相机投影与新世界/新相机投影一致。

## 融合方法与置信度

先把 SfM 位置变换为 `P_sfm_metric`，在共同有效帧计算：

`r = P_srt - P_sfm_metric`

对 `r` 在时间轴使用可配置的鲁棒滑动中位数，默认窗口 `2.0` 秒，得到 `r_smooth`；每个帧输出：

`P_fused = P_sfm_metric + r_smooth`

这样保留 SfM 高频局部形状，以 SRT 修正尺度和低频漂移。SRT 跳点不能逐帧复制到输出。SRT 覆盖范围外不外推 `r_smooth`：仅输出 `P_sfm_metric`，并降低置信度。

`fusion_confidence` 范围为 `[0, 1]`，表示内部数据一致性和融合可靠性，不是绝对定位精度或误差概率。每帧同时输出可解释组成项：SRT 有效性、距最近 SRT 时间、插值跨度、Sim3 inlier、加权残差、GPS 跳变标记、SfM 注册状态、观测数和重投影误差。

位置字段标记 `position_source=sfm_srt_fused`；方向字段固定标记 `orientation_source=sfm`，禁止任何 `orientation_source=srt`。

## 数据 schema 与产物

`02_srt/srt_frame_samples.csv` 最少含：

`frame_index, frame_time_sec, pts_time_sec, srt_time_sec, latitude, longitude, rel_alt, abs_alt, selected_height, height_source, gps_valid, height_valid, interpolated, source_entry_before, source_entry_after`

`02_srt/srt_trajectory.json` 保存 ENU 原点、所有有效 SRT 样本、原始高度字段和轨迹统计；`srt_sync_stats.json`、`srt_sync_report.md` 记录同步源、`time_scale`、offset、时长差、重叠率和 warning。

`02_fusion/camera_trajectory_fused.json` 与既有 trajectory loader 兼容，保留 `fps`、尺寸、intrinsics 和 poses。顶层 `meta` 至少含 `trajectory_mode=srt_sfm_fused`、`coordinate_system=local_enu`、position/orientation source、height source、SRT origin、完整 Sim3、同步参数和 `vertical_weight`。每个 pose 保留旧字段，并可追加 `srt_valid`、`srt_interpolated`、`srt_residual_m`、`fusion_confidence` 与 `fusion_confidence_components`。

`02_fusion/fused_camera_path.csv` 输出 `frame_index,x,y,z,yaw,pitch,roll,fov,trajectory_source,srt_valid,srt_residual_m,fusion_confidence`。`trajectory_comparison.csv` 同时包含 transformed SfM、raw/interpolated SRT、fused 位置、XY/Z residual 和 confidence。报告必须中文；诊断图可选，且不接入正式前端。

## 默认参数与失败条件

默认参数放入单一 `FusionConfig`，不散落硬编码：`time_scale=1.0`、`time_offset_sec=0.0`、`smoothing_method=rolling_median`、`smoothing_window_sec=2.0`、`vertical_weight=0.5`、固定 `ransac_seed`、最少共同帧、最小 XY baseline、最小时间重叠、GPS/高度有效率、空间去重距离、RANSAC 阈值、合理 scale 区间、最大 GPS 速度与最大高度跳变。

以下情形明确失败并建议“当前 partial-SRT 不适合融合，可回退到 sfm_only”：无有效 GPS、缺少可用高度（非 `height-source=sfm` 时）、共同帧不足、重叠不足、去重样本不足、rank/奇异值/空间 baseline 退化、速度或跳变后没有足够样本、Sim3/RANSAC 失败、scale 异常、NaN/Inf 或输出 schema 不兼容。CLI 不静默回退。

## CLI 与兼容策略

`python -m cadscene.cli.fuse_srt_sfm` 提供 `--dataset`、`--run-id`、`--output-root`、`--trajectory`、`--srt`、`--video`、`--time-scale`、`--time-offset-sec`、`--height-source`、`--smoothing-method`、`--smoothing-window-sec`、`--vertical-weight`、质量阈值和 `--export-diagnostics`。显式路径优先，manifest 仅作回退。

CLI 仅写 `02_srt/` 和 `02_fusion/`，不覆盖 `02_sfm/`。现有 `align_to_cad` 增加“显式 trajectory path”接入（如确有必要），使用相同 loader，不复制 SRT 专用 CAD 对齐逻辑。no-SRT 仍指向原 SfM trajectory。

## 验收指标与测试

合成测试必须覆盖：

1. PTS 优先同步、恒定帧率回退、非整数 FPS、time scale/offset、覆盖外无效；
2. WGS84→ENU 数值合理、相对高度选择；
3. 已知 Sim3 恢复，固定 seed 可复现，XY/Z 残差单独输出；
4. rank/奇异值、短 baseline、线性/平面退化、纯旋转明确拒绝；
5. 时间均匀采样和空间去重避免重复 GPS 过度投票；
6. GPS 跳点不直接进入 fused position，低频漂移向 SRT 修正；
7. 非单位 Sim3 旋转的四元数投影等价性；
8. 旧 `load_sfm_trajectory` 可读取 fused JSON；
9. fused JSON 可运行 `align_to_cad` smoke；
10. no-SRT 回归与 `check_no_project_dependency.py` 通过。

真实数据 smoke 仅使用本地未提交数据。报告必须分别给出：XY/Z 残差、时间均匀且空间去重后的样本数、RANSAC 内点比例、XY/3D baseline、低置信区间、SRT 阶梯/重复检查以及与 sfm_only、metric SfM、raw SRT、fused trajectory 和人工关键帧 CAD 投影的对比。轨迹视觉平滑不是验收充分条件。

## 当前限制

普通 DJI SRT 不是 RTK 真值；`fusion_confidence` 不承诺绝对精度。partial-SRT 没有完整相机/云台姿态，因此 SRT 不能单独提供斜视相机方向。本阶段融合结果是局部 ENU，仍需人工关键帧与现有 CAD alignment 才能进入工程坐标。
