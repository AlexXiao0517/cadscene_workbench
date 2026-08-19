# 故障排查

本页按“症状 → 可能原因 → 检查 → 处理”组织。当前项目管线先保留 `project_id`、`clip_id`、job ID、`GET /api/projects/<project_id>/snapshot`、job runtime 和对应 attempt 日志；兼容 dataset/run 工作流再保留 `job_status.json`、`job_process.json` 与 `logs/workflow/<stage>.log`。ProjectRuntime 会恢复可验证的持久队列状态，但不会假装已中断的外部子进程仍在运行。产物和字段见[HTTP API 与产物参考](api-and-artifacts.md)。

## 项目加载、会话与启动恢复

### 重启后旧项目找不到、全部待处理或显示结果已失效

**可能原因：** 服务使用了不同的 `--storage-root`；打开了旧 `project_id`；项目 manifest 引用的兼容 `data/runs` 工件被移动；不可变 workbench/render 输出校验失败；或 CAD 替换后的旧分析身份恢复失败。

**检查：** 核对实际启动命令，确认 `<storage-root>/projects/<project_id>/project_manifest.json`、`clips_manifest.json`、`jobs_manifest.json`、`render_manifest.json` 均存在。请求 `GET /api/projects/<project_id>/snapshot`，检查服务启动 stderr 中是否包含 lease、manifest reconciliation、media spec、render validation 或 `legacy analysis identity` 错误。不要只看浏览器旧 URL。

**处理：** 用项目创建时相同的 storage root 重启，并从 `/apps/project_workspace/?projectId=<project_id>` 进入。若 manifest 引用了旧 `data/runs` 产物，恢复完整原目录或从备份恢复，不要只改绝对路径。CAD 已合法替换但出现 `legacy analysis identity does not match exactly` 时，应使用包含 CAD 替换恢复修复的版本；修复会用 `_analysis_revisions.*.input_snapshot` 精确校验，不应重跑分析或手工修改 fingerprint。

### 刷新后提示“项目工作台会话已失效”“clip has no matching session”或已有 active session

**可能原因：** 带 token 的工作台 URL 已过期、被关闭、属于另一片段，或旧标签页仍持有活动编辑 session。session 是临时写权限，不是持久工作台输出。

**检查：** 返回项目片段管理查看该片段 capability；用 session inspect API 核对 token 的 project/clip/status。确认 `workbench_outputs/<revision>/workbench_output_manifest.json` 是否已经保存成功。

**处理：** 关闭旧工作台标签页，从同一项目页重新点击“进入工作台”，让服务恢复或签发新 session。不要复制旧 token、删除 `workbench_sessions/*.json` 或把 session 状态当成已保存结果。只有服务确认旧 session 已关闭/过期后才创建新编辑会话。

## 项目队列与进度

### 任务一直 pending、直接到 99%，或一个进度条结束后又从低百分比开始

**可能原因：** 作业在等待依赖/资源槽；前端正在展示同一 DAG 的不同阶段；算法已结束但产物仍在 validating/publishing；或两个服务竞争同一 storage root。

**检查：** 请求 job runtime，核对 `status`、`stage`、`progress`、`depends_on_job_ids`、attempt 和日志。检查项目页“进行中”数量和服务端 `.serve_viewer.lease`。预览视频、生成摘要或 FFmpeg 结束都不是发布成功证明。

**处理：** 保持唯一服务运行，让 validating/publishing 完成。仅当 runtime 明确失败或可取消时使用项目 API retry/cancel；不要根据动画直接杀算法进程。进度应在结果校验和 owner manifest 发布后才到 100%。

### 服务重启后任务状态不可信或取消了错误 PID

**可能原因：** 旧进程 PID 已复用，或兼容 JobRunner 的 `job_process.json` 仍记录重启前进程。当前 ProjectRuntime 会把无法验证的执行标为 interrupted，并恢复安全 queued 作业。

**检查：** 对当前项目使用 job runtime 和 attempt claim/token；对兼容 run 同时比对 PID、进程启动时间和 `job_process.command`。检查服务启动时的 restore 日志。

**处理：** 不要仅凭 PID 调用系统终止。通过项目 API retry 生成新 attempt；兼容 run 优先使用新 `runId`，需要原地重跑时先备份整个 run。

## 全局 CAD 替换

### 项目页没有“替换”入口，或替换请求被拒绝

**可能原因：** 项目尚无可验证的保存工作台输出，坐标系确认未勾选，上传 CAD 与当前 SHA-256 相同，或已有替换作业进行中。

**检查：** 查看 snapshot 的 `cad_replacement.eligible/reason/status`，验证至少一个片段引用的 `workbench_outputs/<revision>` 完整且 SHA-256 正确。

