# HTTP API 与产物参考

这是本地工作台当前使用的 HTTP 与文件约定，供部署、调试和前端维护使用；它不是面向公网的稳定服务承诺。设计和坐标语义请先阅读[系统架构](../design/system-architecture.md)与[坐标系与 SfM-CAD 对齐](../design/coordinates-and-alignment.md)。

## 服务与通用约定

用 `python -m cadscene.cli.serve_viewer` 启动服务。JSON API 使用 UTF-8。数据集 manifest、SRT 分析和 JobRunner/工作流状态等由各自的临时文件加 `os.replace` 写入；上传文件也先写 `.upload` 临时文件再替换。不要把此保证扩展到所有产物：通用 `core.io.write_json` 直接写目标文件，许多阶段产物和 run manifest 因而不是原子写入。除上传接口外，POST 请求体是 JSON。成功响应通常含有 `ok: true`；无效参数为 400，缺少文件为 404，正在运行的同一 `dataset + runId` 或 Interface-only SRT 路线冲突为 409。

`dataset` 会被规范化为小写 ASCII slug（最多 80 个字符）；`runId` 必须匹配 `[A-Za-z0-9_.-]+`。不要把 API 接收到的路径、URL 或客户端文件名当成可穿越目录的路径。

## 工作流 HTTP 端点

| 方法与路径 | 输入 | 作用 |
| --- | --- | --- |
| `POST /api/workflow/create-dataset` | JSON：`dataset`，可选 `cadScale`、`originX`、`originY`、`hoveringDeclared`、`runId` | 创建或更新数据集并返回 `dataset_manifest.json` 内容 |
| `POST /api/workflow/upload-video` | 查询 `dataset`，可选 `runId`；`multipart/form-data`，字段名必须为 `file` | 保存 `.mp4/.mov/.avi/.mkv` 视频并更新 manifest |
| `POST /api/workflow/upload-cad` | 同上 | 导入 `design.json`、`.dxf`、`.dwg` 或 CAD 资产 `.zip` |
| `POST /api/workflow/upload-srt` | 同上 | 保存 `.srt`，分析并返回 SRT 路由/能力摘要 |
| `GET /api/workflow/list-datasets` | 无 | 返回存储根下的数据集摘要 |
| `GET /api/workflow/dataset-manifest?dataset=…` | `dataset` | 返回数据集 manifest |
| `GET /api/workflow/srt-analysis?dataset=…` | `dataset` | 返回分析结果、`trajectory_mode` 和提示 |
| `POST /api/workflow/run-stage` | JSON：`dataset`、`runId`、`stage`、可选 `options` | 启动 `sfm`、`alignment`、`quality`、`render` 或底层支持的 `pure_rotation` 后台阶段；纯旋转应优先使用专用的 `POST /api/pure-rotation/run` |
| `POST /api/workflow/cancel` | JSON：`dataset`、`runId` | 取消当前后台进程 |
| `POST /api/workflow/job-status` | JSON：`dataset`、`runId`、`stage`，可选 `status`、`progress`、`message`、`error` | 写入工作流槽位状态，主要用于服务内部/调试 |
| `GET /api/workflow/job-log?dataset=…&runId=…&stage=…&tail=200` | `dataset`、`runId`、`stage`；`tail` 限制为 1–2000 | 返回阶段日志尾部 |
| `POST /api/workflow/save-camera-track` | JSON：`dataset`、`runId`、对象 `cameraTrack` | 保存人工关键帧轨迹 |
| `POST /api/workflow/generate-keyframe-plan` | JSON：`dataset`、`runId`，可选 `intervalFrames` | 初始对齐后生成关键帧计划 |
| `GET /api/workflow/keyframe-plan?dataset=…&runId=…` | `dataset`、`runId` | 读取关键帧计划 |
| `POST /api/workflow/ignore-suggestion` | JSON：`dataset`、`runId`、`frame_index`，可选 `reason` | 记录用户确认无需补帧的建议 |
| `GET /api/workflow/sfm-camera-init?dataset=…&runId=…` | `dataset`、`runId` | 从 SfM 轨迹读取相机初始化信息 |

`run-stage` 会拒绝 `srt_sfm_fused` 和 `srt_full_pose`：它们只有上传、检测和界面提示，尚不能由 JobRunner 端到端执行。它在底层接受 `pure_rotation` stage，但门户和客户端应使用 `POST /api/pure-rotation/run`，以便在启动前执行该实验路线的专用 manifest 路由检查。

## pure-rotation 端点

| 方法与路径 | 输入 | 作用 |
| --- | --- | --- |
| `POST /api/pure-rotation/run` | JSON：`dataset`、`runId`，可选 `options` | 只在 manifest 路由为 `pure_rotation` 时启动外部后端 |
| `POST /api/pure-rotation/placement` | JSON：`dataset`、`runId`、`placement` | 保存固定相机中心的全局放置并生成基础轨迹 |
| `POST /api/pure-rotation/corrections` | JSON：`dataset`、`runId`、数组 `corrections` | 写入局部姿态校正并生成校正轨迹 |
| `GET /api/pure-rotation/status?dataset=…&runId=…` | `dataset`、`runId` | 返回后端摘要、全局放置和校正列表 |
| `GET /api/pure-rotation/trajectory?dataset=…&runId=…&kind=raw` | `kind` 为 `raw`、`base` 或 `corrected` | 返回对应轨迹 JSON |

