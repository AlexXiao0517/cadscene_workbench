# 开发者指南

本页面向部署、维护和二次开发人员。面向设计人员的启动与操作请看项目根目录的 [README](../../README.md)；系统边界与坐标约定分别见[系统架构](../design/system-architecture.md)和[坐标系与 SfM-CAD 对齐](../design/coordinates-and-alignment.md)。

## 环境与安装

项目要求 Python 3.10 或更高版本。基础安装只包含 `numpy`；按需安装的依赖组由 `pyproject.toml` 定义：

| 依赖组 | 用途 |
| --- | --- |
| `dev` | `pytest` 测试工具 |
| `sfm` | OpenCV 与 `pycolmap` SfM 后端 |
| `cad` | `ezdxf` DXF 导入 |
| `diagnostics` | 质量图表等可选诊断输出 |
| `segmentation` | Torch/Transformers 语义分割实验依赖 |

在 PowerShell 中建立可编辑开发环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev,sfm,cad,diagnostics]"
python -m pip install PyYAML
```

当前 `pyproject.toml` 没有声明 PyYAML；上面的 extras 不会安装它。需要读取 YAML 管线/数据集配置时，临时显式安装 `PyYAML`，直到项目元数据更新为止。`segmentation` 只在需要该实验性依赖时追加安装。安装后先检查解释器和 SfM 后端能力：

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
| `cadscene/cad/` | DXF 导入、DWG 转换、中心线检测与投影 |
| `cadscene/srt/` | SRT 解析、PTS 同步、ENU、能力检测和实验性融合核心 |
| `cadscene/pure_rotation/` | 外部 OpenGV 后端、固定相机中心放置与局部姿态校正 |
| `cadscene/projects/` | 项目 manifest、视频分析、持久队列、工作台 session、渲染、合并、CAD 替换与启动恢复 |
| `cadscene/annotations/` | CAD/video 锚点模型、工程标牌布局、tracking revision 与透明叠加渲染 |
| `cadscene/rendering/`、`cadscene/diagnostics/`、`cadscene/viewer/` | 叠加渲染、道路/姿态诊断及查看器场景导出 |
| `apps/workflow_portal/` | 创建项目和上传文件的静态入口 |
| `apps/project_workspace/` | 项目资产、片段、批量任务、CAD 替换与合并的管理界面 |
| `apps/web_camera_viewer/` | 视频、CAD、关键帧、工作流阶段与产物查看器 |

## CLI 目录

所有命令均从仓库根目录运行。下表列出当前实现的入口；参数以各模块的 `--help` 输出为准。

| 命令 | 主要用途与关键参数 |
| --- | --- |
| `serve_viewer` | 本地 HTTP 服务；`--bind`、`--port`、`--root`、`--storage-root`、可重复的 `--extra-root NAME=PATH` |
| `check_sfm_environment` | 检查后端；`--colmap-exe`、`--device {auto,cpu,cuda}`、`--json` |
| `run_sfm` | 运行 SfM；`--dataset`、`--run-id`、`--output-root`、`--video`、`--backend {auto,pycolmap,colmap_cli}`、`--device {auto,cpu,cuda}`、全局 BA 参数 |
| `align_to_cad` | 用人工轨迹做 Sim3 对齐；需要 `--trajectory`、`--web-camera-track`、`--cad-dir`、`--cad-scale`、`--origin-xy` |
| `evaluate_quality` | 生成关键帧质量时间线和建议；需要对齐、SfM 相机路径、人工轨迹和 CAD 参数 |
| `export_viewer_scene` | 导出查看器场景；需要 `--alignment`、`--cad-scale`、`--origin-xy`，点云/轨迹可选 |
| `analyze_road_surface` | 输出道路表面和姿态诊断；需要点云、轨迹、对齐、SfM 路径、CAD 与坐标参数 |
| `render_overlay` | 输出视频叠加；需要 `--video`、`--cad-dir`、`--sfm-camera-path` |
| `run_pipeline` | 读取流水线配置并串行执行；`--config`、`--stages`、`--skip-render`、`--skip-road-surface`、`--skip-viewer-scene` |
| `fuse_srt_sfm` | 实验性 partial-SRT CLI；需要轨迹和 SRT，支持 `--frame-timestamps`、时间偏移、ENU/融合质量参数 |
| `run_pure_rotation` | 调用外部 OpenGV 纯旋转后端；需要 `--video`、`--output-root`，可提供后端目录/命令 |
| `render_pure_rotation` | 用已校正的固定中心轨迹渲染；需要视频、CAD 和 `--track` |
| `benchmark_sfm_backends` | 对比 SfM 后端；支持 `--dry-run`、帧范围和 GPU 索引 |

## 本地服务、静态根与存储根

建议为人工项目显式准备独立可写根：

```powershell
New-Item -ItemType Directory -Force -Path D:\cadscene-work | Out-Null
python -m cadscene.cli.serve_viewer `
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
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
```

项目队列与恢复契约在 `tests/projects/`，标牌在 `tests/annotations/`，静态前端在 `tests/viewer/`，命令行在 `tests/cli/`，跨领域 smoke 在 `tests/integration/`。当前测试包含单元、静态契约和合成集成验证，但不代替真实 GPU、外部 OpenGV、长视频、DXF/DWG 转换器和浏览器人工验收。提交前先跑聚焦测试；涉及行为、持久化或依赖边界时必须跑完整套件及依赖扫描。

## 开发边界

- `sfm_only` 是唯一 Stable 的端到端路线；`pure_rotation` 是 Experimental，固定相机中心，不恢复平移或尺度。
- partial-SRT core 是 Experimental CLI，尚未接入正式 JobRunner。门户中的 `srt_sfm_fused` 与 `srt_full_pose` 均为 Interface only，服务会阻止其启动阶段。
- CAD 锚定工程标牌已经接入预览和正式片段渲染；视频目标跟踪标牌创建入口当前隐藏，不能作为正式功能宣传。
- 全局 CAD 替换只适用于坐标系、单位和原点不变且已有有效工作台输出的项目；成功后保留轨迹，只让渲染和合并 stale。
- 相机轨迹、annotation、渲染和合并必须保持 source PTS/frame-map 契约；禁止用固定 FPS frame index 替代。
- 可靠且一致的人工关键帧 FOV 优先于不可靠的 SfM 重建 FOV；普通 SRT 只是元数据能力线索，而非高精度位置、姿态或 CAD 高程真值。
