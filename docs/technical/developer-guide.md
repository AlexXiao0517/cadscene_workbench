# 开发者指南

本页面向部署、维护和二次开发人员。面向设计人员的启动与操作请看项目根目录的 [README](../../README.md)；系统边界与坐标约定分别见[系统架构](../design/system-architecture.md)和[坐标系与 SfM-CAD 对齐](../design/coordinates-and-alignment.md)。

## 环境与安装

项目要求 Python 3.10 或更高版本。基础安装包含当前正式服务、SfM、DXF 和媒体处理所需依赖；按需安装的依赖组由 `pyproject.toml` 定义：

| 依赖组 | 用途 |
| --- | --- |
| `dev` | `pytest` 测试工具 |
| `diagnostics` | 质量图表等可选诊断输出 |
| `sfm` / `cad` / `video_analysis` | 兼容既有安装命令的能力别名；正式依赖已在基础安装中声明 |

在 PowerShell 中建立可编辑开发环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,diagnostics]"
cadscene-workbench doctor --storage-root D:\cadscene-work
```

`doctor` 会统一检查核心 Python 包、FFmpeg/FFprobe、pycolmap、正式网页和配置资源、固定版本 OpenGV 纯旋转后端及存储目录可写性。full-pose 还依赖 `pyproj` 随包提供的 PROJ 数据库；安装或制作离线包后应额外执行 EPSG:4549 smoke：

```powershell
python -c "from pyproj import CRS, Transformer; assert CRS.from_epsg(4549).to_epsg() == 4549; e,n=Transformer.from_crs(4326,4549,always_xy=True).transform(120,30); assert abs(e-500000)<1"
```

这条检查只证明坐标库和 `proj.db` 可读取，不代表当前 CAD 必然采用 120°中央经线。也可继续单独检查 SfM 后端能力：

```powershell
python --version
python -m cadscene.cli.check_sfm_environment --device auto
python -m cadscene.cli.check_sfm_environment --device cuda --json
```

检查命令会探测 `pycolmap`、可选的官方 COLMAP CLI 与 CUDA；它不等同于真实视频、GPU 或 OpenGV 的端到端验收。默认 SfM 是 `pycolmap + cpu`。CUDA 必须显式选择，并只加速已支持的特征提取和匹配；不可用时可回退 CPU，除非运行 SfM 时传入 `--no-cpu-fallback`。

## 模块与前端位置

| 位置 | 职责 |
| --- | --- |
| `cadscene/cli/` | 可直接以 `python -m cadscene.cli.<module>` 调用的维护和批处理入口 |
| `cadscene/workflow/` | 数据集导入、JobRunner、工作流状态和关键帧计划 |
| `cadscene/sfm/` | `pycolmap` / COLMAP CLI 后端、轨迹、相机初始化和点云 |
| `cadscene/alignment/` 与 `cadscene/core/sim3.py` | 人工关键帧、SfM-CAD Sim3 对齐、质量与工件记录 |
| `cadscene/cad/` | 正式 DXF 导入、兼容 DWG 转换、中心线检测与投影 |
| `cadscene/srt/` | SRT 解析、PTS 同步、能力检测、PROJ-backed CGCS2000 候选/投影、全姿态米制轨迹、固定轨迹视觉姿态和历史 partial-SRT 融合核心 |
| `cadscene/pure_rotation/` | 外部 OpenGV 后端、固定相机中心放置与局部姿态校正 |
| `cadscene/projects/` | 项目 manifest、视频分析、持久队列、工作台 session、渲染、合并、CAD 替换与启动恢复 |
| `cadscene/annotations/` | CAD/video 锚点模型、工程标牌布局、tracking revision 与透明叠加渲染 |
| `cadscene/rendering/`、`cadscene/diagnostics/`、`cadscene/viewer/` | 叠加渲染、道路/姿态诊断及查看器场景导出 |
| `apps/workflow_portal/` | 创建项目和上传文件的静态入口 |
| `apps/project_library/` | 本地项目库、卡片/列表切换和项目重命名入口 |
| `apps/project_workspace/` | 项目资产、片段、批量任务、CAD 替换与合并的管理界面 |
| `apps/web_camera_viewer/` | 视频、CAD、关键帧、工作流阶段与产物查看器 |

正式用户界面的输入契约以 `apps/workflow_portal/index.html` 和
`apps/workflow_portal/workflow_portal.js` 为准：当前只开放 MP4、DXF 和可选 SRT。
`cadscene.projects.uploads` 与旧 `cadscene.workflow` 中较宽的扩展名集合用于兼容、迁移
或维护接口，不能直接转写成 README 的正式格式支持清单。

## CLI 目录

所有命令均从仓库根目录运行。下表列出当前实现的入口；参数以各模块的 `--help` 输出为准。

| 命令 | 主要用途与关键参数 |
| --- | --- |
| `cadscene-workbench serve` | 统一本地 HTTP 服务；参数与 `serve_viewer` 兼容 |
| `cadscene-workbench doctor` | 检查完整正式运行环境并可输出 JSON |
| `serve_viewer` | 兼容的 Python 模块入口 |
| `check_sfm_environment` | 检查后端；`--colmap-exe`、`--device {auto,cpu,cuda}`、`--json` |
| `run_sfm` | 运行 SfM；`--dataset`、`--run-id`、`--output-root`、`--video`、`--backend {auto,pycolmap,colmap_cli}`、`--device {auto,cpu,cuda}`、全局 BA 参数 |
| `align_to_cad` | 用人工轨迹做 Sim3 对齐；需要 `--trajectory`、`--web-camera-track`、`--cad-dir`、`--cad-scale`、`--origin-xy` |
| `evaluate_quality` | 生成关键帧质量时间线和建议；需要对齐、SfM 相机路径、人工轨迹和 CAD 参数 |
| `export_viewer_scene` | 导出查看器场景；需要 `--alignment`、`--cad-scale`、`--origin-xy`，点云/轨迹可选 |
| `analyze_road_surface` | 输出道路表面和姿态诊断；需要点云、轨迹、对齐、SfM 路径、CAD 与坐标参数 |
| `render_overlay` | 输出视频叠加；需要 `--video`、`--cad-dir`、`--sfm-camera-path` |
| `run_pipeline` | 读取流水线配置并串行执行；`--config`、`--stages`、`--skip-render`、`--skip-road-surface`、`--skip-viewer-scene` |
| `fuse_srt_sfm` | 实验性 partial-SRT CLI；需要轨迹和 SRT，支持 `--frame-timestamps`、时间偏移、ENU/融合质量参数 |
| `build_srt_full_pose` | 用精确 frame map、完整 DJI SRT、已确认 georeference 和用户水平 FOV 构建 `02_srt_full_pose` 米制轨迹；不调用 SfM |
| `build_srt_fixed_track_visual_pose` | 用精确 frame map、SRT GPS/`rel_alt`、已确认 georeference 和用户水平 FOV 自适应抽帧调用 COLMAP 稀疏重建/RADIAL 标定，把旋转注册到锁定位置并构建 `02_srt_visual_pose`；点云仅供诊断 |
| `build_cad_georeference_candidates` | 从不可变候选请求读取 SRT/CAD bbox，按可选中央经线筛选 CGCS2000 EPSG，原子输出候选与 `adapter_progress.json` |
| `run_pure_rotation` | 调用外部 OpenGV 纯旋转后端；需要 `--video`、`--output-root`，可提供后端目录/命令 |
| `render_pure_rotation` | 用已校正的固定中心轨迹渲染；需要视频、CAD 和 `--track` |
| `benchmark_sfm_backends` | 对比 SfM 后端；支持 `--dry-run`、帧范围和 GPU 索引 |

## 本地服务、静态根与存储根

建议为人工项目显式准备独立可写根：

```powershell
New-Item -ItemType Directory -Force -Path D:\cadscene-work | Out-Null
cadscene-workbench serve `
  --bind 127.0.0.1 `
  --port 8300 `
  --storage-root D:\cadscene-work
```

