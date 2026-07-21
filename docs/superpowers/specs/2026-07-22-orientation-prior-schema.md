# Stage 6B-1b-a：人工姿态先验包 Schema 与资格门

本文件定义 Rank-1 constrained alignment 未来会消费的、版本化人工姿态先验包。本阶段只解析和验证先验，**没有**实现 Rank-1 求解器，也没有接入正式前端。

## Schema v2

顶层 `camera_track_schema_version` 语义由 `schema_version: 2` 表达。它必须声明：

- `coordinate_system: web_cad_world`；
- `pose_convention`：位置单位、世界轴（CAD XY 地面、Z 向上）、相机轴（右/下/前）、角度单位、`camera_to_world` 语义、前端 pitch 约定，以及到 Python 后端状态的转换版本；
- `keyframes`：每帧的原视频 `source_frame_index`、`pts_time_sec`、人工相机参数、姿态元数据和先验角色。

关键帧姿态元数据包含 `orientation_source`、`orientation_confirmed`、yaw/pitch/roll 各自的确认标记、投影检查标记和可选像素残差。`prior` 包含是否启用、方向类型、`solve|validate|none` 角色与质量状态。

## 旧 Schema 兼容

旧的 `camera_track_manual.json` 仍由既有 no-SRT loader 原样读取。本模块只在内存中将旧记录规范化为兼容视图：`frame` 与 `time` 分别标记为 `legacy_inferred` 的原视频帧号和 PTS；坐标系标为 `web_cad_world_legacy`；角度默认 degree 和既有姿态约定。

旧记录的姿态确认、roll 确认均默认为 `false`，角色为 `none`。因此它们不会自动获得 Rank-1 第二方向先验资格。

## 姿态与方向约定

先验包先复用 `web_camera_to_python_state`：位置由 web CAD world 转为 CAD meters，且前端 pitch 在进入 Python 状态时反号。再复用现有 `_camera_to_world_rotation` 生成 `rotation_cad_from_camera`。矩阵列依次为 camera right、camera down、camera forward；因此 `camera_up = -camera_down`。旋转必须正交且行列式约为 +1，所有方向均归一化。

支持候选方向：

- `camera_forward`：已确认 yaw 与 pitch 即可，不依赖 roll；
- `camera_up`、`camera_right`：还必须确认 roll；
- `cad_up`：只作为 CAD 世界方向元数据，不能被当作 ENU Up 或单独完成对齐；
- `ground_normal`：只有未来提供版本化来源和质量字段时才可用；当前拒绝。

## 资格门

`qualify_orientation_prior` 检查原视频帧号、PTS、有限相机参数、允许的人工来源、姿态确认状态、方向类型、单位向量和角色。默认最小非共线夹角是 **20°**。

夹角按无向轨迹轴计算：`min(angle(d_prior, d_track), angle(d_prior, -d_track))`。小于阈值时以 `prior-near-parallel-to-trajectory` 拒绝。算法预测、未确认姿态、零/非有限方向以及未确认 roll 的 up/right 同样拒绝。

## 求解/验证分离

角色仅能为 `solve`、`validate` 或 `none`。未来正式验收需要至少一个已通过资格门的 solve prior 和一个独立 validate anchor；同一 `source_frame_index` 不能同时参与两种角色，validate anchor 不得进入求解器。本阶段的 `validate_prior_split` 只验证这一 hold-out 协议；它不运行任何变换求解。

## 当前边界

- 没有 Rank-1 constrained alignment；
- 没有标准 Sim3 改动；
- 没有融合相机轨迹或点云变换；
- 没有正式 UI、Job Runner 或保存接口改动。

后续实现仍需明确人工 anchor 如何与 SfM 旋转观测配对、CAD/ENU 完整轴链、第二方向先验质量阈值及其独立验证协议。
