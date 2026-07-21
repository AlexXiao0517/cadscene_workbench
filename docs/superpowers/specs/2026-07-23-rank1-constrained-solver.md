# Stage 6B-1b-b：Rank-1 SfM→CAD 约束对齐核心

## 1. 适用范围

该离线接口处理时间同步正常、SRT 水平 baseline 足够，但位置轨迹近似直线且空间秩为 1 的 partial-SRT 数据。标准 `rank>=2` 轨迹继续使用现有三维 Sim3；本接口不会降低标准质量门或把直线位置轨迹伪装成完整三维约束。

## 2. 数学模型

SfM 与 SRT 公共帧分别进行 PCA。第一主方向通过首尾时间前进位移消除正负号歧义，并定义沿程坐标：

`u = dot(C - C_ref, d)`。

沿程尺度使用固定随机种子的确定性一维 RANSAC，再以 inlier 两两斜率中位数和 offset 中位数细化：

`u_srt = scale * u_sfm + offset`。

它不使用单一首尾点，也不对 Rank-1 三维点执行 Umeyama。

## 3. 人工姿态与 SfM→CAD

只有通过 schema v2 资格门、角色为 `solve` 的人工关键帧参与求解。同帧 SfM 旋转与人工 CAD 相机姿态按既有公式组合：

`R_cad_from_sfm = R_cad_from_camera_manual @ R_cam_from_sfm`。

尺度来自 SRT 沿程，平移只来自 solve anchor：

`t = C_cad_manual - scale * R_cad_from_sfm * C_sfm`。

多个 solve prior 必须通过旋转一致性和平移离散度门；不会静默选择第一帧。基础变换命名为 `rank1_sfm_to_cad_base`，不是 `sfm_to_enu_sim3`。

## 4. SRT 仅沿程约束

SRT 的局部 ENU 轨迹只提供真实米制沿程尺度和低频沿程残差。基础 CAD 轨迹只沿 `d_cad = normalize(R_cad_from_sfm * d_sfm)` 修正。默认使用 2 秒时间窗口中位数，不跨无效空洞、不在覆盖外外推，也不会把横向 GPS 噪声或单帧跳点复制到 CAD 轨迹。

位置来源标记为 `rank1_sfm_srt_along_track_cad`；姿态来源标记为 `sfm_plus_manual_prior`。SRT 不修改 CAD 横向、CAD Up 或相机姿态。

## 5. 相机姿态转换

世界坐标变换为 `X_cad = scale * R_cad_from_sfm * X_sfm + t` 时，相机旋转遵循：

`R_cam_from_cad = R_cam_from_sfm * R_cad_from_sfm^T`。

合成测试会同时变换世界点和相机中心，并比较透视归一化投影，覆盖非单位全局旋转。

## 6. solve/validate 隔离

`validate` anchor 不参与 scale、rotation、translation 或沿程平滑参数估计。每个 hold-out anchor 独立报告位置总误差、沿程、横向、垂向、完整姿态角以及 forward/up/right 方向角误差。人工 `projection_residual_px` 只作为质量元数据。

正式输出要求至少一个合格 solve prior 和一个不同源帧的 validate anchor。缺少 hold-out 时只写失败诊断，不生成正式 trajectory。

## 7. 输出与失败条件

独立 CLI 输出到 `03_rank1_alignment/`，正式 trajectory 的坐标系为 `cad_meters`，模式为 `srt_rank1_manual_prior`。输出兼容现有通用 trajectory loader。

以下情况失败关闭：输入不是 rank=1 near-linear、baseline 或公共帧不足、scale 非正或不稳定、先验未确认/近平行/帧不匹配、多 solve 姿态或平移不一致、hold-out 缺失或超出质量阈值、旋转非法、输出出现 NaN/Inf。失败不会回退标准 Sim3。

## 8. 当前边界

本阶段只有离线库、独立 CLI、合成测试和报告。尚未接入正式前端、schema v2 保存 UI、Job Runner、workflow 路由或 Stage 6B-2，也未实现点云组合变换与正式渲染链。
