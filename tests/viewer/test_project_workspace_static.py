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


def test_workspace_sidebar_opens_project_library_and_uses_original_icons() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'data-nav="files"' in html
    assert 'querySelector(\'[data-nav="files"]\')' in script
    assert 'window.location.assign("/apps/project_library/")' in script
    assert 'd="M4 6.5h6l2 2h8v10.5H4z"' in html
    assert "M19.4 15a1.7 1.7" in html
    assert '<span aria-hidden="true">«</span>' in html
    assert "M3.5 7.5h6l2-2h3l2 2h4" not in html
    assert "M12 2.8v2.1" not in html
    assert 'd="m14 6-6 6 6 6"' not in html
    assert ".app-shell.sidebar-collapsed .sidebar-toggle span:first-child" in css
    assert ".project-name-row:hover .project-rename-button" in css
    assert ".project-name-row:focus-within .project-rename-button" in css


def test_workspace_sidebar_theme_toggle_reuses_the_global_preference() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="sidebarThemeToggle"' in html
    assert 'class="sun-icon"' in html
    assert 'class="moon-icon"' in html
    assert html.index('id="sidebarThemeToggle"') < html.index('class="sidebar-project')
    for contract in (
        'const THEME_STORAGE_KEY = "mediaflow-theme"',
        'document.documentElement.setAttribute("data-theme", theme)',
        "window.localStorage.getItem(THEME_STORAGE_KEY)",
        "window.localStorage.setItem(THEME_STORAGE_KEY, theme)",
        'window.matchMedia?.("(prefers-color-scheme: light)")',
        'setAttribute("aria-pressed", String(isLight))',
    ):
        assert contract in script


def test_workspace_light_theme_covers_the_full_application_shell() -> None:
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    for contract in (
        ':root[data-theme="light"]',
        "--bg: #f5f8fc",
        "--surface: #ffffff",
        "--line: #cad7e5",
        "background: var(--page-background)",
        "background: var(--sidebar-background)",
        "background: var(--topbar-background)",
        "background: var(--panel-background)",
        ".sidebar-theme-toggle",
        ':root[data-theme="light"] .sidebar-theme-toggle .sun-icon',
        ".app-shell.sidebar-collapsed .sidebar-theme-toggle",
        ".button.primary { color: #fff;",
        ".cad-file-field { display: grid; gap: 8px; margin: 18px 0; color: var(--text);",
        ".cad-coordinate-confirmation { display: flex; align-items: flex-start; gap: 9px; color: var(--text);",
    ):
        assert contract in css


def test_workspace_uses_friendly_clip_columns_and_only_one_merge_action() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert "场景 01 · 第 1 段" in html
    assert "时间范围" in html and "时长" in html
    assert "检测模式" not in html
    assert 'class="motion-mode"' not in html
    assert 'class="confidence"' not in html
    assert "推荐工作流" in html and "最终工作流" in html
    assert "当前状态" in html and "进度" in html
    assert "源 PTS" not in html
    assert html.count('data-action="merge-project"') == 1
    assert ".workflow-recommendation" in css
    assert "font-size: var(--font-size-body)" in css
    assert 'sfm_only: "三维重建"' in script
    assert 'srt_fixed_track_visual_pose: "SRT 轨迹 + 稀疏重建姿态"' in script
    assert 'srt_sfm_fused: "SRT 定位 + 三维重建（实验）"' not in script
    assert 'srt_full_pose: "SRT 全姿态（跳过三维重建）"' in script
    assert 'pure_rotation: "旋转估计"' in script
    assert 'WORKFLOW_LABELS[clip.recommended_workflow]' in script
    assert 'clip.recommended_workflow || "需人工确认"' not in script