打开 `http://127.0.0.1:8300/apps/workflow_portal/`。`--root` 是静态站点根，默认是代码仓库根；`--storage-root` 是项目、兼容 workflow 和产物的可写根。未传 `--storage-root` 时它继承 `--root`，服务会在代码目录中创建 `projects/`。生产部署可进一步分离静态根：

```powershell
python -m cadscene.cli.serve_viewer `
  --root D:\deploy\cadscene-site `
  --storage-root D:\cadscene-workspace `
  --extra-root legacy=D:\cadscene-legacy
```

服务以 Project API 访问 `projects/`，并以 `/data/`、`/runs/` 暴露兼容 workflow 数据；根分离时它们都位于 storage root。`--extra-root` 是只读旧数据挂载，不改变写入位置。不要把额外挂载命名为 `data` 或 `runs`。视频请求支持 HTTP Range。项目 workbench token 是写权限凭据；服务没有公网认证层，不要把监听地址改为外网地址，除非另有认证和网络隔离。

同一个 `<storage-root>/projects/` 由 `.serve_viewer.lease` 保证只有一个服务进程消费队列。启动第二个指向同一根的服务会失败；不同端口不能规避此约束。

## 当前项目目录与持久化边界

项目主目录不是旧版 `data/runs`，而是：

```text
<storage-root>/projects/<project_id>/
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
```

五个 manifest 分别拥有项目、片段、队列、渲染和标牌状态。更新必须携带当前 `expected_revision`；跨 manifest 操作使用 `operation_id` 和恢复 intent。不要通过脚本直接把某个 JSON 的 `status` 改为 success，也不要只移动单个 immutable revision。

`workbench_sessions/` 保存临时授权和心跳；`workbench_outputs/` 保存可恢复的持久结果。前端刷新后应从项目页创建/恢复 session，不能长期复用带 `projectWorkbenchToken` 的旧 URL。

渲染成功必须同时发布 `rendered.mp4`、`render_frame_map.json` 和 `render_output_manifest.json`。source decoded-frame integer PTS、精确 `time_base` 和 output ordinal 是渲染/合并契约；任何标牌叠加都不得修改该映射。

## 开发与测试

先跑快速的契约测试，再跑完整套件：

```powershell
python -m pytest -q tests/cli/test_serve_viewer_cli.py tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_workflow_api.py
python -m pytest -q tests/projects tests/annotations tests/rendering tests/integration
python -m pytest -q tests/docs/test_current_documentation.py
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
```

项目队列与恢复契约在 `tests/projects/`，标牌在 `tests/annotations/`，静态前端在 `tests/viewer/`，命令行在 `tests/cli/`，跨领域 smoke 在 `tests/integration/`。`cad_georeference_candidates` 是不绑定 clip 的 `light_compute` 项目任务，输入指纹绑定 CAD、SRT、中央经线、候选上限和算法版本；成功产物只有当前指纹仍有效时才能确认。`tests/integration/test_srt_full_pose_workflow.py` 覆盖完整姿态的 metric-direct 对齐；固定轨迹相关集成测试覆盖 SRT Base 位置、COLMAP 姿态注册、可选诊断点云和六自由度残差拟合。packaging 测试在源码目录外解析 EPSG:4549。当前测试仍不代替真实 GPU、外部 OpenGV、长 MP4、DXF、现场 DJI 镜头或独立测量高程验收。提交前先跑聚焦测试；涉及行为、持久化或依赖边界时必须跑完整套件及依赖扫描。

现行文档契约测试同时读取上传页、自动分析与项目页源码，锁定“正式界面只支持
MP4/DXF”“纯旋转由分析自动推荐、项目页可覆盖”“Project 与兼容 dataset/run 存储
分层”等事实。修改这些产品契约时，应先改实现和对应行为测试，再同步文档契约，不能
仅放宽文档措辞。

## 开发边界

- `sfm_only` 与 `pure_rotation` 都是正式可执行路线；后者固定相机中心且不恢复平移或尺度，依赖固定版本 OpenGV 后端。当前仍不成熟的是自动视频分析和路线推荐精度，项目页允许人工覆盖。
- partial-SRT core 是 Experimental CLI，`srt_sfm_fused` 只兼容读取历史 manifest/产物，新项目不再推荐或创建它。`srt_full_pose` 与 `srt_fixed_track_visual_pose` 都必须绑定当前 CAD 指纹确认 CGCS2000 投影，并由用户输入单一水平 FOV。完整姿态分支跳过 SfM；固定轨迹分支用 SRT GPS/`rel_alt` 提供位置，运行 COLMAP 稀疏重建/RADIAL 标定并注册旋转，但不让 SfM 改写最终逐帧位置。诊断点云不是下游强依赖。
- CAD 锚定工程标牌已经接入预览和正式片段渲染；视频目标跟踪标牌创建入口当前隐藏，不能作为正式功能宣传。
- 全局 CAD 替换只适用于坐标系、单位和原点不变且已有有效工作台输出的项目；成功后保留轨迹，只让渲染和合并 stale。
- 相机轨迹、annotation、渲染和合并必须保持 source PTS/frame-map 契约；禁止用固定 FPS frame index 替代。
- 可靠且一致的人工关键帧 FOV 优先于不可靠的 SfM 重建 FOV；两个 SRT 正式分支的 FOV 都是明确的用户水平角度。固定轨迹分支的 `rel_alt` 加统一 `route_offset_xyz_m[2]`，全姿态分支则用 `cad_z_offset_m` 接入 CAD；`abs_alt` 只作诊断，不能宣传为自动解决的绝对高程。
