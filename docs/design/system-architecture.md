# 系统架构与工作流

本文描述当前 `main@a53fbd1` 已实现的运行架构。历史 `data/<dataset>`、`runs/<dataset>/<runId>` 工作流仍用于兼容单片段管线，但新的人工项目、任务恢复、标牌、渲染和合并由 `cadscene.projects` 项目领域管理。

## 组成与职责

| 组成 | 职责 | 运行边界 |
| --- | --- | --- |
| 上传门户 | 创建项目，上传视频、CAD 和可选 SRT，显示分析进度并进入项目页。 | `apps/workflow_portal/`；不直接执行片段轨迹。 |
| 项目片段管理 | 展示项目资产、视频分析片段、推荐/最终工作流、任务进度、CAD 替换、批量轨迹/渲染和合并。 | `apps/project_workspace/`；以 Project API snapshot 为权威。 |
| Web Camera Viewer | 执行单片段 SfM/纯旋转、关键帧、质量、工程标牌、预览和渲染交互。 | `apps/web_camera_viewer/`；只有项目签发的工作台 session 拥有写权限。 |
| HTTP 服务 | 提供静态文件、Range 视频、兼容 Workflow API 和当前 Project API。 | `python -m cadscene.cli.serve_viewer` 是唯一服务入口。 |
| 项目领域 | 管理项目、片段、作业、渲染和标牌五类 manifest，以及 revision、operation ID 和跨 manifest 恢复。 | `cadscene/projects`、`cadscene/annotations`。 |
| 本地资源队列 | 按 heavy compute、light compute、media I/O 和 control 资源类调度分析、轨迹、渲染、合并与 CAD 替换。 | 同一 `projects/` 根只能有一个活动服务租约。 |
| 兼容工作流 | 运行 SfM、alignment、quality、render 和 pure-rotation 的既有阶段命令。 | 产物作为工作台或项目作业的不可变输入/输出被项目层封装。 |

## 从上传到交付

```mermaid
stateDiagram-v2
    [*] --> "上传项目资产"
    "上传项目资产" --> "异步视频/CAD 分析"
    "异步视频/CAD 分析" --> "项目片段管理"
    "项目片段管理" --> "片段轨迹任务"
    "片段轨迹任务" --> "工作台编辑"
    "工作台编辑" --> "SfM 关键帧与质量": "sfm_only"
    "工作台编辑" --> "旋转放置与校正": "pure_rotation"
    "SfM 关键帧与质量" --> "保存工作台输出"
    "旋转放置与校正" --> "保存工作台输出"
    "保存工作台输出" --> "工程标牌与渲染"
    "工程标牌与渲染" --> "片段不可变渲染"
    "片段不可变渲染" --> "严格帧分区合并"
    "严格帧分区合并" --> [*]
```

上传端先保存带 SHA-256 和校验报告的不可变视频/CAD，再建立 CAD 分析和视频分析 DAG。视频分析产出 source PTS 半开区间片段；激活 analysis revision 后，`clips_manifest.json` 保存稳定 `clip_id` 和每段推荐/覆盖/最终工作流。

片段轨迹可以在项目页批量排队，也可以由工作台触发。工作台 session 是临时写权限，刷新后旧 token 可能失效；`workbench_outputs/<revision>/` 才是持久、不可变且可校验的结果。再次进入片段时，项目服务从有效工作台输出恢复阶段，而不是信任旧 URL 参数。

## 项目状态与任务队列

每个项目有五个独立 owner：

| Manifest | 权威内容 |
| --- | --- |
| `project_manifest.json` | 源资产、媒体契约、分析 revision、CAD 版本和项目状态 |
| `clips_manifest.json` | 激活 analysis revision 的片段定义、工作流和工作台引用 |
| `jobs_manifest.json` | 队列作业、顺序、输入指纹、尝试、进度、校验和发布状态 |
| `render_manifest.json` | 片段渲染、合并计划和已发布输出 |
| `annotations_manifest.json` | 独立标牌领域、annotation revision 和 active tracking revision |

写入使用 `expected_revision` 做乐观并发控制。涉及多个 manifest 的操作携带 `operation_id` 和 operation intent，按固定顺序原子替换单个 JSON；服务重启时 `reconcile_project` 可以补齐已发布前缀或恢复原值。前端遇到 revision conflict 必须刷新 snapshot 后重试，不能覆盖服务端当前 revision。

队列百分比来自真实阶段：queued/preparing/running/validating/publishing/success。预览和汇总不代表作业已完成；只有产物校验及 owner manifest 发布成功后才显示 100%。同一项目根的 `.serve_viewer.lease` 阻止两个服务并发消费同一队列。

## source PTS、渲染与合并

视频分析、标牌和正式渲染以 source decoded-frame integer PTS、精确 `time_base` 和半开区间为权威。浏览器的 `video.currentTime` 只用于选择邻近权威 PTS，不能用 `currentTime × fps` 或固定 FPS 帧号生成正式帧身份。

每个成功片段渲染发布：

    render_outputs/<clip_id>/<render_revision>/
      rendered.mp4
      render_frame_map.json
      render_output_manifest.json

