# Rank-1 第二方向先验可行性审计

## 1. 当前关键帧数据结构

当前人工关键帧保存为 `camera_track_manual.json` 的 `keyframes[]` 项。前端 `makeKeyframe` 与 `cloneCameraPose`（`apps/web_camera_viewer/viewer_legacy.js`）实际写入：

- `frame`、`time`、`source`；
- `camera.x/y/z`；
- `camera.yaw/pitch/roll`；
- `camera.fov`。

保存接口 `save_camera_track`（`cadscene/workflow/job_runner.py`）原样原子写入该 JSON；没有另行补充四元数、旋转矩阵、CAD 对应点、地面法向或方向置信度。`frame` 是前端视频帧号，不是显式命名的 `source_frame_index`；该 schema 没有 PTS、抽帧序号或 COLMAP image id 字段。

关键帧位置不是屏幕二维点击。用户在三维 viewer 中调整虚拟相机状态后保存；`getCameraAxes`（`apps/web_camera_viewer/viewer_legacy.js`）用该姿态投影 CAD。前端坐标是 `web_cad_world`：CAD 位于 XY 地面、Z 向上。`web_camera_to_python_state`（`cadscene/core/coordinates.py`）将位置按 `(x-origin_x)*cad_scale`、`(y-origin_y)*cad_scale`、`z*cad_scale` 转为 CAD meters；yaw/roll 保持，pitch 取反。后端 `CameraState`（`cadscene/core/camera.py`）明确使用 CAD meters。

角度是度。前端 `getCameraAxes` 和后端 `_camera_to_world_rotation`（`cadscene/alignment/aligner.py`）给出了实际轴与组合公式：相机局部轴为 right/down/forward，后端 pitch 是向下角，roll 围绕 forward 旋转。它们没有以 schema 字段声明 Euler order、`camera_from_world`/`world_from_camera` 或 pose basis；这些约定目前只存在于代码实现。SfM 轨迹另存 `cam_from_world_quat_wxyz`，但人工关键帧本身不保存四元数。

`build_correspondences`（`cadscene/alignment/aligner.py`）把同一 `frame` 查询 SfM 中心与 `r_camfromworld_sfm`，并把人工状态解释为 CAD 世界位姿。`alignment.json` 的 anchors 因而同时记录 CAD 相机中心、yaw/pitch/roll 以及 SfM 相机中心；`camera_track_pred.json` 则保留人工帧并标注 `coordinate_system: web_cad_world`。

## 2. 当前人工标定实际约束了什么

当前人工标定约束的是“在 CAD 世界中使该视频帧投影匹配”的完整虚拟相机状态：三维相机中心、yaw、pitch、roll 与水平 FOV，而不是 CAD 点对应表。`confirmed_keyframes`（`cadscene/alignment/keyframes.py`）只排除 `algorithm_prediction`，因此 `manual_anchor`、`manual_corrected` 和确认帧都会成为对齐 anchor。

现有 CAD 对齐已将人工姿态用于 SfM→CAD：`_estimate_global_sim3_from_oriented_keyframes` 计算 `R_cad_from_sfm = R_cad_from_cam_manual @ R_cam_from_sfm`，再用人工 CAD 位置估计尺度和平移。这证明可从人工姿态构造 CAD 世界的相机 forward/up/right；但这也是人工信息已经参与现有 CAD 对齐的证据。

## 3. 可提取的候选第二方向

- **camera forward**：可由 yaw/pitch/roll 通过 `getCameraAxes` 或 `_camera_to_world_rotation` 得到，且在斜视影像中通常最直接对应用户观察的投影效果；但若相机朝向接近飞行主方向，则与 Rank-1 主方向近乎平行，必须拒绝。
- **camera up / right**：也可由同一完整姿态推导，能作为与主方向非共线的第二向量；但它尤其依赖手工 roll。当前 UI 允许 roll 调整/锁定，却没有记录 roll 是否经用户可靠确认，因此不能默认高可信。
- **CAD Up**：`[0,0,1]` 是现有 `web_cad_world` 的明确世界方向，数值稳定且通常不平行于近水平航迹；但它是 CAD 世界方向，不是 ENU Up，不能在未知 ENU→CAD 变换时直接充当 SfM→ENU 的第二方向。
- **地面法向**：当前关键帧 schema 和 CAD metadata 没有保存“此帧使用的可信地面法向”。不能从普通 CAD 图层或单个关键帧自动假定得到，需要显式选择并记录。

