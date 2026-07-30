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
| `cadscene/rendering/`、`cadscene/diagnostics/`、`cadscene/viewer/` | 叠加渲染、道路/姿态诊断及查看器场景导出 |
| `apps/workflow_portal/` | 创建数据集和上传文件的静态入口 |
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

最小本地启动命令为：

```powershell
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

打开 `http://127.0.0.1:8300/apps/workflow_portal/index.html`。`--root` 是静态站点根，默认是项目根；未传 `--storage-root` 时，它也同时是可写 workflow `data/` 与 `runs/` 根。传入 `--storage-root` 时，该目录必须预先存在，并成为可写 workflow 根；部署时将二者分开可避免把运行产物写入静态代码目录：

```powershell
python -m cadscene.cli.serve_viewer `
  --root D:\deploy\cadscene-site `
  --storage-root D:\cadscene-workspace `
  --extra-root legacy=D:\cadscene-legacy
```

服务会以 `/data/` 和 `/runs/` 暴露 workflow 数据；当两个根分离时，它们映射到 storage root 的两个目录。`--extra-root` 只读挂载，目录必须已存在，也不会改变写入位置。虽然程序只在两个根不同时拒绝名为 `data` 或 `runs` 的额外挂载，部署约定一律不要使用这两个名称，以免覆盖或混淆 workflow 路径。视频请求支持 HTTP Range。不要把监听地址改为外网地址，除非另有认证和网络隔离措施。

## 开发与测试

先跑快速的契约测试，再跑完整套件：

```powershell
python -m pytest -q tests/cli/test_serve_viewer_cli.py tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_workflow_api.py
python -m pytest -q tests/workflow tests/alignment tests/sfm tests/srt
python -m pytest -q
```

静态前端契约在 `tests/viewer/`；命令行契约在 `tests/cli/`；端到端的 partial-SRT 覆盖在 `tests/integration/`。当前测试包含单元、静态契约和合成集成验证，但并不证明真实 GPU、外部 OpenGV、大规模视频、DXF/DWG 转换器或所有采集设备已经验证。提交前应至少运行受改动覆盖的测试；涉及行为改动时再运行完整套件。

## 开发边界

- `sfm_only` 是唯一 Stable 的端到端路线；`pure_rotation` 是 Experimental，固定相机中心，不恢复平移或尺度。
- partial-SRT core 是 Experimental CLI，尚未接入正式 JobRunner。门户中的 `srt_sfm_fused` 与 `srt_full_pose` 均为 Interface only，服务会阻止其启动阶段。
- 可靠且一致的人工关键帧 FOV 优先于不可靠的 SfM 重建 FOV；普通 SRT 只是元数据能力线索，而非高精度位置、姿态或 CAD 高程真值。
