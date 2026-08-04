from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKSPACE = ROOT / "apps" / "project_workspace"


def test_workspace_sidebar_has_exactly_the_four_approved_destinations() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")

    assert html.count("data-nav=") == 4
    for label in ("项目概览", "项目文件", "片段管理", "设置"):
        assert label in html
    assert "任务队列" not in html
    assert "渲染导出" not in html
    assert 'id="sidebarToggle"' in html
    assert 'aria-label="收起侧栏"' in html


def test_workspace_uses_friendly_clip_columns_and_only_one_merge_action() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert "场景 01 · 第 1 段" in html
    assert "时间范围" in html and "时长" in html
    assert "推荐工作流" in html and "最终工作流" in html
    assert "当前状态" in html and "进度" in html
    assert "源 PTS" not in html
    assert html.count('data-action="merge-project"') == 1
    assert ".workflow-recommendation" in css
    assert "font-size: var(--font-size-body)" in css


def test_polling_uses_etag_and_preserves_dirty_edits_and_selection() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "const POLL_INTERVAL_MS = 1500" in script
    assert '"If-None-Match"' in script
    assert "response.status === 304" in script
    assert "dirtyEdits" in script
    assert "selectedClipIds" in script
    assert "capabilities" in script
    assert "workflow_override: null" in script
    assert "dirtyEdits.has(clip.clip_id)" in script
    assert "edit.name === null ? clip.generated_display_name" in script


def test_workflow_portal_redirects_new_uploads_to_project_workspace() -> None:
    script = (ROOT / "apps" / "workflow_portal" / "workflow_portal.js").read_text(
        encoding="utf-8"
    )

    assert 'new URL("/apps/project_workspace/"' in script
    assert 'target.searchParams.set("projectId", state.dataset)' in script
    assert "state.projectRevision = 0" in script
    assert "state.manifest = null" in script


def test_preflight_shows_per_clip_reasons_and_confirms_only_checked_subset() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="preflightItems"' in html
    assert "body.reasons[clipId]" in script
    assert 'data-confirm-clip-id' in script
    assert "confirmedClipIds" in script
    assert "confirmed_clip_ids: confirmedClipIds" in script


def test_workspace_wires_reanalysis_retry_and_cancel_to_real_api_routes() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "/analysis/start" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/retry`" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/cancel`" in script
    assert '#reanalyzeButton").addEventListener' in script
