# CAD Replacement Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让完成全局 CAD 替换的项目在服务重启时，使用不可变分析输入快照验证旧分析任务并恢复队列。

**Architecture:** 保持当前分析契约和 fail-closed 校验不变。仅当旧 identity 无法由当前资产验证时，按 video analysis job 精确定位 `_analysis_revisions` 快照并再次验证；验证成功后继续使用现有迁移逻辑重写为当前契约。

**Tech Stack:** Python 3.11、dataclasses、pytest、原子 JSON repositories。

## Global Constraints

- 不修改人工验收项目 manifest。
- 不重跑 CAD/视频分析，不修改 SfM、Pure Rotation、渲染或 CAD 替换算法。
- 快照缺失、request key 不一致或 identity 不匹配时继续拒绝恢复。
- main 的未提交 Pure Rotation、SOP 和周报修改不得进入本分支。

---

### Task 1: CAD 替换后的旧分析任务恢复

**Files:**
- Modify: `tests/projects/test_analysis_jobs.py`
- Modify: `cadscene/projects/service.py:4004-4109`

**Interfaces:**
- Consumes: `ProjectService._migrate_legacy_analysis_jobs_locked(project_id, manifest)`、`_analysis_revisions` descriptor、`QueueJob`。
- Produces: `ProjectService._legacy_analysis_snapshot_assets(project_assets, video_job, request_key) -> Mapping[str, object] | None`。

- [ ] **Step 1: 写入失败回归测试**

```python
def test_restore_migrates_legacy_analysis_from_snapshot_after_cad_replacement(
    tmp_path: Path,
) -> None:
    service, repositories, queue = _service(tmp_path)
    job_ids, revision = _complete_analysis(service, repositories, queue, tmp_path)
    before = repositories.project.load("p1")
    current_jobs = tuple(queue.get(job_id) for job_id in job_ids)
    legacy_jobs = tuple(
        _as_legacy_analysis_job(item, before.source_assets)
        for item in current_jobs
    )
    _persist_jobs(repositories, "p1", legacy_jobs)
    replacement = tmp_path / "replacement.dxf"
    replacement.write_text("replacement", encoding="utf-8")
    repositories.project.update(
        "p1",
        expected_revision=before.revision,
        mutate=lambda value: replace(
            value,
            source_assets={
                **value.source_assets,
                "cad": {
                    **value.source_assets["cad"],
                    "path": str(replacement),
                    "sha256": "c" * 64,
                },
            },
        ),
    )
    restarted_queue = LocalResourceQueue()
    restarted = ProjectService(
        repositories,
        restarted_queue,
        default_workflow_adapters(),
        projects_root=tmp_path / "projects",
        now=lambda: "restart",
    )

    restarted.restore_jobs("p1", process_probe=lambda _pid: None)

    restored = tuple(restarted_queue.get(job_id) for job_id in job_ids)
    assert all(job.status == "success" for job in restored)
    assert all(
        restarted._current_input_fingerprint(job) == job.input_fingerprint
        for job in restored
    )
    project = repositories.project.load("p1")
    assert project.active_analysis_revision == revision
    assert project.project_state == "ready"
```

- [ ] **Step 2: 运行测试并确认 RED**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_analysis_jobs.py::test_restore_migrates_legacy_analysis_from_snapshot_after_cad_replacement -q`

Expected: FAIL，错误包含 `legacy analysis identity does not match exactly`。

- [ ] **Step 3: 实现精确快照回退校验**

```python
@staticmethod
def _legacy_analysis_snapshot_assets(
    project_assets: Mapping[str, object],
    *,
    video_job: QueueJob,
    request_key: str,
) -> Mapping[str, object] | None:
    revisions = project_assets.get("_analysis_revisions")
    if not isinstance(revisions, Mapping):
        return None
    descriptor = revisions.get(f"analysis-{video_job.job_id}")
    if not isinstance(descriptor, Mapping):
        return None
    snapshot = descriptor.get("input_snapshot")
    if not isinstance(snapshot, Mapping) or snapshot.get("request_key") != request_key:
        return None
    if any(not isinstance(snapshot.get(name), Mapping) for name in ("video", "cad")):
        return None
    if snapshot.get("srt") is not None and not isinstance(snapshot.get("srt"), Mapping):
        return None
    return snapshot
```

在 legacy fingerprint 校验处，先用当前资产校验；不匹配时仅尝试上述精确快照。两者都不匹配则保留原异常。成功后仍将 job identity 更新为 `current_contract`。

- [ ] **Step 4: 运行聚焦测试并确认 GREEN**

Run: `python -m pytest -p no:cacheprovider tests/projects/test_analysis_jobs.py -q`

Expected: 全部通过。

- [ ] **Step 5: 提交实现**

```powershell
git add cadscene/projects/service.py tests/projects/test_analysis_jobs.py
git commit -m "fix: restore analysis jobs after CAD replacement"
```

### Task 2: 回归验证与真实项目启动

**Files:**
- Verify only: `cadscene/projects/service.py`
- Verify only: `tests/projects/test_analysis_jobs.py`

**Interfaces:**
- Consumes: Task 1 的恢复行为。
- Produces: 可运行的 `serve_viewer` 和六片段 hygs 项目页面。

- [ ] **Step 1: 运行全量测试与静态门禁**

Run:

```powershell
python -m pytest -p no:cacheprovider
python scripts/check_no_project_dependency.py
git diff --check
```

Expected: 1442 个既有测试、1 个新增测试全部通过；依赖扫描和差异检查通过。

- [ ] **Step 2: 从修复 worktree 启动真实服务**

Run:

```powershell
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8602 --storage-root D:\zjic2026\cadscene_workbench\work\stage9_manual_acceptance\20260813-121634
```

Expected: 8602 开始监听，stderr 无恢复异常。

- [ ] **Step 3: 验证真实项目 API**

请求 `/api/projects/dataset-1460343f-192d-4fa7-b90b-ab809138d515/snapshot`，确认 HTTP 200、project id 正确且 clip 数为 6。

- [ ] **Step 4: 打开项目管理页面**

打开：`http://127.0.0.1:8602/apps/project_workspace/?projectId=dataset-1460343f-192d-4fa7-b90b-ab809138d515`。
