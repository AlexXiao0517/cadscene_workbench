# HTTP API 与产物参考

这是现行本地工作台使用的 HTTP 与文件约定，供部署、前端和排障使用；它不是面向公网的兼容性承诺，也不以容易过期的提交号代替接口版本。架构与坐标语义见[系统架构](../design/system-architecture.md)和[坐标系与 SfM-CAD 对齐](../design/coordinates-and-alignment.md)。

## 服务与通用约定

用 `python -m cadscene.cli.serve_viewer` 启动服务。JSON 使用 UTF-8；除上传外，POST/PATCH 请求体为 JSON。Project API 的稳定 ID 匹配 `[A-Za-z0-9_.-]+`。不可信文件名、URL 或客户端路径不能直接作为项目路径。

建议显式传入独立 `--storage-root`。当前 Project API 数据位于 `<storage-root>/projects/`；兼容 Workflow API 使用 `data/` 和 `runs/`。同一 `projects/` 根只能由一个 `serve_viewer` 实例持有租约。

常见状态码：

| 状态 | 含义 |
| --- | --- |
| 200/201/202 | 读取成功、资源已创建或异步作业已入队 |
| 400 | 请求字段、文件、坐标系确认或状态前提无效 |
| 403 | workbench token 无权访问项目/片段 |
| 404 | 项目、片段、产物或不可变 revision 不存在 |
| 409 | `expected_revision` 冲突、session replay/stale 或资源/路线冲突 |

## Project API

### 项目、上传与分析

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/projects` | 创建五类空 manifest，返回 `project_id` 和项目页 URL |
| `PATCH /api/projects/<project_id>` | 按 `expected_revision` 更新项目显示名 |
| `GET /api/projects/<project_id>/snapshot` | 返回项目、片段、作业、渲染、标牌和 capability 的一致快照；支持 ETag/no-store |
| `POST /api/projects/<project_id>/uploads/video` | multipart 上传并发布不可变视频与校验报告 |
| `POST /api/projects/<project_id>/uploads/cad` | multipart 上传初始 CAD |
| `POST /api/projects/<project_id>/uploads/srt` | multipart 上传可选 SRT |
| `POST /api/projects/<project_id>/analysis/start` | 建立 CAD/video analysis DAG |
| `POST /api/projects/<project_id>/analysis/activate` | 激活已验证 analysis revision 和片段定义 |
| `POST /api/projects/<project_id>/uploads/cad-replacement` | multipart 上传全局 CAD 候选；HTTP 查询参数要求 `expectedRevision=<当前 revision>&sameCoordinateSystem=1` |

上传先写临时文件，验证扩展名、大小和 SHA-256，再发布到 `assets/`。分析产物位于 `analysis_artifacts/<analysis_revision>/`。前端不能在上传完成前从原始临时路径读取资产。

正式上传界面当前只发送 MP4 视频、DXF 图纸和可选 SRT。Project API 底层校验器为迁移和维护兼容仍接受额外视频/CAD 扩展名，但这些扩展名没有在正式界面开放，不构成用户侧端到端支持承诺。调用方若绕过界面使用兼容格式，必须自行验证解码、CAD 转换、分析、工作台和渲染全链路。

CAD replacement 返回 202 和 `job_id`。候选导入成功前活动 CAD 不变；成功后项目活动 CAD 原子切换，片段/轨迹保持，旧 render/merge owner 记录变为 `stale_input`。相同 SHA-256、无有效工作台输出、未确认坐标系或已有进行中替换都会被拒绝。

### 片段、作业、渲染和合并

| 方法与路径 | 作用 |
| --- | --- |
| `PATCH /api/projects/<project_id>/clips/<clip_id>/workflow` | 保存人工工作流覆盖 |
| `PATCH /api/projects/<project_id>/clips/<clip_id>/name` | 更新片段显示名 |
| `POST /api/projects/<project_id>/trajectory-jobs` | 对一个或多个片段建立轨迹作业和依赖 |
| `POST /api/projects/<project_id>/render-jobs` | 对一个或多个片段建立渲染作业 |
| `POST /api/projects/<project_id>/merge-jobs` | 由全部当前片段 render revision 建立严格 concat 作业 |
| `GET /api/projects/<project_id>/jobs/<job_id>/runtime` | 返回阶段、真实进度、尝试、日志和错误 |
| `POST /api/projects/<project_id>/jobs/<job_id>/retry` | 为可重试作业创建下一 attempt |
| `POST /api/projects/<project_id>/jobs/<job_id>/cancel` | 取消当前项目队列中的作业 |
| `GET /api/projects/<project_id>/clips/<clip_id>/renders/<render_revision>/video` | 读取已发布片段视频 |
| `GET /api/projects/<project_id>/merge-output/video` | 读取当前已发布合并视频 |

批量 API 接收 snapshot 中当前 jobs/project revision，服务端按 exclusive key、依赖 DAG、输入指纹和幂等键去重。`pending` 只表示等待依赖或资源槽；100% 只在验证和 manifest 发布完成后出现。

视频分析会写入 `recommended_workflow`。激活候选 analysis revision 时，如不存在人工 `workflow_override`，`resolved_workflow` 直接采用推荐值；项目片段管理的最终工作流选择器通过上述 PATCH 保存覆盖。纯旋转推荐来自自动源视频证据，不依赖上传页复选框。

成功 render revision 必须同时拥有视频、frame map、owner record、输入指纹和 validation proof。文件存在但 owner 不匹配不能作为成功结果。合并同样验证每段 revision、frame identity、项目媒体契约和发布 operation。

### Workbench session

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/projects/<project_id>/clips/<clip_id>/workbench-sessions` | 创建或恢复片段编辑会话，返回带 token 的 viewer URL |
| `GET /api/projects/<project_id>/workbench-sessions/<token>` | 检查 session 身份、阶段和恢复信息 |
| `POST .../<token>/heartbeat` | 延长活动编辑会话 |
| `POST .../<token>/trajectory-ready` | 把项目轨迹作业输出接入当前 session |
| `POST .../<token>/save` | 校验并发布不可变 `workbench_output_revision` |
| `POST .../<token>/close` | 关闭临时编辑授权 |