**处理：** 先完成并保存一个片段的坐标系标定。确认新版 CAD 的坐标系、单位和原点确实相同后再勾选确认。坐标系变化时新建项目，不要绕过 eligibility。

### CAD 替换失败、成功后渲染失效，或重启时项目恢复失败

**可能原因：** 候选 CAD 导入/转换失败；成功替换按设计使 CAD 依赖的 render/merge stale；旧分析 job 仍包含替换前 CAD identity。

**检查：** 查看 snapshot 的 replacement progress/error、对应 `cad_replacement` job runtime、`project_manifest.source_assets._cad_versions` 和活动 CAD。成功时 clips/trajectory/workbench 引用应保持，render/merge owner 应为 `stale_input`。

**处理：** 替换失败时修复 CAD 文件后重新提交，旧 CAD 会继续活动。替换成功后重新进入工作台检查/微调并重新渲染、合并，不要重跑 SfM/初始路线。重启恢复错误应先升级/使用 snapshot identity 修复，再检查不可变输入是否缺失；不要手工把 job 标为 success。

## CAD 文字与工程标牌

### DXF 桩号/文字不显示，或大型 CAD 拖动明显卡顿

**可能原因：** 文本实体不是受支持的 `TEXT`、`MTEXT` 或块属性；图层隐藏；CAD 转换丢失文字；或显示预算/距离裁剪主动隐藏了远处文字。

**检查：** 查看导出的 `design.json` 文本实体数量、内容、图层、位置、旋转、字号和 `text_role`。切换图层并靠近文本区域，比较原 DXF 与转换后的设计数据。

**处理：** 优先使用可直接解析的 DXF，确认块属性实际包含值。不要为显示所有远距离文字取消预算；应调整字号/可见图层或分区查看，保持相机交互性能。

### 播放时标牌、引线或锚点单独残留，或 CAD 标牌不随视角移动

**可能原因：** 浏览器缓存了旧脚本；当前 PTS 没有有效 Base/Corrected CameraState；只更新了卡片可见性而没有同步 leader/anchor；或 annotation 的 CAD world 坐标/PTS 范围不正确。

**检查：** 从项目页重新进入工作台并强制刷新，确认当前阶段为 render、annotation 为 `cad_anchor`，查看 annotation-preview 的权威 PTS、有效相机轨迹和浏览器控制台/API 错误。behind-camera、出画和超时范围应让卡片、线和锚点一起隐藏。

**处理：** 使用当前服务生成的新 session，不复用旧 token URL。重新在右侧 CAD 选点并保存；确认 Base/Corrected 切换后重新投影。视频目标跟踪标牌创建入口当前隐藏，不应通过 DOM 或旧 URL 强行启用。

### 预览有标牌但 `rendered.mp4` 没有，或提示“输入已变化，本次渲染未发布”

**可能原因：** 渲染启动后 annotation、CAD、工作台输出或活动轨迹 revision 发生变化；渲染绑定的 annotation bundle 为空/旧；或标牌叠加后帧数/frame map 校验失败。

**检查：** 查看 render job runtime、attempt 中的 annotation render bundle、`annotated_overlay.mp4`、`render_frame_map.json` 和 validation error。对比启动和完成时的 input fingerprint/annotation revision。

**处理：** 停止编辑，保存最新标牌后重新点击“渲染视频”。存在标牌时应烧录完整卡片/引线/锚点；无标牌时仍允许快速渲染。不要复制未发布 attempt 视频代替 `render_outputs/<clip>/<revision>/rendered.mp4`。

## 上传、路径与查看器媒体

### 上传被拒绝、数据集找不到或目录异常

**可能原因：** 上传接口要求 `multipart/form-data` 且文件字段名为 `file`；视频只接受 `.mp4`、`.mov`、`.avi`、`.mkv`，SRT 只接受 `.srt`。数据集会被转为 ASCII slug，`runId` 只能使用字母、数字、`_`、`.`、`-`。

**检查：** 查看浏览器网络响应中的 400/404；请求 `GET /api/workflow/list-datasets`，再用返回的 slug 查询 `dataset-manifest`。确认上传请求包含查询参数 `dataset`，而不是把它误放进 JSON。

**处理：** 使用界面上传，或让客户端发送正确的 multipart 请求；使用简单的 ASCII 数据集名和安全的 run ID。不要把 `..`、盘符或 URL 当作 dataset/run ID。ZIP CAD 若失败，检查压缩包中是否存在可用的 `design.json`，并移除不安全的绝对路径或 `..` 成员。

### 查看器缺少视频、CAD 或运行产物

