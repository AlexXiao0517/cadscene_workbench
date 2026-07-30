# 文档中心

本中心面向两类读者：使用工作台完成视频与 CAD 核对的设计人员，以及负责部署、排障和二次开发的维护人员。项目的日常入口是 [中文 README](../README.md) 和 [English README](../README_EN.md)；这里解释其背后的工作流和技术边界。

## 使用工作台

- [上传工作流与轨迹路由](workflow_routing.md)：视频、CAD 和可选 SRT 的上传结果，以及哪些路由可实际执行。
- [SRT 能力检测](srt_capability_detection.md)：SRT 字段识别、保守路由和普通 SRT 的精度边界。
- [Pipeline 使用说明](pipeline_usage.md)：维护人员的命令行批处理入口。
- [Web Camera Viewer 使用说明](web_viewer_usage.md)：查看器 URL、产物加载和旧数据的只读临时挂载。

当前状态一览：

| 工作流或能力 | 状态 | 应如何理解 |
| --- | --- | --- |
| `sfm_only` | Stable | 当前唯一稳定的端到端主路径。 |
| `pure_rotation` | Experimental | 可执行的固定相机中心实验；不恢复平移或尺度，依赖外部 OpenGV 后端。 |
| partial-SRT core | Experimental CLI | PTS、ENU 与稳健 Sim3 融合核心可通过命令行使用，尚未接入正式 JobRunner。 |
| `srt_sfm_fused` portal route | Interface only | 门户可识别和提示，但服务会阻止其启动正式工作流。 |
| `srt_full_pose` | Interface only | 尚无端到端执行流程。 |
| SfM CUDA | Optional | 仅在受支持的特征提取和匹配范围内加速；不能确认时回退 CPU。 |

## 理解设计

- [系统架构与工作流](design/system-architecture.md)：门户、查看器、JobRunner、状态、存储根和恢复边界。
- [坐标系与 SfM-CAD 对齐](design/coordinates-and-alignment.md)：SfM、CAD、Web 和 ENU 坐标，Sim3、关键帧、FOV 与可观测性。

## 部署与维护

- [SfM CUDA 后端](sfm_cuda_backend.md)：默认 `pycolmap + cpu`、可选 CUDA 与 CPU 回退。
- [Pipeline 使用说明](pipeline_usage.md)：批处理、阶段产物和命令行排查。
- [Web Camera Viewer 使用说明](web_viewer_usage.md)：`--root`、`--storage-root` 和静态/产物访问边界。
- [Roadmap](roadmap.md)：尚未实现的方向；它不是当前操作说明。

## 历史设计记录

`stage4c_*`、`stage4d_*`、`stage5b_*`、`v0.1.0_baseline_report.md` 和 [SOP](SOP/) 是历史记录或过程材料，不能替代当前操作说明。`superpowers/plans/` 与 `superpowers/specs/` 保存设计和实施过程，例如 [本轮文档设计](superpowers/specs/2026-07-30-documentation-refresh-design.md)；它们同样不是运行手册。
