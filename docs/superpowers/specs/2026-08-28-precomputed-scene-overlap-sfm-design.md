# 同场景预计算重叠 SfM 与快速路线打通设计

## 目标

把当前“点击向上/向下打通后临时再次运行 SfM”改为“首次批量轨迹反算时一次性计算同场景隐藏重叠区”。同一场景的任意一个片段完成路线拟合和微调后，可以利用预计算的共同 source PTS 快速为相邻片段生成初始路线；点击打通时不得再运行 SfM。

本设计只在隔离分支 `codex/async-job-progress-scene-positioning` 验证，不合入 main。旧轨迹不保留慢速兼容路径，需在该分支重新运行轨迹反算后才能使用快速打通。

## 核心区间与求解区间

每个逻辑片段保留两套互不混用的权威区间：

- `core_interval`：现有 source decoded-frame integer PTS 半开区间。它仍是工作台视频、渲染、render frame map 和 concat 的唯一权威范围；
- `solve_interval`：只供 `sfm_only` 重建和相邻路线打通使用的隐藏扩展区间。

`solve_interval` 必须根据 `scene_index` 和 `segment_index` 统一计算：

- 同一场景只有一个片段：`solve_interval = core_interval`，不具备打通能力；
- 同一场景第一段：只向后扩展 4 秒；
- 同一场景中间段：向前、向后各扩展 4 秒；
- 同一场景最后一段：只向前扩展 4 秒；
- 扩展边界必须吸附到源 decoded-frame PTS，并裁在本场景首尾核心区间内；
- 不得读取相邻 `scene_index` 的任何帧。

相邻同场景片段在一个边界两侧各扩展 4 秒，因此最多具有约 8 秒共同 source PTS。正式 core frame map、输出帧数和 concat 分区不得改变。

## 一次 SfM 的产物

现有核心视频继续用于工作台、预览和渲染。对多片段 `sfm_only` 场景新增独立的 `sfm_solve_export` 后台任务，导出一个 solve 视频和精确 solve frame map。视频导出可以增加一次，但 SfM 只能运行一次。

`sfm_only` 轨迹任务对 solve 视频执行 SfM，并发布：

- 完整 solve trajectory：保留 solve 视频局部 frame ordinal，并为每个 pose 绑定精确 source PTS；
- 核心 trajectory：从完整 solve trajectory 按 source PTS 投影到 core frame map，frame index 重新编号为核心视频局部 ordinal，保持现有工作台和渲染接口；
- 同一次重建的 sparse point cloud；
- solve/core frame map 身份和输出校验证据。

单片段场景直接使用 core 视频进行一次 SfM，不生成额外 solve 视频，也不显示打通按钮。Pure Rotation、SRT 工作流不进入 scene bridge。

`sfm_solve_export` 和 `sfm_only` adapter 版本升级。旧版本轨迹在该实验分支中自然变为非当前结果，用户需重新轨迹反算；项目资产、人工成果和 main 不被删除或改写。

## 快速打通

打通按钮仍位于已保存路线成果的源片段。服务端只允许以下组合：

- 源和目标 `scene_index` 相同；
- 源和目标按 `segment_index` 紧邻；
- 所属场景至少两个片段；
- 两者都是 `sfm_only`；
- 两者都有当前版本且验证通过的 solve/core SfM 产物；
- 源片段存在当前已保存 workbench 路线；
- 目标片段没有活动或已保存的人工工作台成果。

点击后执行以下轻量流程，不再导出 solve 视频，也不再调用 `run_sfm`：

1. 将源 workbench 的核心关键帧按 core frame map 转为 source PTS，再映射到源 solve frame ordinal；
2. 使用现有路线拟合对源 solve trajectory 求 CAD 对齐路径；
3. 从源、目标 solve frame map 的交集中选择两个不同且间隔足够的精确 source PTS；
4. 用源 CAD 路径在两个共同 PTS 的相机位姿构建目标 solve 锚点；
5. 使用现有路线拟合对齐目标 solve trajectory；
6. 将目标 solve CAD 路径按 source PTS映射到目标 core trajectory，生成两个核心锚点；
7. 使用现有核心路线拟合发布不可变 scene bridge，并进入 `awaiting_route_refinement`；
8. 用户进入工作台微调，随后才继续质量检测。

当目标片段微调并保存后，它可以作为新的源片段继续向同场景下一段或上一段传播。

## 数据、失效与状态

solve 导出和轨迹身份至少绑定：分析 revision、scene/segment 顺序、core/solve 精确 PTS、源视频身份、算法版本和 frame map 哈希。以下变化使 solve/trajectory 失效：

- 视频、分析 revision、片段核心区间或场景分组改变；
- 重叠长度或区间算法版本改变；
- SfM adapter 版本或参数改变。

同坐标系 CAD 版本替换不得使原始 SfM 或 solve export 失效。源 workbench 路线变化只使依赖它的 scene bridge 失效，不重新运行 SfM。

项目管理行的主要状态继续表示轨迹或渲染状态。旧 scene bridge 为 `stale_input` 时，不得覆盖一条仍为 success 的 SfM 轨迹；应在 scene bridge 子状态中显示“旧打通结果已失效，可重新打通”。打通任务运行期间可以临时显示其单调进度。

## 失败处理

- solve 区间越过场景边界：准备阶段拒绝；
- solve/core frame map 无法精确关联：轨迹验证失败，不发布部分结果；
- 两个 solve reconstruction 少于两个共同注册 source PTS：打通失败并保留目标原状态；
- 任何输入在运行中变化：候选标记 stale，不发布 active bridge；
- 禁止单锚点、近似帧匹配、跨场景回退和点击时临时 SfM。

## TDD 与验收

测试至少覆盖：

- 单片段、首段、中间段、末段以及相邻不同场景的 solve 区间；
- VFR 视频下 4 秒扩展吸附到精确 decoded-frame PTS；
- core frame map、渲染帧数、output ordinal 和 concat 分区完全不变；
- 一个 solve SfM 同时发布 solve trajectory 与正确重编号的 core trajectory；
- 快速打通命令中不存在 `run_sfm` 和 solve 视频导出；
- 同场景相邻片段成功打通并进入微调；
- 不同场景、单片段、Pure Rotation、旧 adapter 产物被拒绝；
- 旧 bridge stale 不遮蔽成功轨迹状态；
- 源微调成果可继续链式传播；
- 聚焦测试、全量测试和真实 `hygs` 隔离副本 smoke 通过。
