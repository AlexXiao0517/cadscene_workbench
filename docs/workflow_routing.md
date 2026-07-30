# 上传工作流与轨迹路由

Stage 6A 提供上传入口和自动轨迹工作流路由。它先保存并检查输入，再把检测结果写入数据集清单；路由结果不会替代人工标定、质量检查或对齐验证。

## 输入与上传顺序

- **视频**：必传。支持本地 MP4、MOV、AVI、MKV。
- **CAD**：必传。可上传 `design.json`、DXF、DWG 或包含 CAD assets 的 ZIP；只有 CAD 解析为可用 assets 后，数据集才可运行。
- **SRT 遥测**：选传。上传后仅做解析和能力检测，不上传也不影响已可用的无 SRT 流程。

门户会先创建数据集，再依次上传视频、CAD 和（可选）SRT。随后进入 Viewer；实际 SfM、关键帧人工标定、SfM-CAD 对齐和质量检查仍由既有流程执行。调试模式可以显示模式覆盖选项，但它不能把尚未实现的能力变成可执行流程。

## 自动路由结果

| `trajectory_mode` | 触发条件 | 当前状态 | 可执行含义 |
| --- | --- | --- | --- |
| `sfm_only` | 未上传 SRT、SRT 无法解析，或定位/高度覆盖不足 | `ready` | 已真实可用：沿用无 SRT 的 SfM、人工关键帧和 SfM-CAD 对齐流程。 |
| `srt_sfm_fused` | SRT 具备足够的 GPS 与高度轨迹，但不满足完整相机姿态条件 | `interface_only` | 只完成上传、分析、清单和界面提示；SRT/SfM 融合算法尚未启用，不能启动该路线。 |
| `srt_full_pose` | SRT 具备足够的 GPS、 高度与完整云台相机姿态 | `interface_only` | 只完成上传、分析、清单和界面提示；直接以 SRT 完整姿态驱动的流程尚未启用，不能启动该路线。 |

当模式为 `interface_only` 时，服务会阻止工作流阶段启动并提示功能待启用，避免把未实现接口误当成结果。无 SRT 的 `sfm_only` 才是当前可运行的稳定路径。

## 清单状态

数据集的 `dataset_manifest.json` 保存 `workflow.trajectory_mode`、`workflow.implementation_status` 和 SRT 分析摘要。SRT 检测异常会回退到 `sfm_only`；解析出的警告（例如时长不匹配）会保留在分析报告中，供人工复核。

详细的字段识别和阈值见 [SRT 能力检测](srt_capability_detection.md)。
# Stage 6B-1：Partial-SRT 融合核心

`srt_sfm_fused` 现有独立命令行融合核心，尚未接入正式 Job Runner。它通过 PTS 优先的帧时间表将 SRT 映射到 SfM 注册帧，在局部 ENU 中估计 SfM→SRT Sim3，再以 SRT 低频残差修正 SfM 位置；相机旋转始终来自 SfM。输出仍需进入既有人工关键帧与 `align_to_cad` 流程，不能把 SRT 默认视为高精度轨迹真值或 CAD 绝对高程。