token 同时绑定项目和片段，不是持久结果。刷新后如 token 失效，应从项目页重新创建/恢复 session。服务端只接受 session 允许目录中的工件，并为 workbench output manifest、工件路径、SHA-256 和 operation ID 建立校验引用。

### Annotation 与工程标牌

| 方法与路径 | 作用 |
| --- | --- |
| `POST /api/projects/<project_id>/annotations` | 创建 `cad_anchor` 或兼容 `video_track` annotation |
| `PATCH /api/projects/<project_id>/annotations/<annotation_id>` | 按 annotation manifest revision 更新内容、样式、偏移、范围或可见性 |
| `DELETE /api/projects/<project_id>/annotations/<annotation_id>` | 删除 annotation；不删除历史 tracking revision |
| `POST /api/projects/<project_id>/annotations/<annotation_id>/track` | 生成新的 immutable tracking revision（当前 UI 不开放创建入口） |
| `GET /api/projects/<project_id>/annotations/<annotation_id>/tracking` | 读取 active tracking revision 的精确 PTS 结果 |
| `GET /api/projects/<project_id>/clips/<clip_id>/annotation-preview` | 返回浏览器把 currentTime 映射到权威 source PTS 所需的 timing |

Annotation 至少包含 `annotation_id`、`clip_id`、`anchor_type`、`content.title`、`content.body`、`panel`、`leader`、`style`、source PTS 范围、`screen_offset`、visibility policy、annotation revision 和 active tracking revision。旧 `text` 字段读取时兼容为 body。

标题、正文、样式和 screen offset 修改只使对应片段 render stale，不触发轨迹或 video tracking。`cad_anchor` 不产生 tracking revision。`video_track` 的 ROI/re-anchor 才生成新 revision；lost 帧要求 `visibility=false` 且位置为空，不冻结、不跨区间插值。

## 五类 manifest 与原子更新

```text
<storage-root>/projects/<project_id>/
  project_manifest.json
  clips_manifest.json
  jobs_manifest.json
  render_manifest.json
  annotations_manifest.json
```

每个 manifest 都包含 `manifest_owner`、`schema_version`、`revision`、`updated_at`、`project_id` 和可选 `operation_id/operation_intent`。单 owner 写入先生成同目录临时文件、flush/fsync，再 `os.replace`。跨 owner publication 保存 participants、base revisions 和 candidate，启动恢复按 operation ID 验证并修复前缀。

