# Scene Bridge Restart Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 scene bridge 在发布、重试和服务重启后保持可验证，并消除 saved 工作台上的历史失效误报。

**Architecture:** 使用 `QueueJob.submission_operation_id` 作为请求的稳定身份；旧任务通过语义输入指纹安全查找请求。前端只在没有正式 saved 工作台结果时显示历史打通失效提示。

**Tech Stack:** Python 3、pytest、原生 JavaScript、JSON manifests

## Global Constraints

- 不修改 scene bridge、SfM 或工作台路线数学。
- 不自动接受语义输入指纹不一致的历史输出。
- 保持旧项目 manifest 可读取。

---

### Task 1: Stable scene bridge request identity

**Files:**
- Modify: `tests/projects/test_workbench_sessions.py`
- Modify: `cadscene/projects/service.py`

**Interfaces:**
- Consumes: `QueueJob.submission_operation_id`, `QueueJob.input_fingerprint`
- Produces: `_load_scene_bridge_request(job)` 在 publication `operation_id` 改变后仍返回同一语义请求

- [ ] 写一个成功发布后重新加载请求的失败测试，并断言新任务保存 `submission_operation_id`。
- [ ] 运行聚焦测试，确认因发布后的 operation ID 没有对应请求文件而失败。
- [ ] 创建任务时记录稳定提交 ID；加载请求时优先使用稳定 ID，并为旧任务按输入指纹安全回退。
- [ ] 运行 scene bridge 聚焦测试确认通过。

### Task 2: Saved workbench UI precedence

**Files:**
- Modify: `tests/viewer/test_project_workspace_static.py`
- Modify: `apps/project_workspace/project_workspace.js`

**Interfaces:**
- Consumes: snapshot `clip.workbench.state`, `clip.scene_bridge.status`
- Produces: saved 工作台不显示历史 bridge 失效提示

- [ ] 写前端失败测试，要求 bridge 错误分支排除 `workbench.state === "saved"`。
- [ ] 运行聚焦测试确认失败。
- [ ] 最小调整错误提示条件。
- [ ] 运行聚焦测试、完整测试和 `git diff --check`。
