# CADScene Workbench 文档体系刷新设计

## 背景

当前 `README.md` 同时包含旧英文发布说明和乱码中文内容，仍以
`v0.1.0-road-sfm-only` 为主叙事。它没有覆盖已经合入 `main` 的上传门户、
CUDA 可选后端、partial-SRT 融合核心、pure-rotation 实验工作流、
SfM-CAD 对齐修复和 `--storage-root`。现有 CHANGELOG、roadmap、workflow、
SRT、CUDA 和 viewer 文档也存在状态不一致。

本轮将文档整体更新，并按用户要求全部纳入 Git 和远程仓库。

## 目标读者

- 首次使用工作台的项目成员；
- 需要判断某条工作流是否可用的算法和产品人员；
- 调试 SfM、CAD 对齐、SRT 或 pure-rotation 的开发者；
- 维护 HTTP API、数据产物和前端工作流的工程人员。

## 信息架构

### 项目入口

- `README.md`：中文主入口；
- `README_EN.md`：英文主入口；
- `CHANGELOG.md`：面向版本和行为变化的新增功能、修复与限制；
- `docs/README.md`：文档中心导航。

两个 README 采用相同章节顺序：

1. 项目定位和语言切换；
2. 快速链接；
3. 输入、处理流程和输出；
4. 工作流状态矩阵；
5. Quick Start；
6. 工作流选择；
7. 数据与产物结构；
8. CLI 入口；
9. GPU 和运行边界；
10. 已知限制；
11. 开发与测试；
12. Roadmap、CHANGELOG、致谢和许可说明。

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
python -m pip install -e ".[sfm,cad,diagnostics,dev]"
python -m cadscene.cli.check_sfm_environment --json
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

入口统一为：

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

## README 展示约束

- 不添加不存在的 CI、coverage、release 或 license 徽章；
- 可以添加静态 Python 3.10+ 徽章；
- 仓库当前没有 LICENSE，必须明确“尚未声明开源许可证”，不能暗示开源授权；
- 不硬编码测试通过数量到徽章；开发章节只给测试命令；
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
