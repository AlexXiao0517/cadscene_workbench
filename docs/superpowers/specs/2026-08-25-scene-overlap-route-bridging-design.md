# 同场景隐藏重叠与路线自动打通设计

## 目标

同一场景只要求用户人工完成一个 SfM 片段的关键帧标定和路线拟合。已完成片段可向同场景上一段或下一段传递两个精确相机位姿锚点，目标片段自动完成路线拟合后进入人工微调，再继续现有质量检测、渲染和合并流程。

## 时间范围

每个逻辑片段保留两套互不混用的权威范围：

- `core_interval`：当前片段的 source decoded-frame integer PTS 范围，是预览交付、片段渲染、render frame map 和最终 concat 的唯一范围；
- `solve_interval`：在 core 前后各扩展默认 4 秒并裁剪到源视频边界，只用于片段准备、SfM、场景打通和路线拟合。

相邻片段的 solve interval 允许重叠，但所有 rendered.mp4 和最终合并继续严格使用不重叠的 core interval。不得改变 core 的帧数、source PTS、output ordinal 或 Stage 8 帧分区。

旧项目没有 solve interval 时，在首次执行场景打通前按源视频和权威 PTS frame map 按需生成；不迁移或改写已有 core 输出。

## 打通方向与锚点

按钮位于已保存路线拟合成果的源片段：

- `向上打通`：使用源片段 core 开头与上一片段 solve 尾部的交集；
- `向下打通`：使用源片段 core 结尾与下一片段 solve 头部的交集。

服务端从交集中选择两个不同的 source decoded-frame integer PTS。两个 PTS 必须存在于源片段已保存路线和目标 solve frame map 中，并保持足够时间间隔；第一版不做画面相似匹配，不复制近似帧，也不根据速度或方向外推相机位姿。

源路线通过现有 PTS 轨迹采样语义得到两个 CAD 相机位姿。目标片段生成两个来源为 `scene_overlap_anchor` 的已确认锚点，记录源/目标 clip、source PTS、目标 decoded frame ordinal、相机状态、CAD revision、源 workbench output revision/fingerprint 和 operation ID。

## 不可变输出与失效

场景打通输出使用独立领域和不可变 revision，不塞入 clips/jobs manifest 的业务数据。建议目录：

```text
projects/<project_id>/scene_bridges/<target_clip_id>/<bridge_revision>/
```

至少包含 `scene_bridge_manifest.json` 和 `camera_track_seed.json`。clips manifest 只保存当前 active bridge 的轻量引用。

以下变化使 bridge stale：

- source video fingerprint 或任一 clip 的 PTS/frame map 改变；
- CAD revision 改变；
- 源片段 active workbench output revision/fingerprint 改变；
- 打通方向、重叠参数或锚点选择规则版本改变。

目标片段存在活动会话、待恢复保存、有效微调结果或已保存 workbench 输出时禁止覆盖。重复 operation_id 必须幂等；失败的媒体准备或路线拟合不得发布 active bridge。

## 后台任务与状态

打通使用现有 ProjectService、expected_revision、operation_id、原子 JSON 和资源队列。组合任务顺序为：

1. 准备或验证目标 solve 媒体与 solve frame map；
2. 准备目标 SfM 输出；
3. 选择两个共同 source PTS 并生成候选 bridge；
4. 使用两个继承锚点调用现有路线拟合；
5. 校验拟合输出后发布 bridge，并把目标状态设置为 `awaiting_route_refinement`。

项目管理显示一条单调组合进度，100% 只代表 bridge 和路线拟合均已验证发布。任务在后台运行，用户可新建项目、上传或操作其他项目。同项目输入变化允许旧进程结束，但结果必须标记 stale，禁止发布。

## UI 与工作台恢复

已完成的 SfM 源片段显示 `向上打通` / `向下打通`。点击后目标片段显示“场景坐标打通中”及真实进度。

自动拟合成功时：

- 用户仍停留在项目管理页：自动打开目标工作台；
- 用户已离开：目标片段显示“待微调”，重新进入后恢复同一 bridge 和拟合路线；
- 工作台直接进入现有路线微调阶段，不重复关键帧标定；
- 用户完成微调后继续现有质量检测、渲染和保存返回流程。

按钮不可用时显示具体原因，例如不同场景、源路线未保存、目标已有成果、源视频不可用或重叠区不足两个有效 PTS。

## 失败与降级

- 共同 PTS 少于两个、间隔不足或任一位姿无效：打通失败并保留目标原状态；
- solve 媒体、SfM 或路线拟合失败：展示对应阶段诊断，可从原 operation 重试；
- 不允许用单锚点、最后位置冻结、跨 lost 区间插值或轨迹外推伪造成功；
- 纯旋转片段不参与 scene bridge；
- 第一版不做跨场景继承和基于视觉相似度的跨片段配准。

## 测试与验收

按 TDD 覆盖：

- core 与 solve PTS 边界、源视频首尾裁剪和 VFR 精确 PTS；
- 上下方向分别选取正确的两个共同 PTS 和源路线位姿；
- 两锚点自动路线拟合后进入待微调，不进入质量检测；
- 刷新、离开和重新进入可恢复同一 bridge；
- 不同场景、纯旋转、目标已有成果及重叠不足时拒绝；
- revision 变化和同项目并发修改阻止旧结果发布；
- rendered.mp4 帧数、render frame map、core source PTS 和 output ordinal 完全不变；
- Stage 8 concat 无重复、无遗漏；
- 旧项目按需准备 solve interval；
- 现有手工关键帧、单边界定位、Pure Rotation、SfM、标签和无标签渲染无回归。
