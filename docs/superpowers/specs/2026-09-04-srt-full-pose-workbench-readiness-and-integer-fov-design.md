# SRT Full-Pose Workbench Readiness and Integer FOV Design

## Goal

完整姿态 SRT 项目必须先生成并验证轨迹，再进入工作台。工作台打开时应直接显示 SRT 轨迹、当前帧相机视锥、整数水平 FOV 和整轨 XYZ 微调控件，任何状态都不得显示或启动 SfM。

## Confirmed Root Cause

项目 `p-4550302b3ede4a51` 已正确解析为 `srt_full_pose`，CAD 地理参考也已确认，但创建出的工作台会话仍是 `launch_mode: workflow_start`，且没有 `trajectory_job_id`、`trajectory_output_revision` 或 `trajectory_output_fingerprint`。原因有两层：

1. `ProjectWorkbenchService.resolve_context` 只要求 `srt_fixed_track_visual_pose` 在进入工作台前具有有效轨迹，遗漏了 `srt_full_pose`；
2. `ProjectApi._create_workbench_session` 只会为固定轨迹视觉姿态路线自动提交轨迹任务，完整姿态路线会退回普通片段准备逻辑。

因此页面在轨迹产物不存在时提前打开。虽然两步导航已按 `srt_full_pose` 隐藏多余步骤，内部会话仍处于 `sfm`，通用状态文案便显示“SfM 重建”，轨迹、视锥和 FOV 参数也没有来源。

FOV 弹出“两个最接近的有效值”则来自前端 `step="0.1"` 与 `min="1.01"` 形成的浏览器步长网格，而不是镜头数据库匹配。

## Selected Flow

采用“轨迹先于工作台”方案：

1. 用户在项目页点击进入工作台；
2. 若物理视频或 CAD 工作台数据尚未准备，沿用现有片段准备任务；
3. 若工作流是 `srt_full_pose` 且尚无当前输入对应的已验证轨迹，后端自动执行轨迹预检并提交 `srt_full_pose` 轨迹任务；
4. 项目页保持在准备进度弹窗，文案为“正在生成 SRT 全姿态轨迹”，展示真实任务进度；
5. 任务成功且适配器输出验证通过后，重新创建工作台会话；
6. 只有携带轨迹任务 ID、输出修订和输出指纹的 `trajectory_ready` 会话才能打开完整姿态工作台；
7. 工作台从已发布的 `camera_trajectory_full_pose.json`、`camera_track_pred.json` 和 `sfm_viewer_scene.json` 初始化，直接进入“轨迹微调”。

## Backend Rules

- `srt_full_pose` 与 `srt_fixed_track_visual_pose` 都属于“进入工作台前必须有已验证轨迹”的工作流。
- 创建会话时若完整姿态轨迹缺失，API 返回 `202 preparing_trajectory`，不创建空白 `workflow_start` 会话。
- 轨迹任务必须与当前 clip 分析修订、当前 CAD/SRT/FOV/地理参考输入指纹一致。
- 完整姿态工作台会话若缺少有效轨迹，协调器拒绝创建，避免其他调用路径绕过 API。
- 完整姿态工作流永远不允许执行 SfM 或质量检测。

## Frontend Rules

- 项目页区分完整姿态和视觉姿态准备文案；完整姿态显示“正在生成 SRT 全姿态轨迹”。
- 完整姿态任务成功后自动进入工作台，无需用户再次点击。
- 工作台完整姿态模式只显示“轨迹微调”和“渲染导出”。
- 即使收到异常的 `workflow_start` 会话，状态也显示“全姿态轨迹尚未准备”，不能显示“SfM 重建”或暴露 SfM 启动动作。
- 轨迹微调区显示项目整数 FOV（只读）、当前整轨 XYZ 偏移和保存入口；三维视图显示原始 SRT 轨迹、调整后轨迹与当前帧相机视锥。

## Integer FOV Contract

- SRT 配置输入框采用整数步长 `1`，范围保持 `2` 到 `178` 度。
- 打开历史小数配置时，界面以四舍五入后的整数显示；`59.11` 显示为 `59`。
- 前端只提交整数，后端配置写入也统一四舍五入为整数，避免 API 调用绕过界面。
- 当前测试项目的下一次保存会把 `59.11` 持久化为 `59`；轨迹输入指纹随之变化，旧轨迹不得复用。
- 工作台和渲染全程使用同一个已保存整数 FOV，不再尝试匹配相机标定候选。

## Error Handling

- 轨迹预检失败时留在项目页，直接显示缺失的坐标系、FOV、SRT 字段或物理输入原因。
- 轨迹任务失败时关闭等待循环并显示适配器错误，不打开工作台。
- 轨迹成功但产物缺失或指纹过期时视为失败，不允许降级到 SfM。
- 历史空白完整姿态会话刷新时应恢复到项目页重新创建会话，不能继续写入或渲染。

## Tests

1. API 集成测试：完整姿态物理片段就绪但轨迹缺失时返回 `202 preparing_trajectory`，并提交 `srt_full_pose` 适配器任务。
2. 会话单元测试：完整姿态缺少有效轨迹时拒绝创建；有效轨迹时创建 `trajectory_ready` 会话并使用 `keyframes` 恢复阶段。
3. 前端静态/行为测试：完整姿态准备文案、无 SfM 文案和动作、成功后自动打开工作台。
4. FOV 测试：输入框为整数，历史 `59.11` 显示为 `59`，服务持久化整数，适配器获得相同整数。
5. 工作台回归：完整姿态轨迹、视锥、只读 FOV 和 XYZ 控件均有初始化来源。
6. 兼容回归：`sfm_only`、`pure_rotation` 和 `srt_fixed_track_visual_pose` 行为不变。

## Acceptance Criteria

- 从项目页点击进入完整姿态工作台时，先看到真实的全姿态轨迹生成进度。
- 工作台打开后当前任务为“轨迹微调”，不出现“SfM 重建”。
- 三维视图能看到 SRT 路线和当前帧视锥，微调区能看到整数 FOV 与 XYZ 偏移。
- 保存微调后进入独立渲染阶段，渲染继续使用 SRT 位置、SRT 姿态和同一个整数 FOV。
