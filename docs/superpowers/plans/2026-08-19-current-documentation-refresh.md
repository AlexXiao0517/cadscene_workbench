# Current Workflow Documentation Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让所有现行入口文档准确描述 `main@a53fbd1` 已交付的项目管线、工程标牌、全局 CAD 替换和恢复行为。

**Architecture:** 保持“快速入口—文档导航—设计—技术参考—排障”的现有分层，只更新现行文档；历史计划、规格和 SOP 不改写。所有描述以代码、自动化测试和真实 hygs 六片段项目启动结果为依据。

**Tech Stack:** Markdown、Python 3.11、pytest、Git。

## Global Constraints

- 中英文 README 对齐，不宣传未开放的功能。
- 当前界面只开放 `cad_anchor` 工程标牌；`video_track` 创建入口仍隐藏。
- 不修改源代码、测试逻辑、项目 manifest 或人工测试数据。
- 保留 source decoded-frame integer PTS、exact time base 和 frame-map 契约。
- 历史 `docs/superpowers/` 与 `docs/SOP/` 仅新增本计划和已批准设计，不改写旧记录。

---

### Task 1: 用户入口与文档导航

**Files:**
- Modify: `README.md`
- Modify: `README_EN.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: 工作流门户 `/apps/workflow_portal/`、项目页 `/apps/project_workspace/?projectId=...`。
- Produces: 面向用户的一致流程、能力边界和技术文档入口。

- [ ] **Step 1: 更新中文 README**

补充项目片段管理、批量轨迹/渲染/合并、CAD 原图文字、CAD 锚定工程标牌、全局 CAD 替换和旧项目恢复；将正式启动示例改为显式 `--storage-root`，说明无标签仍可渲染。

- [ ] **Step 2: 同步英文 README**

按中文 README 的事实边界同步完整流程；使用 `CAD-anchored engineering callout`、`global CAD replacement`、`source decoded-frame integer PTS` 等一致术语。

- [ ] **Step 3: 更新文档中心**

在 `docs/README.md` 增加项目管线、标牌、CAD 替换、恢复和数据目录的导航说明，并明确历史设计记录不是运行手册。

- [ ] **Step 4: 检查入口一致性并提交**

Run:

```powershell
rg -n "workflow_portal|project_workspace|storage-root|CAD|标牌|callout" README.md README_EN.md docs/README.md
git diff --check
```

Expected: 三份文档都包含当前入口，且 `git diff --check` 无输出。

Commit:

```powershell
git add README.md README_EN.md docs/README.md
git commit -m "docs: update user workflow and project entry points"
```

### Task 2: 架构、开发、API 与故障排查

**Files:**
- Modify: `docs/design/system-architecture.md`
- Modify: `docs/technical/developer-guide.md`
- Modify: `docs/technical/api-and-artifacts.md`
- Modify: `docs/technical/troubleshooting.md`

**Interfaces:**
- Consumes: `cadscene.projects`, `cadscene.annotations`, `serve_viewer` Project API 和五类 manifest。
- Produces: 可部署、可维护、可恢复的当前系统说明。

- [ ] **Step 1: 扩展系统架构**

加入项目领域、持久队列、工作台不可变输出、标牌预览/渲染、CAD 替换和 Stage 8 concat；更新状态图和恢复规则，区分旧 dataset/run 与当前 project 路径。

- [ ] **Step 2: 更新开发者指南**

给出静态根与可写存储根分离的启动示例、`projects/<project_id>` 目录树、领域模块职责、测试命令和单服务租约约束。

- [ ] **Step 3: 更新 API 与产物参考**

记录 Project API 的项目/上传/分析/片段/作业/会话/标牌/渲染/合并/CAD 替换端点类别，说明 expected revision、operation ID、不可变 revision、render frame map 和 source PTS 契约。

- [ ] **Step 4: 更新故障排查**

增加旧项目加载失败、active workbench session、队列 pending/进度、CAD 替换、标牌预览/烧录和替换后重启身份恢复的诊断步骤；不建议手工改 manifest。

- [ ] **Step 5: 检查技术术语并提交**

Run:

```powershell
rg -n "projects/<project_id>|annotations_manifest|render_frame_map|expected_revision|CAD 替换|source PTS" docs/design docs/technical
git diff --check
```

Expected: 设计和技术文档覆盖上述契约，且 `git diff --check` 无输出。

Commit:

```powershell
git add docs/design/system-architecture.md docs/technical/developer-guide.md docs/technical/api-and-artifacts.md docs/technical/troubleshooting.md
git commit -m "docs: document project architecture and recovery contracts"
```

### Task 3: 版本状态与最终校验

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/roadmap.md`

**Interfaces:**
- Consumes: Tasks 1–2 的当前能力边界。
- Produces: 已交付、实验性、隐藏和未来能力的唯一一致状态。

- [ ] **Step 1: 更新 Changelog 与 Roadmap**

把项目管线、DXF 文字、CAD 工程标牌、CAD 替换和恢复列为已交付；把 `pure_rotation` 保持为实验性，把视频目标跟踪创建入口、跨片段跟踪、真实遮挡和 SRT 正式路线保留为未开放/未来工作。

- [ ] **Step 2: 校验 Markdown 本地链接**

运行一个只读 Python 检查器，扫描本轮修改 Markdown 的相对链接；忽略 `http(s)`、锚点和示例参数，所有本地目标必须存在。

- [ ] **Step 3: 运行项目验证**

Run:

```powershell
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: 测试全绿、依赖扫描报告没有禁止依赖、`git diff --check` 无输出。

- [ ] **Step 4: 提交版本文档**

```powershell
git add CHANGELOG.md docs/roadmap.md
git commit -m "docs: record current delivered capabilities"
```