`render_frame_map.json` 将 output ordinal 映射到原视频 source PTS。标牌叠加不能改变帧数、source PTS 顺序或 output ordinal。没有活动标牌时渲染适配器走原快速路径；存在标牌时，先生成基础叠加视频，再用透明 RGBA 工程标牌层烧录，最后按同一权威 frame map 打包。

Stage 8 合并只接受完整、当前且经验证的片段 render revision。它检查所有 source PTS 分区无重叠、无缺口，必要时按项目媒体契约归一化，再拼接视频并从原视频裁剪/复用音频。任何片段渲染 stale 都会使旧合并计划和发布输出 stale。

## CAD 文字与工程标牌

DXF 导入器保留 `TEXT`、`MTEXT` 和块属性文字的内容、图层、位置、旋转、字号及 CAD 坐标。查看器以可见性、距离和总量预算管理文字对象，避免大型 CAD 明显拖慢相机交互。

Stage 9 标牌是独立 annotation 领域，不写入 clip/job manifest。当前界面只开放 `cad_anchor`：

- 保存 CAD world XYZ、可选 entity reference、source PTS 范围和 `screen_offset`；
- 使用当前有效 Base/Corrected CameraState 和现有投影数学得到视频锚点；
- behind-camera、出画、轨迹无效或超出 PTS 范围时隐藏卡片、引线和锚点；
- 屏幕卡片保持水平可读，引线连接卡片最近边缘与圆形锚点；
- 修改标题、正文、样式或偏移只提升 annotation revision，并使对应片段 render stale。

`video_track` 的 OpenCV LK 跟踪、immutable tracking revision 和 lost 语义仍保留用于测试与历史数据兼容；当前 UI 隐藏创建入口。它不应被现行用户文档描述为可交付能力。

## 全局 CAD 替换

CAD 替换只有在至少一个片段存在可校验的保存工作台输出时才 eligible，因为该输出证明项目坐标系已经打通。用户必须显式确认新版 CAD 与当前项目使用相同坐标系。

新版图纸先作为候选上传，由 `cad_replacement` light-compute 作业导入和校验。发布成功前活动 CAD 不变；成功时：

- 原 CAD 与新版 CAD 都进入 `_cad_versions` 历史；
- 项目活动 CAD 原子切换到新版数据集；
- clips、分析 revision、轨迹和工作台输出保持有效；
- 片段 render、merge plan 和 published output 变为 `stale_input`；
- 用户重新进入工作台检查/微调后重新渲染。

失败只更新替换状态和错误，旧 CAD 继续活动。如果坐标系、单位或原点发生变化，必须新建项目重新标定，不能使用此快捷路径。

## 存储布局

`--root` 指向静态应用根，默认是仓库根。`--storage-root` 指向可写数据根；省略时继承 `--root`。当前项目主目录为：

```text
<storage-root>/
  projects/
    .serve_viewer.lease
    <project_id>/
      project_manifest.json
      clips_manifest.json
      jobs_manifest.json
      render_manifest.json
      annotations_manifest.json
      assets/
      analysis_artifacts/
      jobs/<job_id>/attempt-<n>/
      workbench_sessions/
      workbench_outputs/<revision>/
      annotations/<clip_id>/<annotation_id>/<tracking_revision>/
      render_outputs/<clip_id>/<render_revision>/
      thumbnails/
  data/                              # 兼容 dataset 输入
  runs/                              # 兼容 dataset/run 阶段产物
```

项目 manifest、不可变 revision 和它们引用的文件必须作为一个整体备份。不要只复制活动 JSON、手工改绝对路径或移动单个输出 revision。

## 启动恢复与失败边界

服务启动时逐项目执行：

1. 修复跨 manifest 操作的可恢复前缀；
2. 读取持久 `jobs_manifest.json`，将无法验证仍在运行的旧进程标记为 interrupted；
3. 校验成功 render revision 的 owner、输入指纹、视频和 frame map；
4. 恢复可安全继续的 queued 作业，并同步分析/CAD 替换状态；
5. 恢复或建立项目媒体契约；旧项目缺少媒体契约时仍可读，但渲染 preflight 会明确报错。

全局 CAD 替换后，保存的分析作业仍绑定替换前 CAD 身份。恢复逻辑先用 `source_assets._analysis_revisions[...].input_snapshot` 精确验证原始视频/CAD/request key，再把已验证作业重绑到当前项目契约。该兼容只接受完整快照和精确 fingerprint/idempotency 配对；不匹配时 fail closed，绝不伪造成功或自动重跑分析。

兼容 `data/runs` JobRunner 的活动子进程不能在服务重启后重新接管。项目队列可以恢复持久状态，但不会假装已中断的外部进程仍在运行。排障时应通过 Project API runtime、作业尝试日志和不可变发布结果判断，而不是手工修改 status。

## 运行依赖边界

默认 SfM 是 `pycolmap + cpu`。CUDA 只覆盖已确认支持的特征提取和匹配，无法确认时回退 CPU；mapper 和 global BA 不宣传为 GPU。`pure_rotation` 依赖外部 OpenGV。DXF 依赖解析器，DWG 依赖外部转换器。普通 SRT 只提供能力线索，不能作为 CAD 高程或高精度位姿真值。