这是 Experimental 工作流：相机中心固定，结果不恢复平移或尺度，也不会自动判断视频是否为纯旋转。

## 数据集 manifest 与输入存储

`<storage-root>/data/<dataset>/dataset_manifest.json` 是数据集的入口。主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `dataset`、`created_at`、`updated_at`、`status` | 数据集身份和总体状态；`ready` 需要视频与可用 CAD，CAD 失败时为 `failed` |
| `video` | 原始文件名、相对路径和查看器 URL 等视频记录 |
| `cad` | `status`、`source_type`、原始/转换文件、`design_json`、URL、边界框及道路中心线能力 |
| `defaults` | `cad_scale` 与 `origin_xy`；应与人工关键帧和管线参数一起审计 |
| `srt` | 原始 SRT、分析 JSON/报告、覆盖率、姿态来源和警告 |
| `workflow` | `trajectory_mode`、实现状态、用户声明的运动模式与可观测性元数据 |
| `warnings` | 导入、转换或能力检测警告 |

典型输入目录如下（具体文件名以上传文件名为准）：

```text
<storage-root>/data/<dataset>/
  dataset_manifest.json
  <uploaded-video>
  raw_cad/<uploaded.dxf-or-dwg>
  design.json
  telemetry/<uploaded.srt>
  srt_analysis.json
  srt_analysis_report.md
```

DXF 会解析为 `design.json`；DWG 先保存原件，再依赖外部 DWG→DXF 转换器。转换器缺失时可能得到 `cad.status: raw_saved`，这不代表后续阶段已有可用 CAD。CAD `.zip` 只在其中包含 `design.json` 时可直接标记为 ready。

## 运行目录与阶段产物

每次执行位于 `<storage-root>/runs/<dataset>/<runId>/`。`manifest.json` 是 run manifest，记录每个阶段的命令、输入、输出、指标、状态和时间；它与数据集的 `dataset_manifest.json` 不同。`job_status.json` 提供面向界面的五个槽位（`upload`、`sfm`、`keyframes`、`quality`、`render`），`job_process.json` 保存当前或最近一次后台子进程的 PID、命令、返回码和日志路径。

```text
<storage-root>/runs/<dataset>/<runId>/
  00_inputs/                         # 已解析的数据集、管线与覆盖项
  01_keyframes/                      # camera_track_manual.json、keyframe_plan.json
  02_sfm/                            # camera_trajectory.json、sparse_points.ply、sfm_stats.json
  03_alignment/                      # alignment.json、sfm_camera_path.csv、对应关系与报告
  04_quality/                        # 时间线、建议、质量化轨迹和报告
  05_viewer_scene/                   # sfm_viewer_scene.json、统计和报告
  06_road_surface/                   # 几何/道路诊断及可选查看器诊断场景
  08_render/                         # sfm_align_overlay.mp4、渲染统计和报告
  02_pure_rotation/                  # 原始旋转轨迹与后端摘要（实验性）
  03_pure_rotation_placement/        # 全局放置与基础固定中心轨迹
  04_pure_rotation_corrections/      # 局部校正与校正后轨迹
  logs/workflow/<stage>.log
  reports/run_summary.md
  job_status.json
  job_process.json
  manifest.json
```

道路中心线不可用时，`run_pipeline` 会把 `road_surface` 作为 `skipped` 记录到 run manifest；这不是渲染或对齐成功的替代证明。质量阶段可在没有 matplotlib 时跳过 PNG 图表，同时在报告中记录警告。

## 根目录、静态文件和路径安全

- `--root` 提供静态站点；未传 `--storage-root` 时，`--root` 也就是可写的 workflow 根。传入 `--storage-root` 时，服务要求该目录预先存在，并在根分离时把它的 `data/` 与 `runs/` 映射到 `/data/` 与 `/runs/`。
- `--extra-root NAME=PATH` 是必须预先存在的只读挂载，用于旧数据兼容。程序仅在根分离时拒绝 `data`、`runs` 这两个名称；文档约定始终不要使用它们，避免与 workflow 路径混淆。静态路径和 ZIP 成员都会解析并验证仍在允许根目录内。
- 服务器校验数据集和 run ID，运行目录不能逃离 `runs/`；`ArtifactManager` 也只允许把阶段产物写进当前 run 目录。
- 服务重启不会接管、续跑或重试旧子进程。恢复时先检查 `job_process.json`、`job_status.json`、`manifest.json` 和阶段日志；优先使用新的 `runId` 重跑以保留原始证据。若确需在原 run 内重跑，先完整备份整个 run 目录：同名阶段产物、`manifest.json`、`job_status.json` 和 `job_process.json` 都可能被覆盖。
- 服务重启后如考虑调用取消接口，先核对 `job_process.json` 的 PID 当前进程命令是否与 `job_process.command` 一致；无法确认一致性时不要取消该 PID，改用新的 `runId` 重新运行。