不要把“文件 JSON 可解析”等同于“领域状态有效”。成功作业必须与 owner record、输出 revision、fingerprint 和 validation proof 完整对应。

## 当前项目产物

```text
<storage-root>/projects/<project_id>/
  assets/
    <asset>-<sha256>.<ext>
    <asset>.validation.json
    validation_attempts/
  analysis_artifacts/<analysis_revision>/
  jobs/<job_id>/attempt-<number>/
  workbench_sessions/<token-hash>.json
  workbench_outputs/<workbench_revision>/
    workbench_output_manifest.json
    artifacts/
  annotations/<clip_id>/<annotation_id>/<tracking_revision>/
    tracking_results.json
  render_outputs/<clip_id>/<render_revision>/
    rendered.mp4
    render_frame_map.json
    render_output_manifest.json
  thumbnails/
```

Job attempt 是可变执行现场；`analysis_artifacts`、`workbench_outputs`、tracking revision 和 render revision 一旦发布即不可变。重试创建新 attempt/revision，不覆盖已发布证据。

## source PTS 与 frame map

项目视频分析用 source decoded-frame integer PTS 和精确 `time_base` 定义片段半开区间。片段导出、标牌、渲染和 concat 必须沿用同一帧身份。

`render_frame_map.json` 至少证明每个 output ordinal 对应的 source PTS。渲染验证要求：

- 输出帧数与权威 source frames 数量一致；
- source PTS 严格按片段定义出现；
- output ordinal 连续且不重复；
- 标牌叠加不增删帧；
- concat 后每个项目 source frame 恰好出现一次。

浏览器可用 `video.currentTime` 查找邻近 PTS 做预览，但正式渲染禁止 `currentTime × fps`、固定 FPS index 或 clip-local float time 反推源帧。

## 启动恢复

服务启动会扫描含 `project_manifest.json` 的项目，修复跨 manifest publication，恢复持久队列，校验成功 render revision，并恢复项目媒体契约。无法验证仍存活的旧进程会被标为 interrupted，不能伪装 running。

CAD 替换后，旧分析 job 的 CAD identity 与当前活动 CAD 不同。服务只在 `source_assets._analysis_revisions[analysis-<video_job_id>].input_snapshot` 的 request key、视频/CAD 路径和 SHA-256 与 job fingerprint/idempotency 精确匹配时，重绑到当前项目契约。缺失或不匹配时 fail closed，并报告恢复错误；不要手工把 fingerprint 改成当前值。

## 兼容 Workflow API 与 `data/runs`

`/api/workflow/*` 和 `/api/pure-rotation/*` 仍支持既有单 dataset/run 工作台阶段。其主要端点包括创建/上传 dataset、`run-stage`、job status/log、camera track、keyframe plan，以及 pure-rotation run/placement/corrections/trajectory。

`srt_sfm_fused` 和 `srt_full_pose` 仍是 Interface only，`run-stage` 会拒绝。兼容 JobRunner 的活动外部子进程不能在服务重启后重新接管；这与当前 ProjectRuntime 能恢复 manifest/队列状态不是同一语义。

旧目录：

```text
<storage-root>/data/<dataset>/dataset_manifest.json
<storage-root>/runs/<dataset>/<runId>/
  job_status.json
  job_process.json
  manifest.json
  01_keyframes/
  02_sfm/
  03_alignment/
  04_quality/
  08_render/
```

它们可能被当前 workbench adapter 引用，但不是当前项目 manifest 的替代品。备份/迁移项目时必须保留项目 manifest 所引用的兼容工件，不能只复制 `projects/<project_id>` 中的 JSON。

## 根目录与路径安全

- `--storage-root` 必须预先存在；省略时继承 `--root`，会把 `projects/` 写入代码根。
- `--extra-root NAME=PATH` 是只读旧数据挂载，不能成为项目写入根；不要使用保留名 `data` 或 `runs`。
- 服务校验 project/dataset/run/clip/revision ID 和 ZIP 成员，拒绝 `..`、绝对路径及根逃逸。
- 不要公开绑定服务；Project API 和 workbench token 不是公网认证系统。
- 删除、移动或手工修复数据前，先停止指向该 storage root 的唯一服务并完整备份根目录。