def test_polling_uses_etag_and_preserves_dirty_edits() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "const POLL_INTERVAL_MS = 1500" in script
    assert '"If-None-Match"' in script
    assert "response.status === 304" in script
    assert "dirtyEdits" in script
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
    assert '<input id="sidebarProjectName"' in html
    assert 'id="sidebarProjectNameInput"' not in html
    assert "readonly" in html[html.index('<input id="sidebarProjectName"') :]
    assert "async function saveProjectRename" in script
    assert "function startProjectRename" in script
    assert 'addEventListener("blur", saveProjectRename)' in script
    assert 'event.key === "Enter"' in script
    assert 'event.key === "Escape"' in script
    assert "state.projectNameEditing" in script
    assert '$holder.removeAttribute("readonly")' in script
    assert '$holder.setAttribute("readonly", "")' in script
    assert '$("#sidebarProjectNameInput")' not in script
    assert "expected_revision: state.snapshot.component_revisions.project" in script
    assert "/api/projects/${encodeURIComponent(projectId)}`" in script
    assert ".project-rename-button" in css
    assert ".project-name-field" in css


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


def test_batch_actions_keep_selection_and_cancel_without_second_clip_confirmation() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="selectAll"' in html
    assert 'class="clip-select"' in html
    assert 'id="preflightDialog"' in html
    assert 'id="cancelPreflight"' in html
    assert "selectedClipIds" in script
    assert "function updateBatchSelectionControls" in script
    assert "const clipIds = [...selectedClipIds]" in script
    assert "if (!clipIds.length)" in script
    assert "至少选择一个片段" in script
    assert "selectedClipIds.size === 0" in script
    assert ": state.snapshot.clips.map((clip) => clip.clip_id)" not in script
    assert "async function preflightBatch" in script
    assert "async function enqueuePreflight" in script
    assert "body.reasons[clipId]" in script
    assert 'data-confirm-clip-id' not in script
    assert "confirmed_clip_ids: pending.needsConfirmation" in script
    assert '#cancelPreflight").addEventListener' in script


def test_final_workflow_selector_preserves_all_four_workflows() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    workflow_select = html.split('class="workflow-select"', 1)[1].split(
        "</select>", 1
    )[0]
    assert workflow_select.count("<option") == 4
    assert '<option value="sfm_only">三维重建（SfM）</option>' in workflow_select
    assert '<option value="srt_fixed_track_visual_pose">SRT 轨迹 + 稀疏重建姿态</option>' in workflow_select
    assert '<option value="srt_sfm_fused">' not in workflow_select
    assert '<option value="srt_full_pose">SRT 全姿态（跳过三维重建）</option>' in workflow_select
    assert '<option value="pure_rotation">旋转估计（OpenGV）</option>' in workflow_select
    assert "使用系统推荐" not in workflow_select
    assert "function visibleWorkflowChoice(clip, edit)" in script
    assert "clip.resolved_workflow" in script
    assert 'workflow === "pure_rotation" ? "pure_rotation" : "sfm_only"' not in script


def test_workspace_explains_srt_route_with_server_coverage() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="srtCoverageSummary"' in html
    assert "function formatSrtCoverage" in script
    assert "trajectory_coverage" in script
    assert "full_pose_coverage" in script
    assert "定位/高度覆盖" in script
    assert "完整姿态覆盖" in script


def test_workspace_lists_optional_terrain_sources_and_explains_fallback() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="terrainSourceCount"' in html
    assert 'id="terrainSourceSummary"' in html
    assert "terrain_sources" in script
    assert "未上传高程文件，姿态解算后仍可通过关键帧微调与路线拟合" in html


def test_full_pose_dialog_uses_one_horizontal_fov_and_explicit_crs_confirmation() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'id="fullPoseDialog"' in html
    assert "水平视场角（°）" in html
    assert 'id="horizontalFovInput"' in html
    assert 'id="centralMeridianInput"' in html
    assert "CGCS2000 中央经线" in html
    assert 'id="horizontalFovInput" type="number"' in html
    assert 'id="horizontalFovInput" type="number" min="2" max="178" step="1"' in html
    assert 'placeholder="例如 72"' in html
    assert 'id="centralMeridianInput" type="text" inputmode="decimal"' in html
    assert 'placeholder="例如 120 或 118°50′；留空自动推荐"' in html
    assert "fovType" not in html and "FOV 类型" not in html
    assert 'id="cadGeoreferenceCandidates"' in html
    assert 'id="cadGeoreferencePreview"' in html
    assert "/cad-georeference/candidates" in script
    assert "/cad-georeference/confirm" in script
    assert "/srt-full-pose`" in script
    assert "cad_axis_mapping" in script
    assert "central_meridian_deg" in script
    assert "project_revision" in script
    assert "expected_revision: state.snapshot.component_revisions.project" in script
    assert "120°是本项目推荐中央经线，不会应用到其他 CAD" in script
    assert "trajectory_polyline_raw" in script
    assert ".full-pose-dialog" in css
    assert ".crs-candidate" in css


