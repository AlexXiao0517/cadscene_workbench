# 系统架构与工作流

本文说明当前代码已经实现的组成和边界。它不把门户上能够显示的选项等同于可以执行的工作流。

## 组成与职责

| 组成 | 职责 | 运行边界 |
| --- | --- | --- |
| 工作流门户 | 创建数据集，上传视频、CAD 和可选 SRT，显示检测到的路线后进入查看器。 | 静态页面位于 `apps/workflow_portal/`，由本地 HTTP 服务提供。 |
| Web Camera Viewer | 展示视频、CAD、关键帧、质量提示和可下载产物；驱动阶段按钮。 | 静态页面位于 `apps/web_camera_viewer/`；通过同一 HTTP 服务读取工作流 API 与文件。 |
| HTTP 服务 | 提供静态文件、Range 视频读取、上传、清单、状态、日志和阶段启动 API。 | `python -m cadscene.cli.serve_viewer` 是本地入口。 |
| JobRunner | 以后台子进程运行 `sfm`、`alignment`、`quality`、`render` 或 `pure_rotation`，并保存状态和日志。 | 一个正在运行的 `dataset + runId` 组合不能重复启动。 |
| 数据导入 | 保存输入，解析 CAD，分析 SRT，并更新数据集清单。 | SRT 分析只决定接口路由，不代替 SfM-CAD 对齐。 |
| 对齐与质量模块 | 用人工/已确认关键帧将 SfM 轨迹与 CAD 联系起来，输出质量诊断与建议帧。 | 算法预测关键帧不会被当作新的对齐锚点。 |

## 从上传到结果的状态

门户先创建数据集，再上传视频、CAD 和可选 SRT。数据集只有在视频和可用 CAD 都已准备好时才是 `ready`；DXF 解析失败会标记失败，DWG 在缺少外部转换器时可以仅保存原文件而不会成为可运行 CAD。

```mermaid
stateDiagram-v2
    [*] --> "上传数据"
    "上传数据" --> "SfM 重建": "sfm_only / Stable"
    "SfM 重建" --> "关键帧标定"
    "关键帧标定" --> "路线拟合": "至少两个人工关键帧"
    "路线拟合" --> "质量检测"
    "质量检测" --> "渲染导出"
    "质量检测" --> "关键帧标定": "需要补帧或复核"
    "上传数据" --> "OpenGV 旋转恢复": "pure_rotation / Experimental"
    "OpenGV 旋转恢复" --> "人工全局放置"
    "人工全局放置" --> "局部姿态关键帧校正"
    "局部姿态关键帧校正" --> "渲染导出": "跳过 alignment 与 quality"
    "上传数据" --> "接口提示": "SRT 路由"
    "接口提示" --> [*]: "Interface only：不启动 JobRunner"
```

界面进度使用 `upload`、`sfm`、`keyframes`、`quality`、`render` 五个槽位。实际 `alignment` 任务写入 `keyframes` 槽位，`pure_rotation` 任务写入 `sfm` 槽位；因此状态标签是面向操作的汇总，不能据此推断内部阶段名称一一对应。路线拟合、质量和渲染会先检查前置产物；其中路线拟合至少需要两个非算法预测的人工关键帧，且常规 3D 路径要求 SfM 结果适合三维重建。

纯旋转分支不进入标准路线拟合 `alignment` 或 `quality`。OpenGV 旋转恢复完成后，用户在同一个 `keyframes` 界面先保存固定相机的人工全局放置，再添加局部姿态关键帧校正，随后直接进入渲染。`workflow.js` 会隐藏 quality 步骤和质量时间线；纯旋转流程若请求显示 `quality`，会转到 `render`。这不是“质量检测通过”，而是该实验性流程没有运行标准 quality 阶段。

## 路由模式与可执行性

| 检测结果 | 状态 | 当前行为 |
| --- | --- | --- |
| `sfm_only` | Stable | 默认的可运行流程：SfM、人工关键帧、路线拟合、质量与渲染。 |
| `pure_rotation` | Experimental | 仅在用户声明悬停旋转且没有 SRT 时选择；可运行外部 OpenGV 旋转恢复与后续人工放置/校正，但相机中心固定，不恢复平移或尺度。系统不会自动判定运动类型。 |
| partial-SRT core | Experimental CLI | 独立 CLI 可做 PTS 时间同步、局部 ENU 和稳健 Sim3 融合；它尚未接入 JobRunner。 |
| `srt_sfm_fused` portal route | Interface only | 上传与检测会成功，查看器显示提示；HTTP 服务拒绝启动阶段。 |
| `srt_full_pose` | Interface only | 同样只完成上传、分析和提示，不存在端到端任务。 |

普通 SRT 只提供元数据能力线索，不能作为高精度位置、姿态或 CAD 高程真值。若上传 SRT 后门户显示 Interface only，应新建不上传 SRT 的项目以走稳定的 `sfm_only` 路径，或由维护人员评估实验性 CLI；不要把该提示当成可执行融合。

## 静态根、存储根和依赖

`--root` 指定静态网站根（默认是项目根）；`--storage-root` 指定工作流数据和产物根（默认继承 `--root`）。两者分离时，服务将存储根的 `data/` 与 `runs/` 以 `/data/`、`/runs/` 提供给浏览器；这两个名称不能再被 `--extra-root` 占用。额外根只读挂载给浏览器，用于临时兼容旧数据，不能改变工作流写入位置。

```text
<storage-root>/
  data/<dataset>/dataset_manifest.json  # 视频、CAD、SRT 与导入/检测摘要
  runs/<dataset>/<runId>/               # 每次执行的阶段产物、状态和日志
    job_status.json
    job_process.json
    logs/workflow/<stage>.log
```

服务会校验数据集和 `runId` 的安全名称，并拒绝逃离 `data/` 或 `runs/` 的路径。上传、清单与状态 JSON 采用临时文件后替换的写法，避免把半写入 JSON 当成正常结果。

运行时依赖的边界同样重要：默认 SfM 是 `pycolmap + cpu`；CUDA 是 Optional，只覆盖已支持的特征提取和匹配，无法确认能力时会回退 CPU 并把原因写入状态/报告。`pure_rotation` 依赖外部 OpenGV 后端。DXF 导入和 DWG 转换分别受解析器及部署的外部转换器限制；DWG 原文件被保存并不表示 CAD 已可用于后续阶段。

## 失败与恢复

- 每个后台任务都记录 `job_process.json`、`job_status.json` 和阶段日志。子进程失败时，状态保存返回码、最后日志行和日志位置；取消会写入 `cancelled` 状态。
- JobRunner 的正在运行进程表只存在服务内存中。服务重启后不会自动重新接管、续跑或重试旧子进程；持久化状态和日志用于判断已完成产物与失败原因。
- 恢复时先查看相同 `dataset + runId` 的状态、日志及已有阶段产物，修正输入、依赖或关键帧后从需要的阶段重新运行。不要把缺失产物或 Interface only 路由强行视为可恢复的成功任务。
- 如果查看器缺少媒体或产物，先核对 `--root`、`--storage-root`、`dataset` 与 `runId` 是否指向同一部署；旧数据需要显式只读挂载或显式 URL，而不是依赖已不存在的默认路径。
