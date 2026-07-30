# CADScene Workbench 文档体系刷新设计

## 背景

当前 `README.md` 同时包含旧英文发布说明和乱码中文内容，仍以
`v0.1.0-road-sfm-only` 为主叙事。它没有覆盖已经合入 `main` 的上传门户、
CUDA 可选后端、partial-SRT 融合核心、pure-rotation 实验工作流、
SfM-CAD 对齐修复和 `--storage-root`。现有 CHANGELOG、roadmap、workflow、
SRT、CUDA 和 viewer 文档也存在状态不一致。

本轮将文档整体更新，并按用户要求全部纳入 Git 和远程仓库。

## 目标读者

- 主要读者是建筑、道路等领域的设计人员，默认不具备计算机或三维视觉背景；
- 次要读者是负责部署、故障排查和二次开发的工程人员；
- README 优先帮助设计人员完成真实任务，不以代码结构、测试体系或算法名词组织内容；
- 面向工程人员的细节集中放在 `docs/technical/`，不打断 README 的使用主线。

## 信息架构

### 项目入口

- `README.md`：中文主入口；
- `README_EN.md`：英文主入口；
- `CHANGELOG.md`：面向版本和行为变化的新增功能、修复与限制；
- `docs/README.md`：文档中心导航。

两个 README 采用相同章节顺序：

1. 项目定位和语言切换；
2. 适用场景与开始前准备；
3. 输入、处理流程和输出；
4. 快速开始；
5. 如何选择工作流；
6. 从上传到结果导出的操作流程；
7. 运行时间、GPU、FOV、SRT 等用户常见问题；
8. 工作流能力与限制；
9. 结果文件和故障排查入口；
10. 面向维护人员的技术文档入口；
11. Roadmap、CHANGELOG、致谢和许可说明。

README 中不展开模块清单、完整 CLI 目录、测试方法或内部 API。开发安装、
命令行批处理、模块架构、测试和产物协议统一由 `docs/technical/` 承载。
README 只保留启动工作台所需的最短命令，以及一个“维护与二次开发”链接。

README 参考成熟项目的克制风格：COLMAP 的清晰定位与 Getting Started、
nerfstudio 的 Quick Start 和功能入口、OpenMVS 的输入输出边界、Open3D 的
模块导航。不得复制其具体文案。

### 设计文档

- `docs/design/system-architecture.md`：系统组件、前端、工作流状态机、
  路由模式、依赖边界和失败恢复；
- `docs/design/coordinates-and-alignment.md`：SfM/CAD/Web/ENU 坐标，
  Sim3、关键帧、FOV 优先级、可观测性和对齐验证。

### 技术文档

- `docs/technical/developer-guide.md`：安装、模块、CLI、开发测试和运行环境；
- `docs/technical/api-and-artifacts.md`：HTTP API、manifest、`data/` 和
  `runs/` 目录、阶段产物及路径安全；
- `docs/technical/troubleshooting.md`：上传、CUDA、全局 BA、CAD、FOV、
  SRT、pure-rotation 和渲染故障排查，以及本轮重要 bug 修复索引。

### 现有文档同步

更新以下事实来源，避免文档互相矛盾：

- `docs/roadmap.md`
- `docs/sfm_cuda_backend.md`
- `docs/workflow_routing.md`
- `docs/srt_capability_detection.md`
- `docs/pipeline_usage.md`
- `docs/web_viewer_usage.md`

历史 stage 文档和 `docs/superpowers/{plans,specs}` 保留为设计记录，但
`docs/README.md` 不把它们作为当前用户操作说明。

## 功能状态用语

所有文档使用统一状态：

| 能力 | 状态 | 文档边界 |
|---|---|---|
| `sfm_only` | Stable | 当前唯一稳定的端到端主路径 |
| `pure_rotation` | Experimental | 可执行；固定相机中心，不恢复平移；依赖外部 OpenGV 后端 |
| partial-SRT core | Experimental CLI | PTS、ENU、robust Sim3 和融合核心可用；尚未接入正式 JobRunner |
| `srt_sfm_fused` portal route | Interface only | 可识别和提示，正式 workflow 启动被阻止 |
| `srt_full_pose` | Interface only | 尚无端到端执行 |
| SfM CUDA | Optional | 只确认特征提取和匹配；mapper/global BA 不宣传为 GPU |

不得写入未合入 `main` 的 rank1 方案。

## 技术事实约束

- 默认 SfM 是 `pycolmap + cpu`；CUDA 需要显式选择并通过能力检测；
- CUDA 无法确认时回退 CPU，并在日志和摘要中记录；
- FOV 不能宣传为 SfM 必然准确：一致的已确认手动关键帧 FOV 优先；
- ordinary SRT 只是元数据能力来源，不是高精度位置、姿态或 CAD 高程真值；
- pure-rotation 不恢复相机平移、尺度或自动判断运动模式；
- DWG 是否 ready 取决于外部转换工具；
- `--root` 提供静态资源，`--storage-root` 提供 workflow 数据和产物；
- 当前测试覆盖单元、静态契约和合成集成，不等同于真实 GPU、OpenGV、
  大规模视频和全部设备验证。

## Quick Start 约束

README 使用通用命令，不引用私有数据集、硬编码 CAD 比例或真实坐标：

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

安装依赖和环境检查移至开发者指南；README 的快速开始假定应用已经由维护人员
部署完成，并使用非专业人员能够理解的界面操作步骤解释上传和工作流选择。

入口统一为：

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

## README 展示约束

- 不添加不存在的 CI、coverage、release 或 license 徽章；
- 可以添加静态 Python 3.10+ 徽章；
- 仓库当前没有 LICENSE，必须明确“尚未声明开源许可证”，不能暗示开源授权；
- 不硬编码测试通过数量到徽章；README 不设置独立“开发与测试”章节；
- 算法名首次出现时同时给出业务含义，例如“SfM（三维重建）”；
- 优先使用界面名称、按钮名称和结果名称，CLI、JSON 字段和目录协议不进入主流程；
- 没有可长期维护的截图资产时不制造占位图片。

## CHANGELOG 范围

`Unreleased` 至少覆盖：

- 上传门户与工作流路由；
- SRT 能力检测与 partial-SRT core；
- pure-rotation 实验工作流；
- pycolmap/COLMAP CLI 与可选 CUDA、CPU fallback、BA 边界；
- SfM-CAD 基线、FOV 和退化结果验证；
- 关键帧、viewer、Unicode 上传、工作流按钮和 `--storage-root` 修复；
- 仍未完成的 full-pose 和正式 SRT workflow 集成。

## 验证

- Markdown 链接指向仓库内存在文件或有效外部项目；
- 中文和英文 README 章节、状态矩阵、命令和限制保持一致；
- README、CHANGELOG、roadmap 与代码状态不矛盾；
- 对全部 Markdown 执行占位符、乱码、合并冲突标记和相对链接扫描；
- 运行文档相关静态测试和完整 pytest；
- 由独立审查员分别复核技术事实、中英文一致性和最终 Git diff。

## 非目标

- 本轮不修改任何运行代码、算法、API 或前端行为；
- 不增加 MkDocs、CI、文档部署和图片资产；
- 不选择或新增 LICENSE；
- 不承诺尚未完成的 SRT、GPU BA、自动运动分类或 rank1 功能。