def test_srt_horizontal_fov_rounds_historical_and_submitted_values() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "Math.round(Number(settings.horizontal_fov_deg))" in script
    assert "const horizontalFov = Math.round(Number(fovInput.value));" in script
    assert "请输入 2° 到 178° 之间的整数水平视场角" in script


def test_full_pose_dialog_accepts_degree_minute_central_meridian() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="centralMeridianInput" type="text"' in html
    assert "例如 120 或 118°50′；留空自动推荐" in html
    assert "function parseCentralMeridianInput" in script
    assert "function formatCentralMeridian" in script
    assert 'candidate.crs_source === "custom"' in script
    assert "自定义 CGCS2000 高斯—克吕格" in script


def test_srt_configuration_dialog_conditionally_supports_fixed_track_visual_pose() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="srtConfigDescription"' in html
    assert "COLMAP 稀疏三维重建只反算姿态" in html
    for element_id in (
        "routeOffsetFields",
        "routeOffsetXInput",
        "routeOffsetYInput",
        "routeOffsetZInput",
    ):
        assert f'id="{element_id}"' in html
    assert 'workflow.value === "srt_fixed_track_visual_pose"' in script
    assert "COLMAP 稀疏三维重建只反算姿态" in script
    assert "不执行三维重建" not in script[script.index("function openFullPoseDialog"):script.index("function closeFullPoseDialog")]
    assert "srt_fixed_track_visual_pose_settings" in script
    assert "/srt-fixed-track-visual-pose`" in script
    assert "route_offset_xyz_m" in script
    assert "state.srtConfigWorkflow" in script
    assert 'id="reconstructionResolutionField"' in html
    assert 'id="reconstructionResolutionInput"' in html
    assert '<option value="1080p">1080p（推荐）</option>' in html
    assert '<option value="720p">720p（快速）</option>' in html
    assert '<option value="source">原始分辨率</option>' in html
    assert '$("#reconstructionResolutionField").hidden = !fixedTrack;' in script
    assert 'settings.reconstruction_resolution || "1080p"' in script
    assert "reconstruction_resolution: reconstructionResolution" in script
    assert 'body.state === "preparing_trajectory"' in script
    assert "preparingTrajectory ? clip.progress" in script
    assert "activeProgress?.message" in script
    assert "clip.status" in script
    assert "clip.error" in script
    assert "preparationFailed" in script
    assert "preparation?.error" in script
    assert 'if (!preparingTrajectory && preparation?.status === "success")' in script
    assert "await openWorkbench(clip," in script


def test_srt_workbench_uses_shared_six_dof_keyframe_controls() -> None:
    viewer = ROOT / "apps" / "web_camera_viewer"
    html = (viewer / "index.html").read_text(encoding="utf-8")
    assert "full_pose_adjustment.js" not in html
    assert 'id="fullPoseAdjustmentPanel"' not in html
    assert 'id="cameraSettingsDetails"' in html
    assert 'id="cameraControls"' in html
    assert 'id="addKeyframe"' in html
    assert 'id="workflowRunAlignment"' in html
    assert 'id="workflowRender"' in html


def test_candidate_dialog_polls_persisted_operation_and_shows_real_progress() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    for element_id in (
        "cadGeoreferenceProgress",
        "cadGeoreferenceProgressFill",
        "cadGeoreferenceProgressText",
        "cadGeoreferenceProgressPercent",
    ):
        assert f'id="{element_id}"' in html
    assert "function renderCadGeoreferenceOperation" in script
    assert "async function pollCadGeoreferenceCandidates" in script
    assert 'method: "GET"' in script
    assert "central_meridian_deg" in script
    assert "candidate_job_id" in script
    assert "candidate_input_fingerprint" in script
    assert "progress-indeterminate" in script
    assert ".candidate-operation-progress" in css


