# 上传入口与轨迹工作流自动路由设计

## 目标

新增独立的本地上传入口：用户上传必需的视频和 CAD，以及可选的 DJI SRT。系统以保守规则识别轨迹能力，将结果写入数据集 manifest，并跳转到现有 viewer；稳定的 SfM、alignment 与 render 核心算法保持不变。

## 本阶段范围

- 新增纯解析和能力检测包 `cadscene.srt`。
- 在现有数据集导入与 HTTP workflow API 中增加 SRT 上传和分析读取。
- 新增无依赖的静态页面 `apps/workflow_portal/`。
- 保留既有 viewer 上传 API 与无 SRT 工作流行为。
- 仅新增最小化、合成的 SRT fixture 与自动化测试。

本阶段不包括：SRT/COLMAP 融合、SRT 直接生成相机轨迹、CUDA 后端、GPS 到 CAD 投影，以及新的质量评估算法。

## 架构

`workflow_portal` 通过现有 server 自动创建内部 dataset/run 标识，分别调用既有视频/CAD 上传接口和新的 SRT 上传接口。`cadscene.srt.parser` 以增量方式读取字幕记录并提取常见遥测字段别名；`cadscene.srt.capability` 基于解析结果生成保守的能力报告与 `trajectory_mode`。`cadscene.workflow.data_import` 将分析文件路径、归一化 SRT 状态和 workflow 状态写入 `dataset_manifest.json`。既有 viewer 从 manifest 读取 workflow 状态；URL 参数只用于兼容显示，不能覆盖 manifest。

## 模式判定

| 输入证据 | `trajectory_mode` | 实现状态 |
| --- | --- | --- |
| 未上传 SRT、SRT 无法解析、或无法取得有效 GPS 与高度覆盖 | `sfm_only` | `ready` |
| 有时间、GPS、高度；但无法确认完整相机/云台姿态 | `srt_sfm_fused` | `interface_only` |
| 有时间、GPS、高度，以及确认属于相机/云台的完整 yaw/pitch/roll | `srt_full_pose` | `interface_only` |

只有 drone 飞行器姿态绝不能判定为完整姿态。解析器先统一经纬度、高度、云台姿态、相机姿态和飞行器姿态的字段别名，再判断能力。检测采用保守覆盖率阈值；若 GPS 覆盖不足或 SRT 时长与已知视频时长明显不一致，写入 warning。SRT 解析失败不得破坏已成功上传的视频和 CAD，也不得阻塞无 SRT 工作流。

## API 与 manifest 契约

- `POST /api/workflow/upload-srt?dataset=<dataset>`：仅接受大小写不敏感的 `.srt`；流式保存至 `data/<dataset>/telemetry/<安全文件名>`；写出 `srt_analysis.json` 与 `srt_analysis_report.md`；返回归一化状态、模式、分析结果和用户可读消息。
- `GET /api/workflow/srt-analysis?dataset=<dataset>`：返回已保存的分析结果。
- manifest 新增 `srt`：记录可用性、安全相对路径、分析文件、原始文件名、覆盖率、姿态类别和 warnings。
- manifest 新增 `workflow`：记录自动检测模式、可选 debug override 和实现状态。未上传 SRT 时必须明确为 `sfm_only` 与 `ready`。

## 用户体验

普通模式的 portal 只展示视频（必传）、CAD（必传）、SRT（选填）、各文件独立进度/状态、检测到的中文模式名称、简短说明和“进入项目”按钮。界面不展示 dataset/run ID、缩放/原点、后端参数或导入资产格式。`debug=1` 可显示内部标识与受限制的 override。视频与 CAD ready 后按钮即可启用，SRT 的任何非失败状态都不阻塞。进入项目后打开现有 viewer，并带上 dataset/run/mode 显示提示；partial/full 必须显示为待启用接口，不能调用未实现算法。

## 测试与安全

先写测试，覆盖解析模式、别名识别、异常 SRT、warnings、manifest 写入、上传端点、portal 静态约束及无 SRT API 兼容。上传继续使用现有 multipart 流式读取和文件名/路径校验。不得提交用户 SRT、视频、CAD、`project/`、`data/` 或 `runs/` 产物。