未来若使用候选向量，必须定义并配置与轨迹主方向的最小夹角，接近平行时拒绝；同时记录方向类型、先验来源、是否依赖 roll、单帧投影残差。单帧仅适合作为候选先验；至少应由独立第二人工帧、独立 CAD 几何或其他先验交叉验证。当前代码没有这类夹角门、来源字段或双帧验证。

## 4. 坐标链与循环依赖

标准 partial-SRT 的目标链为 `SfM → ENU → CAD`，而当前人工关键帧的语义直接是 `SfM frame ↔ CAD world pose`，不是 ENU pose。

方案 A 试图从 CAD 人工姿态取得 ENU Up，再完成 SfM→ENU，最后用同一关键帧做 ENU→CAD。现有代码中没有独立 ENU→CAD 姿态，因此 CAD Up 不能无歧义变成 ENU Up；同一锚点同时提供第二方向和最终 CAD 对齐/评价还会造成循环依赖与虚高残差。

方案 B 使用 SRT 只提供沿程尺度、时间配对位置统计和低频约束，而把人工关键帧作为直接的 SfM→CAD 姿态先验，更符合当前 schema。此时不应声称得到独立的 ENU 完整姿态；SRT 对尺度的贡献、人工姿态先验和 CAD 对齐评价必须分开记录。用于求解方向的 anchor 不得同时作为唯一验收 anchor，至少保留独立人工帧作 hold-out 验证。

方案 C（关键帧仅有二维投影）不符合当前实现，因为真实保存了三维相机中心与完整欧拉姿态；但当前缺少姿态约定、质量和来源元数据，故它们尚不能被无条件提升为可靠自动先验。

## 5. 推荐方案

推荐 **方案 B（带显式资格门的直接 SfM→CAD 方向先验）**。

理由是人工位姿已经定义于 CAD 世界，现有对齐器也以该语义组合人工 `R_cad_from_cam` 与 SfM `R_cam_from_sfm`。把它反推为 ENU Up 会依赖尚未知的 ENU→CAD 方向，形成不必要的循环。SRT 仍应保留为沿程尺度、位置一致性和低频漂移诊断约束，但不得被表述为已恢复完整 ENU 相机姿态。

该推荐是条件性的：先验帧必须通过与主运动方向非平行、姿态字段完整、方向来源明确、独立验证帧可用等门槛；否则退回 position-only diagnostic 或 `sfm_only`。

## 6. 实现前缺失项

实施前必须补充并版本化：

- 人工关键帧的 `coordinate_system`、轴方向、角度单位、Euler 组合/pose basis、pitch 符号；
- `frame` 与 `source_frame_index`/PTS 的显式关联；
- `orientation_source`、`orientation_confirmed`、姿态质量/投影残差和是否手工确认 roll；
- 可选的 `prior_direction_type`、地面法向或 CAD 平面来源；
- 最小方向夹角、单帧残差阈值、双帧/hold-out 验证规则；
- 哪些人工帧用于求解、哪些只用于验证的协议；
- SRT 沿程尺度约束与直接 SfM→CAD 先验如何在报告中分离；
- 若未来需要 ENU 输出，ENU→CAD 的独立轴约定与估计来源。

## 7. 下一步建议

下一小阶段只定义并验证“人工姿态先验包”的 schema：保存显式坐标/姿态约定、先验来源、roll 确认状态和独立验证标记，并用合成数据验证方向夹角拒绝与 hold-out 协议。该阶段不实现 Rank-1 对齐、不改变标准 Sim3，也不接入 Job Runner 或前端正式流程。