def test_workspace_wires_reanalysis_retry_and_cancel_to_real_api_routes() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "/analysis/start" in script
    assert "/analysis/activate" in script
    assert 'id="analysisCandidateDialog"' in html
    assert 'id="confirmAnalysisCandidate"' in html
    assert 'id="analysisCandidateSummary"' in html
    assert 'id="analysisCandidateClips"' in html
    assert 'id="analysisCandidateRevision"' not in html
    assert "candidate_analysis_revision" in script
    assert "candidate_analysis_preview" in script
    assert "preview.clips.map" in script
    assert "WORKFLOW_LABELS" in script
    assert "expected_clips_revision" in script
    assert 'reanalyzeButton.textContent = "重新分析中…"' in script
    assert 'reanalyzeButton.textContent = "应用新分析结果"' in script
    assert "dismissedCandidateRevision" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/retry`" in script
    assert "`/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/cancel`" in script
    assert '#reanalyzeButton").addEventListener' in script


def test_cancelled_trajectory_returns_to_pending_presentation() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "function trajectoryDisplayStatus(clip)" in script
    assert 'clip.job_type === "trajectory" && clip.status === "cancelled"' in script
    assert "STATUS_LABELS[trajectoryDisplayStatus(clip)]" in script
    assert '"failed", "interrupted", "cancelled"' in script
    assert '["ready", "queued"].includes(trajectoryDisplayStatus(clip))' in script


def test_cad_card_reveals_coordinate_preserving_replacement_with_real_progress() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'id="cadAssetCard"' in html
    assert 'id="cadReplacementTrigger"' in html
    assert 'aria-label="替换 CAD 图纸"' in html
    assert 'id="cadReplacementDialog"' in html
    assert 'id="cadReplacementFile"' in html
    assert 'id="cadCoordinateConfirmation"' in html
    assert "新版 CAD 与当前项目使用相同坐标系" in html
    assert "cad_replacement.eligible" in script
    assert "/uploads/cad-replacement" in script
    assert "sameCoordinateSystem=1" in script
    assert "cad_replacement.progress?.fraction" in script
    assert "Math.min(99" in script
    assert ".cad-replacement-card.is-replace-eligible:hover" in css
    assert ".cad-replacement-rail" in css
    assert "background: var(--surface-2)" in css
    assert "border-radius: 0 10px 10px 0" in css


def test_workspace_wires_batch_render_to_render_preflight_api() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="batchRenderButton"' in html
    assert '"render-jobs"' in script
    assert '#batchRenderButton").addEventListener' in script
    assert "snapshot.capabilities.can_render" in script


def test_workspace_wires_merge_button_to_project_queue_and_download() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="mergeStatusMessage"' in html
    assert 'id="mergeResultDialog"' in html
    assert 'id="mergeResultVideo"' in html
    assert 'id="mergeResultDownload"' in html
    assert 'id="closeMergeResult"' in html
    assert '"/merge-jobs"' in script or "/merge-jobs`" in script
    assert "snapshot.capabilities.can_merge" in script
    assert "snapshot.merge?.download_url" in script
    assert "snapshot.merge?.progress?.fraction" in script
    assert "function openMergeResult" in script
    assert 'searchParams.set("download", "1")' in script
    assert "mergeResultDialog" in script
    assert "mergeResultVideo" in script
    assert "mergeResultDownload" in script
    assert "window.location.assign(state.snapshot.merge.download_url)" not in script
    assert "正在提交合并任务…" in script
    assert "合并失败：" in script
    assert 'addEventListener("click", mergeProject)' in script


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