**可能原因：** `--root` 与 `--storage-root` 指向不同部署但查看器访问了另一台服务；`project_id`/`clip_id` 或兼容 `dataset`/`runId` 不一致；旧数据没有被显式只读挂载。

**检查：** 对当前项目核对 `<storage-root>/projects/<project_id>` 五类 manifest、snapshot 和 workbench output 引用；对兼容 workflow 再核对 `<storage-root>/data/<dataset>/dataset_manifest.json` 与 `<storage-root>/runs/<dataset>/<runId>/manifest.json`。确认浏览器媒体 URL 指向当前服务。

**处理：** 用项目创建时相同的 storage root 重启；旧数据可配置 `--extra-root legacy=PATH` 只读访问。若未传 `--storage-root`，`--root` 同时是项目/workflow 写入根；若传入，storage root 必须预先存在。不要将 `--extra-root` 命名为 `data` 或 `runs`，也不要期望它成为项目写入根。

## SfM、CUDA 与全局 BA

### 请求 CUDA 后回退到 CPU，或 CUDA 不生效

**可能原因：** 默认是 `pycolmap + cpu`；CUDA 只有在显式选用且能力检测确认时才用于已支持的特征提取和匹配。驱动、`pycolmap` 构建、COLMAP CLI 路径或 GPU 可见性都可能不满足条件。

**检查：** 运行：

```powershell
python -m cadscene.cli.check_sfm_environment --device cuda --json
```

检查 `02_sfm/sfm_stats.json`、`job_status.json` 和 SfM 日志中的 fallback 原因。确认 `run_sfm` 使用 `--device cuda`，而不是默认 `--device cpu`。

**处理：** 修复后端/驱动或改用 CPU；CPU fallback 是受支持的降级路径。若任务必须在 CUDA 不可用时失败，显式加入 `--no-cpu-fallback`。不要把 mapper 或 global bundle adjustment 解释为 GPU 加速。

### SfM 卡在 global BA（全局束调整）或进度长时间不变

**可能原因：** 全局 BA 是重建优化的一部分，可能在图像、点数或迭代较多时耗时；它不承诺使用 GPU。也可能是子进程已经失败但界面尚未刷新。

**检查：** 用 `GET /api/workflow/job-log?dataset=…&runId=…&stage=sfm` 读取末尾日志，检查 `job_process.json` 的 `status`、`returncode` 和 PID，以及 `02_sfm/sfm_report.md` / `sfm_stats.json`。复查 `--ba-global-frames-ratio`、`--ba-global-points-ratio`、`--ba-global-frames-freq`、`--ba-global-points-freq`、`--ba-global-max-num-iterations`、`--ba-global-max-refinements`。

**处理：** 让有持续日志和存活 PID 的任务完成，或取消后用较小视频范围/更保守的 BA 参数重新运行。服务重启后不要仅凭 `job_process.json` 的 PID 取消：先确认当前进程命令与 `job_process.command` 一致；无法确认时不要取消，使用新的 `runId` 重跑。若无新日志且进程已结束，以 `job_process.json` 的返回码为准；优先使用新的 `runId`，若必须原地重跑，先完整备份该 run 目录，因为同名产物和 manifest 会被覆盖。不要仅凭进度条推断 GPU 或 BA 已成功。

## CAD、坐标与 FOV

### CAD 上传后不是 ready，或后续阶段提示缺少 CAD

**可能原因：** DXF 解析失败；DWG 仅保存原文件但环境缺少外部 DWG→DXF 转换器；ZIP 不含 `design.json`。

**检查：** 查看 dataset manifest 的 `cad.status`、`cad.error`、`cad.design_json` 及 `warnings`。`raw_saved` 表示 DWG 已保留但尚无可用 `design.json`，不是 ready。

**处理：** 优先上传可解析 DXF 或有效 `design.json`；安装并配置 DWG 转换器后重新导入 DWG。只有 video 与 `cad.status: ready` 同时成立，数据集才会成为 `ready`。

### 对齐被拒绝、路线漂移，或 CAD / SfM 看起来不在同一平面

**可能原因：** SfM world、CAD meters、查看器 `cad_world` 和 partial-SRT 的 local ENU 不是同一坐标系；比例、`origin_xy`、pitch 符号或人工锚点不一致。可观测性不足、共线/退化锚点或过大残差也会导致拒绝。

**检查：** 查看 `03_alignment/alignment.json` 的 `alignment_mode`、`scale_observable`、Sim3、残差、FOV 来源和 warnings，以及 `keyframe_correspondences.csv`。确认人工关键帧不是 `algorithm_prediction`，并核对 `cad_scale`、`origin_xy`。常规对齐至少需要两个位置可区分的人工锚点；partial-SRT 融合需要至少三个空间独立约束。

