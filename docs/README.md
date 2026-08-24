# 文档中心

本中心面向两类读者：使用工作台完成视频与 CAD 对齐、标注和交付的设计人员，以及负责部署、排障和二次开发的维护人员。日常入口是[中文 README](../README.md)和[English README](../README_EN.md)；本页给出当前运行手册和技术参考的阅读顺序。

## 使用工作台

- [中文 README](../README.md)：从上传、项目片段管理、工作台、工程标牌到渲染和合并的完整操作。
- [上传工作流与轨迹路由](workflow_routing.md)：正式项目支持的输入格式、自动运动分析、推荐工作流和人工覆盖边界。
- [Web Camera Viewer 使用说明](web_viewer_usage.md)：查看器 URL、媒体加载、静态根与存储根。
- [SRT 能力检测](srt_capability_detection.md)：普通 SRT 的字段、路由和精度边界。
- [Pipeline 使用说明](pipeline_usage.md)：维护人员的兼容命令行批处理入口。

当前主界面由两个入口组成：

    /apps/workflow_portal/
    /apps/project_workspace/?projectId=<project_id>

上传门户创建项目并异步分析视频/CAD；项目片段管理负责片段工作流选择、批量轨迹、批量渲染、工作台进入和最终合并。刷新或重启后必须使用创建项目时相同的 `--storage-root`，项目主数据位于 `<storage-root>/projects/<project_id>/`。

必须区分两套入口：正式项目流程使用 `/apps/workflow_portal/`、Project API 和 `projects/`，当前上传界面只开放 MP4、DXF 和可选 SRT；兼容 dataset/run 工作流使用 `/api/workflow/*`、`data/` 和 `runs/`，用于既有单片段工件和维护接口。兼容后端接受某个扩展名，不代表正式项目界面支持该格式。

## 当前能力状态

| 工作流或能力 | 状态 | 应如何理解 |
| --- | --- | --- |
| `sfm_only` 项目管线 | Stable | 视频分析、切片、SfM、人工关键帧、路线拟合、质量、渲染和严格帧分区合并已经接入项目队列。 |
| `pure_rotation` | Supported | 固定相机中心的旋转恢复、全局放置、局部姿态校正和渲染已进入正式流程；自动视频分析与路线推荐仍需人工复核。 |
| CAD 原图文字 | Supported | DXF `TEXT`、`MTEXT` 和块属性文字显示在 CAD 三维视图，用于桩号和图纸标注参考。 |
| CAD 锚定工程标牌 | Supported | 屏幕空间卡片、折线引线和锚点按有效相机轨迹投影，并可烧录到片段视频。 |
| 视频目标跟踪标牌 | Hidden baseline | 后端和历史数据兼容存在，但当前创建入口隐藏，不作为用户可用能力。 |
| 全局 CAD 替换 | Supported with guard | 仅坐标系已打通的项目可用；要求确认新版 CAD 坐标系相同，保留轨迹并使渲染/合并 stale。 |
| partial-SRT core | Experimental CLI | PTS、ENU 与稳健 Sim3 核心可通过命令行使用，尚未进入正式项目队列。 |
| `srt_sfm_fused` / `srt_full_pose` | Interface only | 门户可识别并提示，但正式阶段启动被阻止。 |
| SfM CUDA | Optional | 只在受支持的特征提取和匹配范围内加速；不能确认时回退 CPU。 |

## 理解设计

- [系统架构与工作流](design/system-architecture.md)：项目领域、队列、工作台、标牌、渲染/合并、CAD 替换和恢复边界。
- [坐标系与 SfM-CAD 对齐](design/coordinates-and-alignment.md)：SfM、CAD、Web 和 ENU 坐标，Sim3、关键帧、FOV 与可观测性。

## 部署与维护

- [开发者指南](technical/developer-guide.md)：环境、目录、正式服务命令、模块边界和测试。
- [HTTP API 与产物](technical/api-and-artifacts.md)：Project API、五类 manifest、不可变输出、source PTS 和 frame map。
- [故障排查](technical/troubleshooting.md)：项目恢复、会话、队列、CAD 替换、标牌、渲染及兼容工作流诊断。
- [SfM CUDA 后端](sfm_cuda_backend.md)：默认 `pycolmap + cpu`、可选 CUDA 与 CPU 回退。
- [Roadmap](roadmap.md)：尚未开放的方向；它不是当前操作说明。

部署时建议把代码和项目数据分离：

    python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300 --storage-root D:\cadscene-work

`--storage-root` 必须存在并保持不变。省略它会让 `projects/` 写到代码根；旧版 `data/`、`runs/` 仍可能被兼容流程读取，但不应再作为当前项目领域的默认人工测试目录。

## 历史设计记录

`stage4c_*`、`stage4d_*`、`stage5b_*`、`v0.1.0_baseline_report.md` 和 [SOP](SOP/) 是历史或过程材料。`superpowers/plans/` 与 `superpowers/specs/` 保存各阶段的设计与实施证据。它们用于解释决策，不能替代 README、系统架构、开发者指南、API 参考和故障排查这些现行文档。