def test_srt_workbench_entry_requires_confirmed_current_cad_georeference() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert "输入中央经线 → 生成候选 → 点击“确认此坐标系” → 保存配置" in html
    assert "function srtConfigurationBlockReason(clip)" in script
    assert "state.snapshot?.cad_georeference?.confirmed !== true" in script
    assert "请先确认当前 CAD 坐标系，再保存配置" in script
    assert "openFullPoseDialog(clip);" in script
    assert "const srtConfigurationReason = srtConfigurationBlockReason(clip);" in script
    assert "open.title = srtConfigurationReason" in script
    assert "srtConfigurationReason || clip.capabilities?.reason" in script
    assert script.index("srtConfigurationBlockReason(clip)", script.index("async function openWorkbench")) < script.index(
        "/workbench-sessions`", script.index("async function openWorkbench")
    )
    assert '$("#saveFullPoseSettings").disabled = !confirmed;' in script


def test_completed_route_can_bridge_previous_or_next_clip_from_source_row() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert 'class="button compact bridge-up"' in html
    assert 'class="button compact bridge-down"' in html
    assert "向上打通" in html and "向下打通" in html
    assert "capabilities.can_bridge_up" in script
    assert "capabilities.can_bridge_down" in script
    assert "bridge_up_target_clip_id" in script
    assert "bridge_down_target_clip_id" in script
    assert "bridge_down_reason" in script
    assert "function jobStatusLabel" in script
    assert 'scene_bridge: "路线打通"' in script
    assert 'trajectory: "轨迹反算"' in script
    assert 'cancel.textContent = jobActionLabel(clip.job_type, "cancel")' in script
    assert 'retry.textContent = jobActionLabel(clip.job_type, "retry")' in script
    assert '`向上：${capabilities.bridge_up_reason}`' in script
    assert '`向下：${capabilities.bridge_down_reason}`' in script
    assert "capabilities.bridge_up_reason\n      || capabilities.bridge_down_reason" not in script
    assert "旧打通结果已失效，可重新打通" in script
    assert 'const hasSavedWorkbench = clip.workbench?.state === "saved";' in script
    assert (
        'if (!hasSavedWorkbench && ["stale_input", "superseded"].includes(bridgeStatus))'
        in script
    )
    assert "/scene-bridges`" in script
    assert 'direction: direction' in script
    assert "async function bridgeAdjacent" in script
    assert "expected_jobs_revision: state.snapshot.component_revisions.jobs" in script
    assert "async function waitForSceneBridge" in script
    assert "response.status === 202" in script
    assert "await openWorkbench(target, targetRow)" in script
    assert 'const directionLabel = direction === "up" ? "向上" : "向下";' in script
    assert "正在提交${directionLabel}打通任务" in script
    assert "路线打通失败：${error.message}" in script
    assert ".row-actions" in css and "flex-wrap: wrap" in css


def test_ready_clip_prepares_inputs_then_opens_workbench_with_chinese_status() -> None:
    html = (WORKSPACE / "index.html").read_text(encoding="utf-8")
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'id="workbenchPreparationDialog"' in html
    assert 'id="closeWorkbenchPreparation"' in html
    assert 'aria-label="关闭进度窗口"' in html
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


def test_full_pose_workbench_preparation_has_workflow_specific_progress_copy() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")

    assert 'clip?.resolved_workflow === "srt_full_pose"' in script
    assert '"SRT 全姿态轨迹"' in script
    assert '"SRT 轨迹与稀疏重建姿态"' in script


def test_workbench_preparation_dialog_can_close_without_cancelling_background_job() -> None:
    script = (WORKSPACE / "project_workspace.js").read_text(encoding="utf-8")
    css = (WORKSPACE / "style.css").read_text(encoding="utf-8")

    assert "preparationDialogDismissed: false" in script
    assert "function closeWorkbenchPreparationDialog" in script
    assert '$("#closeWorkbenchPreparation").addEventListener("click"' in script
    assert 'dialog.addEventListener("cancel", (event) =>' in script
    assert "event.preventDefault()" in script
    assert "state.preparationDialogDismissed = true" in script
    assert "const shouldOpenWorkbench = !state.preparationDialogDismissed" in script
    assert "if (shouldOpenWorkbench)" in script
    assert "路线打通已完成，可进入目标片段工作台继续微调" in script
    assert "preparation-dialog-close" in css