**处理：** 在 CAD 已核实的位置补充分散的人工关键帧，避免重复标记相邻画面；统一 Web→CAD 换算和前后端 pitch 符号。不要把普通 SRT 高程当作 CAD 高程。`rotation_only` 中 `scale = 1.0` 只是协议回退，不是实测比例。

### FOV 约为 29°、内参异常或对齐拒绝

**可能原因：** 重建出的 FOV 或焦距比例可能不可靠；约 29° 的值本身不是精度证明。没有一致的人工 FOV 时，异常内参会触发拒绝。

**检查：** 读取 `alignment.json` 的 FOV 来源和警告，比较所有已确认人工关键帧的 FOV。可信人工 FOV 必须有限、合法且彼此差异不超过 0.1°。

**处理：** 在同一镜头条件下人工复核关键帧 FOV；满足一致性后其平均值优先于 SfM 重建值。若缺失或不一致，使用经审计的配置 FOV 或修复相机输入后重建；不要仅为了消除 29° 而硬编码一个数值。

## SRT 与 pure-rotation

### 上传 SRT 后显示 Interface only 或无法启动工作流

**可能原因：** 普通 SRT 只用于能力检测；门户路由 `srt_sfm_fused` 和 `srt_full_pose` 均为 Interface only，服务器会返回 409 并阻止 `run-stage`。

**检查：** 查看 `GET /api/workflow/srt-analysis?dataset=…`、manifest 中的 `srt` 和 `workflow.trajectory_mode`，以及 `srt_analysis_report.md`。

**处理：** 要使用 Stable 主路线，创建未上传 SRT 的数据集并走 `sfm_only`。需要研究 SRT 时使用独立的 `fuse_srt_sfm` Experimental CLI，并接受其尚未接入 JobRunner。普通 SRT 不能当作高精度位置、姿态或 CAD 高程真值。

### pure-rotation 后无法放置、没有轨迹或渲染位置不对

**可能原因：** 该 Experimental 路线要求用户声明悬停/纯旋转且没有 SRT，并依赖外部 OpenGV 后端；未经全局放置就没有 CAD 中的基础轨迹，未经校正则没有 corrected 轨迹。

**检查：** 确认 manifest 的 `workflow.trajectory_mode` 为 `pure_rotation`，检查 `02_pure_rotation/backend_summary.json`、`camera_rotation_raw.json`、日志和 `/api/pure-rotation/status`。放置前必须已有 raw 轨迹；校正前必须已有 `03_pure_rotation_placement/camera_track_cad_base.json`。

**处理：** 安装/配置 OpenGV 后端，先运行纯旋转，再保存固定相机中心的全局放置，最后按需提交局部姿态校正。它固定相机中心，不恢复平移或尺度，也不会自动识别纯旋转视频；不能用于推断沿道路行进距离。

## 关键帧、质量、渲染与道路诊断

### “生成关键帧计划”或质量阶段按钮无法完成

**可能原因：** 初始路线拟合尚未生成 `03_alignment/alignment.json`；还没保存人工关键帧；或者质量计划没有与最新人工轨迹同步。

**检查：** 确认 `01_keyframes/camera_track_manual.json` 存在且至少含两个非 `algorithm_prediction` 的人工关键帧，再检查 `01_keyframes/keyframe_plan.json`。调用“生成关键帧计划”前必须完成初始对齐；质量阶段会验证计划。

**处理：** 先点击保存关键帧/相机轨迹，再完成路线拟合，随后生成计划。补帧后再次保存轨迹以同步计划；明确不需要的建议可通过“忽略建议”记录，而不是删除运行状态文件。

### 渲染失败、叠加偏移，或道路表面诊断被跳过

**可能原因：** 渲染缺少视频、CAD 或 `sfm_camera_path.csv`；对齐尺度/原点不一致；CAD 没有检测到道路中心线时，pipeline 会跳过 `road_surface`。

**检查：** 查看 `08_render/render_report.md`、`render_stats.json` 与工作流日志；核对 `03_alignment/sfm_camera_path.csv`、`alignment.json`、`cad_scale`、`origin_xy`。对道路诊断检查 run manifest 的 `road_surface` 记录及 `06_road_surface/sfm_geometry_report.md`。

**处理：** 先修复必需输入并从所需阶段重跑；重新审计关键帧和 CAD 坐标，不要用 `debug_scale` 掩盖真实的 Sim3 或原点问题。缺少道路中心线时，补充可识别的中心线资产后重新运行诊断；`road_surface: skipped` 说明该诊断没有运行，不表示渲染、对齐或道路质量已经通过。
