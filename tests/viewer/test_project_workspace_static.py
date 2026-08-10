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


def test_workflow_portal_waits_for_analysis_before_navigating_and_has_no_hover_choice() -> None:
    html = (ROOT / "apps" / "workflow_portal" / "index.html").read_text(
        encoding="utf-8"
    )
    script = (ROOT / "apps" / "workflow_portal" / "workflow_portal.js").read_text(
        encoding="utf-8"
    )

    assert "无人机悬停，仅转动视角" not in html
    assert 'id="analysisOverlay"' in html
    assert "waitForAnalysisCompletion" in script
    assert "/snapshot" in script
    assert "analysis_failed" in script
    assert "window.location.assign(target.toString())" in script


def test_workspace_uses_icon_sidebar_video_thumbnails_and_progress_track() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert "<svg" in html
    assert 'class="source-video-thumb"' in html
    assert 'class="clip-thumbnail"' in html
    assert "thumbnail_url" in script
    assert ".sidebar-label" in css and "max-width" in css
    assert ".progress-track" in css


def test_workspace_progress_uses_reported_percentage_or_honest_indeterminate_bar() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'class="progress-percent"' in html
    assert "Math.round(fraction * 100)" in script
    assert 'progressPercent.textContent = `${percent}%`' in script
    assert 'progressPercent.textContent = "—"' in script
    assert 'progressTrack.classList.toggle("progress-indeterminate"' in script
    assert "progress-indeterminate" in css
    assert "progress-sweep" in css


def test_workspace_reserves_one_hundred_percent_for_terminal_success() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'clip.status === "success"' in script
    assert "? 100" in script
    assert "Math.min(99" in script


def test_sidebar_project_name_supports_inline_blur_rename() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'id="projectRenameButton"' in html
    assert 'id="sidebarProjectNameInput"' in html
    assert "async function saveProjectRename" in script
    assert "function startProjectRename" in script
    assert 'addEventListener("blur", saveProjectRename)' in script
    assert 'event.key === "Enter"' in script
    assert 'event.key === "Escape"' in script
    assert "state.projectNameEditing" in script
    assert "expected_revision: state.snapshot.component_revisions.project" in script
    assert "/api/projects/${encodeURIComponent(projectId)}`" in script
    assert ".project-rename-button" in css


def test_workspace_uses_product_logo_favicon_and_centered_collapsed_navigation() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'rel="icon" href="/apps/workflow_portal/assets/mediaflow-cad-video-logo.svg"' in html
    assert 'class="brand-logo" src="/apps/workflow_portal/assets/mediaflow-cad-video-logo.svg"' in html
    assert (ROOT / "apps/workflow_portal/assets/mediaflow-cad-video-logo.svg").is_file()
    assert ".app-shell.sidebar-collapsed .nav-item" in css
    assert "grid-template-columns: 24px 0fr" in css
    assert "transition: grid-template-columns" in css


def test_upload_workspace_and_workbench_share_the_product_favicon() -> None:
    favicon = '<link rel="icon" href="/apps/workflow_portal/assets/mediaflow-cad-video-logo.svg" type="image/svg+xml">'
    pages = (
        ROOT / "apps" / "workflow_portal" / "index.html",
        WORKSPACE / "index.html",
        ROOT / "apps" / "web_camera_viewer" / "index.html",
    )

    for page in pages:
        assert favicon in page.read_text(encoding="utf-8")


def test_preflight_shows_per_clip_reasons_and_confirms_only_checked_subset() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="preflightItems"' in html
    assert "body.reasons[clipId]" in script
    assert 'data-confirm-clip-id' in script
    assert "confirmedClipIds" in script
    assert "confirmed_clip_ids: confirmedClipIds" in script


def test_final_workflow_selector_only_exposes_sfm_and_opengv() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    workflow_select = html.split('class="workflow-select"', 1)[1].split(
        "</select>", 1
    )[0]
    assert workflow_select.count("<option") == 2
    assert '<option value="sfm_only">三维重建（SfM）</option>' in workflow_select
    assert '<option value="pure_rotation">旋转估计（OpenGV）</option>' in workflow_select
    assert "使用系统推荐" not in workflow_select
    assert "function visibleWorkflowChoice(clip, edit)" in script
    assert "clip.resolved_workflow" in script


def test_workspace_wires_reanalysis_retry_and_cancel_to_real_api_routes() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "/analysis/start" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/retry`" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/cancel`" in script
    assert '#reanalyzeButton").addEventListener' in script


def test_workspace_wires_batch_render_to_render_preflight_api() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="batchRenderButton"' in html
    assert '"render-jobs"' in script
    assert '#batchRenderButton").addEventListener' in script
    assert "snapshot.capabilities.can_render" in script


def test_workspace_opens_server_session_and_focuses_returning_clip() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "async function openWorkbench" in script
    assert "/workbench-sessions`" in script
    assert "expected_revision: state.snapshot.component_revisions.clips" in script
    assert "return_to:" in script
    assert "window.location.assign(body.workbench_url)" in script
    assert 'params.get("focusClip")' in script
    assert "scrollIntoView" in script
    assert '.open-workbench", row).addEventListener' in script


def test_ready_clip_prepares_inputs_then_opens_workbench_with_chinese_status() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="workbenchPreparationDialog"' in html
    assert "can_prepare_workbench" in script
    assert "expected_jobs_revision: state.snapshot.component_revisions.jobs" in script
    assert "response.status === 202" in script
    assert "waitForWorkbenchPreparation" in script
    for source, translated in (
        ("ready", "待处理"),
        ("queued", "排队中"),
        ("preparing", "准备输入"),
        ("running", "处理中"),
        ("validating", "验证结果"),
        ("success", "已完成"),
        ("failed", "失败"),
    ):
        assert f'{source}: "{translated}"' in script
