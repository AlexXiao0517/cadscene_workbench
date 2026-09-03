from __future__ import annotations

from pathlib import Path
import json
import subprocess

import yaml


APP = Path("apps/web_camera_viewer")


def _read(name: str) -> str:
    return (APP / name).read_text(encoding="utf-8")


def test_workflow_ui_keeps_legacy_dual_view_root_and_panels() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert 'class="workspace"' in html
    assert 'id="sourceVideo"' in html
    assert 'id="sceneContainer"' in html
    assert 'id="cameraControls"' in html
    assert "grid-template-columns:" in css
    assert ".workspace" in css
    assert "workflow-wizard-page" not in html


def test_three_panel_is_visible_and_not_replaced_by_workflow() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert 'id="sceneContainer" class="scene-stage"' in html
    assert 'id="workflowPanel"' in html
    assert "display: none" not in css[css.find(".scene-stage") : css.find(".scene-stage") + 180]


def test_workflow_contains_steps_controls_and_keyframe_intervals() -> None:
    combined = _read("index.html") + _read("workflow.js")

    for text in ("上传数据", "SfM重建", "关键帧标定", "质量检测", "渲染导出"):
        assert text in combined
    for stage in ("upload", "sfm", "keyframes", "quality", "render"):
        assert f'data-stage="{stage}"' in combined
    for value in ("120", "180", "240"):
        assert value in combined
    for button_id in (
        "workflowStartSfm",
        "workflowRunAlignment",
        "workflowRender",
        "workflowGenerateKeyframes",
        "viewCurrentSuggestion",
        "ignoreCurrentSuggestion",
    ):
        assert button_id in combined
    assert "workflowEnterKeyframes" not in combined


def test_workflow_polls_job_status_and_handles_ignored_suggestions() -> None:
    script = _read("workflow.js")

    assert "job_status.json" in script
    assert "setInterval" in script
    assert "1000" in script
    assert "ignored_suggestions.json" in script
    assert "ignore-suggestion" in script
    assert "selectedWorkflowStage" in script
    assert "renderWorkflowPanel" in script
    assert "if (!selectedWorkflowStage)" in script
    assert "renderWorkflowPanel(payload.current_stage" not in script
    assert "nextStageAfterSuccess" in script
    assert 'sfm: "keyframes"' in script
    assert "detectWorkflowStageFromArtifacts" in script
    assert "02_sfm/camera_trajectory.json" in script
    assert "02_sfm/sparse_points.ply" in script
    assert "workflowSfmStatus" in script
    assert "已完成首帧标定后点击「路线拟合」" in script
    assert "CAD-aligned 点云" in script
    assert "不适合三维重建" in script
    assert "明显平移" in script
    assert "suitable_for_3d" in script
    assert "next_step_recommendation" in script
    assert "SfM 注册失败" in script
    assert 'if (sfmSuitable === false) return "sfm"' in script


def test_full_pose_workbench_preserves_workflow_and_uses_no_sfm_artifacts() -> None:
    script = _read("workflow.js")
    pipeline_path = Path("configs/pipelines/srt_full_pose_overlay.yaml")

    assert '"srt_full_pose"' in script
    assert "fullPoseTrajectoryPath" in script
    assert '02_srt_full_pose/camera_trajectory_full_pose.json' in script
    assert "fullPoseTrajectoryReady" in script
    pipeline = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))
    serialized = json.dumps(pipeline)
    assert "road_surface" not in pipeline["stages"]
    assert "sparse_ply" not in serialized
    assert "sfm" not in pipeline["stages"]


def test_fixed_track_workbench_shows_route_and_skips_sfm_and_quality() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "function isFixedTrackVisualPoseWorkflow()" in workflow
    assert '"srt_fixed_track_visual_pose"' in workflow
    assert '02_srt_visual_pose/camera_trajectory_visual_pose.json' in workflow
    assert 'keyframes: "render"' in workflow
    assert 'stage === "quality" ? "render"' in workflow
    assert 'stageTitles.keyframes = fixed ? "视觉姿态"' in workflow
    assert 'stageTitles.render = fixed ? "微调与渲染"' in workflow
    assert 'li[data-stage="upload"]' in workflow
    assert 'li[data-stage="sfm"]' in workflow
    assert 'li[data-stage="quality"]' in workflow
    assert (
        "isFullPoseWorkflow() || isFixedTrackVisualPoseWorkflow()" in workflow
    )
    assert 'stateLabel.textContent = "轨迹已就绪"' in workflow
    assert 'message.textContent = "SRT→CAD 固定轨迹已载入；位置锁定，仅姿态可微调。"' in workflow

    assert "window.cadsceneSetFixedTrackVisualPoseMode" in viewer
    assert 'for (const key of ["x", "y", "z"])' in viewer
    assert "fixedTrackPoseAtFrame" in viewer
    assert "orientation_available !== false" in viewer
    assert "setFrustumOrientationAvailable" in viewer


def test_quality_success_refreshes_quality_artifacts_without_page_reload() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "cadsceneReloadQualityArtifacts" in workflow
    assert "cadsceneReloadQualityArtifacts" in viewer
    assert "operation" in workflow and '"quality"' in workflow
    assert "cadsceneQualityArtifactsLoaded" in workflow
    assert 'fetch(path, { cache: "no-store" })' in viewer


def test_alignment_success_reload_keeps_project_workbench_session_open() -> None:
    script = _read("workflow.js")
    reload_flow = script[
        script.index('const pendingReload = sessionStorage.getItem(reloadKey)') :
        script.index('} else if (pendingReload && ["failed", "cancelled"]')
    ]

    internal_navigation = reload_flow.index("projectWorkbenchInternalNavigation = true")
    reload_page = reload_flow.index("window.location.reload()")

    assert internal_navigation < reload_page


def test_ignored_suggestions_are_forwarded_to_the_legacy_timeline_and_scene() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "cadsceneSetIgnoredSuggestions" in workflow
    assert "cadsceneSetIgnoredSuggestions" in viewer
    assert "visibleQualitySuggestions" in viewer
    assert "qualityMarkerHits" in viewer


def test_workflow_buttons_call_real_runner_and_quality_saves_track_first() -> None:
    script = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "/api/workflow/run-stage" in script
    assert "/api/workflow/save-camera-track" in script
    assert "/api/workflow/cancel" in script
    assert "/api/workflow/job-log" in script
    assert "updateMockStage" not in script
    save_index = script.index("/api/workflow/save-camera-track")
    alignment_index = script.index('runStage("alignment"')
    quality_function = script.index("async function startQualityStage()")
    quality_save_index = script.index("await saveCurrentCameraTrack()", quality_function)
    quality_index = script.index('runStage("quality"', quality_function)
    assert save_index < alignment_index
    assert quality_save_index < quality_index
    assert "keyframePlan.pending_count" in script[quality_function:quality_index]
    assert "cadsceneGetCameraTrack" in viewer
    assert "window.cadsceneGetCameraTrack()" in script
    assert "/api/workflow/sfm-camera-init" in script
    assert "cadsceneApplyCameraParameters" in viewer
    assert "window.cadsceneApplyCameraParameters" in script
    assert "safe_fields" in viewer


def test_sfm_initialization_retimes_the_loaded_track_with_authoritative_fps() -> None:
    viewer = _read("viewer_legacy.js")

    assert "const fps = Number(params.fps);" in viewer
    assert "cameraTrack.fps = fps;" in viewer
    assert "keyframe.time = frameToTime(Number(keyframe.frame));" in viewer


def test_bottom_keyframe_edits_are_persisted_to_the_active_run() -> None:
    script = _read("workflow.js")

    assert 'querySelector("#addKeyframe")' in script
    assert 'querySelector("#deleteKeyframe")' in script
    assert "async function persistEditedCameraTrack" in script
    assert "await saveCurrentCameraTrack()" in script
    assert "persistEditedCameraTrack({ advancePlan: true })" in script
    assert 'querySelector("#deleteKeyframe")?.addEventListener("click", () => persistEditedCameraTrack())' in script


def test_project_workbench_bootstraps_coordinates_save_and_returns() -> None:
    script = _read("workflow.js")

    assert 'params.get("projectWorkbenchToken")' in script
    assert 'params.get("projectId") || dataset' in script
    assert 'params.get("workflowStage")' in script
    assert "stageOrder.includes(requestedWorkflowStage)" in script
    assert "bootstrapProjectWorkbenchSession" in script
    assert "/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}`" in script
    existing_save = script.index('/api/workflow/save-camera-track')
    coordinated_save = script.index('/save`', existing_save)
    assert existing_save < coordinated_save
    assert "existing_save: result" in script
    assert "expected_revision: projectWorkbenchSession.clips_revision" in script
    assert "window.location.assign(projectWorkbenchSession.return_to)" in script
    assert 'window.addEventListener("pagehide"' in script
    assert 'keepalive: true' in script
    assert "projectWorkbenchSaveInFlight" in script[script.index('window.addEventListener("pagehide"'):]
    pure_finish = script[
        script.index("async function finishPureRotationCalibration") :
        script.index("async function previewPureRotationFittedTrack")
    ]
    assert "await refreshPureRotationFittedPreview()" in pure_finish
    assert "await finalizeProjectWorkbenchSave(" in pure_finish
    assert '{ ok: true, kind: "pure_rotation_calibration" }' in pure_finish
    assert "{ navigate: false }" in pure_finish
    assert pure_finish.index("finalizeProjectWorkbenchSave") < pure_finish.index(
        'setWorkflowStage("render")'
    )
    save_track = script[
        script.index("async function saveCurrentCameraTrack") :
        script.index("async function bootstrapProjectWorkbenchSession")
    ]
    assert "finalizeProjectWorkbenchSave" not in save_track
    assert "async function finishQualityStage" in script
    quality_start = script.index("async function finishQualityStage")
    quality_finish = script[quality_start:script.index("async function persistWorkbenchDraftForReturn", quality_start)]
    assert "await ensureProjectWorkbenchSession()" in quality_finish
    assert "await saveCurrentCameraTrack()" in quality_finish
    assert "await finalizeProjectWorkbenchSave" in quality_finish
    assert "projectWorkbenchBootstrapPromise" in script
    assert "async function runProjectWorkbenchTrajectory" in script
    assert "trajectory-jobs" in script
    assert "trajectory-ready" in script
    assert "return runProjectWorkbenchTrajectory()" in script
    assert "projectWorkbenchTrajectoryIsPending" in script
    assert "片段视频和项目 CAD 已就绪，请点击开始 SfM 重建" in script
    availability = script[
        script.index("function refreshSupplementalWorkflowActionAvailability") :
        script.index("function blockTrajectoryWorkflowActionsUntilResolved")
    ]
    assert 'querySelectorAll("[data-job-action]")' in availability
    assert "button.disabled = blocked" in availability


def test_stale_workbench_url_reopens_current_project_clip_on_refresh() -> None:
    script = _read("workflow.js")

    recovery = script[
        script.index("async function recoverStaleProjectWorkbenchSession") :
        script.index("async function bootstrapProjectWorkbenchSession")
    ]
    assert "/snapshot`" in recovery
    assert "snapshot.clips.find((item) => item.clip_id === runId)" in recovery
    assert "clip.capabilities?.can_open_workbench" in recovery
    assert "/clips/${encodeURIComponent(runId)}/workbench-sessions`" in recovery
    assert "expected_revision: snapshot.component_revisions.clips" in recovery
    assert "expected_jobs_revision: snapshot.component_revisions.jobs" in recovery
    assert "window.location.replace(payload.workbench_url)" in recovery

    bootstrap = script[
        script.index("async function bootstrapProjectWorkbenchSession") :
        script.index("function projectWorkbenchTrajectoryIsPending")
    ]
    assert 'payload.error === "stale_workbench_session"' in bootstrap
    assert "return recoverStaleProjectWorkbenchSession()" in bootstrap


def test_saved_workbench_url_reopens_current_project_clip_on_refresh() -> None:
    script = _read("workflow.js")
    bootstrap = script[
        script.index("async function bootstrapProjectWorkbenchSession") :
        script.index("function projectWorkbenchTrajectoryIsPending")
    ]
    terminal_state = bootstrap[
        bootstrap.index('if (!new Set(["editing", "pending_save"]).has(payload.state))') :
        bootstrap.index("projectWorkbenchSession = payload")
    ]

    assert "return recoverStaleProjectWorkbenchSession()" in terminal_state
    assert "项目工作台会话已失效" not in terminal_state


def test_completed_action_restores_next_stage_before_stale_url_stage() -> None:
    script = _read("workflow.js")
    start = script.index("function selectInitialWorkflowStage()")
    end = script.index("if (projectWorkbenchToken)", start)
    select_initial = script[start:end]

    assert select_initial.index("stageOrder.includes(restoredWorkflowStage)") < select_initial.index(
        "stageOrder.includes(requestedWorkflowStage)"
    )


def test_project_workbench_workflow_never_falls_back_to_sfm_on_manifest_read_error() -> None:
    script = _read("workflow.js")
    loader = script[
        script.index("async function loadManifestBackedTrajectoryWorkflow") :
        script.index("async function updateSfmSummary")
    ]

    assert "projectWorkbenchSession.workflow" in loader
    assert "projectWorkbenchWorkflowMode" in loader
    assert "trajectoryWorkflow = projectWorkbenchWorkflowMode" in loader
    catch_branch = loader[loader.index("catch (error)") :]
    project_branch = catch_branch[
        catch_branch.index("if (projectWorkbenchWorkflowMode)") :
        catch_branch.index("// Preserve legacy dataset/run URLs")
    ]
    assert 'trajectory_mode: projectWorkbenchWorkflowMode' in project_branch
    assert 'trajectory_mode: "sfm_only"' not in project_branch


def test_project_workbench_applies_session_workflow_before_selecting_pending_stage() -> None:
    script = _read("workflow.js")
    bootstrap = script[
        script.index("async function bootstrapProjectWorkbenchSession") :
        script.index("function projectWorkbenchTrajectoryIsPending")
    ]

    apply_workflow = bootstrap.index(
        "applyProjectWorkbenchSessionWorkflow(projectWorkbenchSession)"
    )
    select_stage = bootstrap.index("setWorkflowStage(\"sfm\")")
    assert apply_workflow < select_stage
    assert "isPureRotationWorkflow()" in bootstrap
    assert "片段视频和项目 CAD 已就绪，请点击开始旋转轨迹恢复" in bootstrap


def test_saved_project_pure_rotation_stage_is_not_overwritten_by_artifact_detection() -> None:
    script = _read("workflow.js")
    workflow = script[
        script.index("function renderTrajectoryWorkflow") :
        script.index("function isPureRotationWorkflow")
    ]

    assert "projectWorkbenchToken" in workflow
    assert "projectWorkbenchSession?.workbench_output_revision" in workflow
    assert 'setWorkflowStage("render")' in workflow
    assert workflow.index("projectWorkbenchSession?.workbench_output_revision") < workflow.index(
        "detectWorkflowStageFromArtifacts()"
    )


def test_failed_project_workbench_bootstrap_does_not_fall_open_to_sfm() -> None:
    script = _read("workflow.js")
    startup = script[script.index("initializeWorkbenchTheme()") :]

    assert "let projectWorkbenchBootstrapFailed = false;" in script
    assert "projectWorkbenchBootstrapFailed = true;" in startup
    assert "if (projectWorkbenchBootstrapFailed) return;" in startup


def test_stale_project_workbench_session_can_return_without_closing_token() -> None:
    script = _read("workflow.js")
    return_flow = script[
        script.index("async function returnToProjectWorkspace") :
        script.index('window.addEventListener("pagehide"')
    ]

    assert "if (projectWorkbenchBootstrapFailed || !projectWorkbenchSession)" in return_flow
    assert "projectWorkbenchFallbackReturnTo()" in return_flow
    assert return_flow.index("projectWorkbenchFallbackReturnTo()") < return_flow.index(
        "await ensureProjectWorkbenchSession()"
    )


def test_project_trajectory_polling_has_one_status_owner_and_terminal_cleanup() -> None:
    script = _read("workflow.js")

    status_poll = script[
        script.index("async function pollJobStatus") : script.index("function stageOptions")
    ]
    log_poll = script[
        script.index("async function pollJobLog") : script.index("function runWithMessage")
    ]
    wait = script[
        script.index("async function waitForProjectWorkbenchTrajectory") :
        script.index("async function runProjectWorkbenchTrajectory")
    ]
    cancel = script[
        script.index("async function cancelRunningJob") :
        script.index("function updateRenderProgressFromLog")
    ]

    assert "function projectWorkbenchTrajectoryOwnsStatus" in script
    assert "if (projectWorkbenchTrajectoryOwnsStatus()) return;" in status_poll
    assert status_poll.index("projectWorkbenchTrajectoryOwnsStatus()") < status_poll.index(
        "projectWorkbenchTrajectoryIsPending()"
    )
    assert "if (projectWorkbenchTrajectoryOwnsStatus()) return;" in log_poll
    assert "projectWorkbenchTrajectoryJobId = null" in wait
    assert 'querySelector("#workflowCancel").hidden = true' in wait
    assert "renderProjectTrajectorySnapshot(clip)" in wait
    assert "if (projectWorkbenchToken)" in cancel
    assert "if (!projectWorkbenchTrajectoryJobId) return null" in cancel


def test_workflow_start_session_auto_binds_an_active_project_trajectory() -> None:
    script = _read("workflow.js")

    assert "async function attachActiveProjectWorkbenchTrajectory" in script
    assert '!clip.job_type || clip.job_type === "trajectory"' in script
    assert "activeStatuses.has(clip.status)" in script
    assert "projectWorkbenchTrajectoryJobId = clip.job_id" in script
    assert "waitForProjectWorkbenchTrajectory(clip.job_id)" in script


def test_project_trajectory_progress_uses_snapshot_and_runtime_only_updates_log() -> None:
    script = _read("workflow.js")
    wait = script[
        script.index("async function waitForProjectWorkbenchTrajectory") :
        script.index("async function runProjectWorkbenchTrajectory")
    ]

    assert "/jobs/${encodeURIComponent(jobId)}/runtime" in wait
    assert "renderProjectTrajectorySnapshot(clip)" in wait
    assert "await renderStatus(runtime.workflow_status)" not in wait
    assert 'document.querySelector("#workflowLogContent")' in wait
    assert 'runtime.lines.join("\\n")' in wait


def test_project_workbench_renews_session_during_long_trajectory_jobs() -> None:
    script = _read("workflow.js")
    wait = script[
        script.index("async function waitForProjectWorkbenchTrajectory") :
        script.index("async function runProjectWorkbenchTrajectory")
    ]

    assert "PROJECT_WORKBENCH_HEARTBEAT_MS = 60_000" in script
    assert "/heartbeat`" in script
    assert "renewProjectWorkbenchSession" in wait
    trajectory_ready = wait.index("/trajectory-ready`")
    assert wait.rindex("renewProjectWorkbenchSession", 0, trajectory_ready) >= 0


def test_project_workbench_renews_session_during_manual_editing() -> None:
    script = _read("workflow.js")
    bootstrap = script[
        script.index("async function bootstrapProjectWorkbenchSession") :
        script.index("function projectWorkbenchTrajectoryIsPending")
    ]

    assert "startProjectWorkbenchEditingHeartbeat" in script
    assert "window.setInterval" in script
    assert "PROJECT_WORKBENCH_HEARTBEAT_MS" in script
    assert "startProjectWorkbenchEditingHeartbeat" in bootstrap


def test_render_rejects_expired_workbench_session_instead_of_silently_skipping_save() -> None:
    script = _read("workflow.js")
    finalize = script[
        script.index("async function finalizeProjectWorkbenchSave") :
        script.index("async function finishQualityStage")
    ]

    assert "项目工作台会话已失效" in finalize
    invalid_state = finalize[
        finalize.index('projectWorkbenchSession.state !== "editing"') :
        finalize.index("projectWorkbenchSaveInFlight = true")
    ]
    assert "throw new Error" in invalid_state
    assert "return null" not in invalid_state


def test_workbench_save_waits_for_inflight_editing_heartbeat() -> None:
    script = _read("workflow.js")
    finalize = script[
        script.index("async function finalizeProjectWorkbenchSave") :
        script.index("async function finishQualityStage")
    ]

    assert "projectWorkbenchEditingHeartbeatPromise" in script
    assert "await projectWorkbenchEditingHeartbeatPromise" in finalize
    assert finalize.index("await projectWorkbenchEditingHeartbeatPromise") < finalize.index(
        "/save`"
    )


def test_render_button_uses_generic_video_copy() -> None:
    html = _read("index.html")

    assert 'id="workflowRender" data-job-action type="button">渲染视频</button>' in html
    assert "渲染带标签视频" not in html


def test_successful_project_trajectory_navigates_to_keyframe_stage() -> None:
    script = _read("workflow.js")
    wait = script[
        script.index("async function waitForProjectWorkbenchTrajectory") :
        script.index("async function runProjectWorkbenchTrajectory")
    ]

    assert 'nextUrl.searchParams.set("workflowStage", "keyframes")' in wait
    assert "window.location.replace(nextUrl.toString())" in wait
    assert wait.index("/trajectory-ready`") < wait.index(
        'nextUrl.searchParams.set("workflowStage", "keyframes")'
    )


def test_existing_pure_rotation_trajectory_opens_directly_in_debug_stage() -> None:
    script = _read("workflow.js")
    detection = script[
        script.index("async function detectWorkflowStageFromArtifacts") :
        script.index("function updateWorkflowStepActive")
    ]

    pure_branch = detection[
        detection.index("if (isPureRotationWorkflow())") :
        detection.index("const renderReady")
    ]
    assert 'runPath("02_pure_rotation/camera_rotation_raw.json")' in pure_branch
    assert 'if (rawReady)' in pure_branch
    assert 'return "keyframes"' in pure_branch


def test_project_trajectory_start_is_single_flight_and_retries_terminal_job() -> None:
    script = _read("workflow.js")
    start = script[
        script.index("async function runProjectWorkbenchTrajectory") :
        script.index("async function finalizeProjectWorkbenchSave")
    ]

    assert "projectWorkbenchTrajectoryStartPromise" in script
    assert "if (projectWorkbenchTrajectoryStartPromise)" in start
    assert "return projectWorkbenchTrajectoryStartPromise" in start
    assert "async function runProjectWorkbenchTrajectoryOnce" in start
    assert 'new Set(["failed", "interrupted", "cancelled", "stale_input", "superseded"])' in start
    assert "`/jobs/${encodeURIComponent(currentClip.job_id)}/retry`" in start
    assert "expected_revision: snapshot.component_revisions.jobs" in start


def test_keyframe_save_is_serialized_and_advances_the_single_plan_progress() -> None:
    script = _read("workflow.js")
    start = script.index("async function persistEditedCameraTrack")
    end = script.index("async function cancelRunningJob", start)
    persist = script[start:end]

    assert "keyframeSaveInFlight" in script
    assert "await saveCurrentCameraTrack" in persist
    assert "jumpToNextPendingKeyframe" in persist
    assert "计划关键帧已全部完成" in persist


def test_render_saves_frontend_track_before_refitting_and_rendering() -> None:
    script = _read("workflow.js")
    start = script.index("async function startRenderStage")
    end = script.index("async function cancelRunningJob", start)
    render = script[start:end]

    assert render.index("await saveCurrentCameraTrack") < render.index('runStage("render")')


def test_project_workbench_render_uses_project_queue_and_snapshot() -> None:
    script = _read("workflow.js")
    start = script.index("async function startRenderStage")
    end = script.index("async function cancelRunningJob", start)
    render = script[start:end]

    assert "if (projectWorkbenchToken)" in render
    assert 'projectWorkbenchRequest("/render-jobs"' in render
    assert "enqueue: false" in render
    assert "enqueue: true" in render
    assert "preflight.needs_confirmation || preflight.confirmation_required || []" in render
    assert "confirmationRequired.includes(clipId) ? [clipId] : []" in render
    assert "confirmed_clip_ids: confirmedClipIds" in render
    assert "waitForProjectWorkbenchRender" in render
    assert render.index("if (projectWorkbenchToken)") < render.index('runStage("render")')
    snapshot_refresh = render.index("/snapshot")
    preflight_request = render.index('projectWorkbenchRequest("/render-jobs"')
    assert snapshot_refresh < preflight_request
    assert "projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs" in render

    wait_start = script.index("async function waitForProjectWorkbenchRender")
    wait_end = script.index("async function startRenderStage", wait_start)
    wait = script[wait_start:wait_end]
    assert "/snapshot" in wait
    assert "clip.render" in wait
    assert "/runtime" in wait
    assert 'new Set(["success", "failed", "interrupted", "cancelled", "stale_input", "superseded"])' in wait


def test_saved_project_workbench_renders_without_attempting_a_second_save() -> None:
    script = _read("workflow.js")
    start = script.index("async function startRenderStage")
    end = script.index("async function cancelRunningJob", start)
    render = script[start:end]

    editable_guard = 'projectWorkbenchSession.state === "editing"'
    saved_guard = 'projectWorkbenchSession.state !== "saved"'
    assert editable_guard in render
    assert saved_guard in render
    assert render.index(editable_guard) < render.index("await saveCurrentCameraTrack()")
    assert render.index("await saveCurrentCameraTrack()") < render.index(
        "await finalizeProjectWorkbenchSave"
    )
    assert render.index(saved_guard) < render.index('projectWorkbenchRequest("/render-jobs"')


def test_project_render_success_keeps_project_status_and_uses_snapshot_preview_url() -> None:
    script = _read("workflow.js")
    wait_start = script.index("async function waitForProjectWorkbenchRender")
    wait_end = script.index("async function startRenderStage", wait_start)
    wait = script[wait_start:wait_end]

    assert 'projectWorkbenchRenderStatus = "success"' in wait
    assert "render.preview_url" in wait
    assert "await refreshRenderOutputState()" in wait

    preview_start = script.index("async function refreshRenderOutputState")
    preview_end = script.index("if (renderPath)", preview_start)
    preview = script[preview_start:preview_end]
    assert "/snapshot" in preview
    assert "clip?.render?.preview_url" in preview
    assert "activeRenderPath" in script


def test_entering_render_stage_refreshes_the_published_project_output() -> None:
    script = _read("workflow.js")
    start = script.index("function setWorkflowStage")
    end = script.index("async function refreshQualityArtifactsAfterSuccess", start)
    select_stage = script[start:end]

    assert 'selectedWorkflowStage === "render"' in select_stage
    assert "projectWorkbenchToken" in select_stage
    assert "projectWorkbenchSession?.clip_id" in select_stage
    assert "refreshRenderOutputState().catch" in select_stage


def test_finishing_sfm_quality_keeps_project_workbench_on_render_stage() -> None:
    script = _read("workflow.js")
    start = script.index("async function finishQualityStage")
    end = script.index("async function persistWorkbenchDraftForReturn", start)
    finish = script[start:end]

    assert finish.index('setWorkflowStage("render")') < finish.index("await persistQualityCompletion()")
    assert "async function persistQualityCompletion" in finish
    assert "await saveCurrentCameraTrack()" in finish
    assert "await finalizeProjectWorkbenchSave" in finish
    finalize = script[
        script.index("async function finalizeProjectWorkbenchSave") :
        script.index("async function finishQualityStage")
    ]
    assert "await persistProjectWorkbenchResumeNow" in finalize


def test_project_workbench_resume_uses_durable_stage_and_authoritative_pts() -> None:
    script = _read("workflow.js")

    assert "/resume`" in script
    assert "expected_resume_revision" in script
    assert "projectWorkbenchSession?.resume_state?.workflow_stage" in script
    assert "window.CadsceneAnnotationPts?.seekSourcePts" in script
    assert 'window.addEventListener("cadscenePtsAuthorityReady"' in script
    assert 'resumeVideo?.addEventListener("pause"' in script
    assert 'resumeVideo?.addEventListener("seeked"' in script
    assert "sourceTimeBase?.()" in script


def test_unsaved_camera_draft_warns_on_close_and_clears_after_durable_save() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "let cameraDraftDirty = false;" in viewer
    notify = viewer[
        viewer.index("function notifyManualCameraChanged") :
        viewer.index("function worldToCamera")
    ]
    assert "cameraDraftDirty = true;" in notify
    assert "window.cadsceneHasUnsavedCameraDraft" in viewer
    assert "window.cadsceneClearUnsavedCameraDraft" in viewer
    assert 'window.addEventListener("beforeunload"' in workflow
    assert "window.cadsceneHasUnsavedCameraDraft?.()" in workflow
    assert "pureRotationCorrectionDraftDirty" in workflow
    persist = workflow[
        workflow.index("async function persistEditedCameraTrack") :
        workflow.index("function projectRenderStatusCopy")
    ]
    assert "window.cadsceneClearUnsavedCameraDraft?.();" in persist
    pagehide = workflow[
        workflow.index('window.addEventListener("pagehide"') :
        workflow.index("function updateKeyframePlanUi")
    ]
    assert "saveCurrentCameraTrack" not in pagehide


def test_alignment_operation_is_shown_and_polled_under_the_keyframe_stage() -> None:
    script = _read("workflow.js")

    assert 'operation === "alignment"' in script
    assert "runningStage = isRunning ? operation" in script


def test_sfm_completion_auto_initializes_fov_without_enter_keyframes_button() -> None:
    script = _read("workflow.js")

    assert "applySfmCameraInitializationOnce" in script
    assert "maybeAutoApplySfmCameraInit();" in script
    assert 'querySelector("#workflowEnterKeyframes")' not in script
    assert "进入/继续关键帧标定" not in _read("index.html")


def test_sfm_fov_is_applied_after_the_legacy_viewer_has_loaded_the_saved_track() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert "cadsceneViewerReady" in workflow
    assert "cadsceneViewerReady" in viewer
    assert "cadsceneHasLoadedCameraTrack" in workflow
    assert "window.cadsceneHasLoadedCameraTrack" in viewer
    assert "Array.isArray(appliedFields)" in workflow


def test_sfm_fov_waits_for_viewer_ready_before_marking_initialization() -> None:
    workflow = _read("workflow.js")

    assert "let viewerReadyForSfmCameraInit = false;" in workflow
    auto_apply = workflow[workflow.index("function maybeAutoApplySfmCameraInit") : workflow.index("async function applySfmCameraInitializationOnce")]
    assert "if (!viewerReadyForSfmCameraInit || isPureRotationWorkflow()) return;" in auto_apply
    assert "viewerReadyForSfmCameraInit = true;" in workflow
    assert "window.addEventListener(\"cadsceneViewerReady\", () => {" in workflow
    assert "sfmCameraInitializationPromise" in workflow


def test_viewer_cache_busts_the_sfm_fov_initialization_script() -> None:
    index = _read("index.html")

    assert 'workflow.js?v=20260831-workbench-resume-v1' in index


def test_sfm_fov_initialization_is_page_local_so_refresh_reapplies_intrinsics() -> None:
    workflow = _read("workflow.js")

    assert "let sfmCameraInitializationPromise = null;" in workflow
    assert "let sfmCameraInitializationComplete = false;" in workflow
    assert "cadsceneSfmCameraInit" not in workflow
    assert "sessionStorage.getItem(key)" not in workflow
    assert "sessionStorage.setItem(key" not in workflow


def test_sfm_fov_preserves_authoritative_track_but_repairs_default_placeholder() -> None:
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    sfm_init = workflow[
        workflow.index("async function applySfmCameraInitializationOnce") :
        workflow.index(
            "function currentFrame",
            workflow.index("async function applySfmCameraInitializationOnce"),
        )
    ]
    assert "window.cadsceneHasLoadedCameraTrack?.()" in sfm_init
    assert "let loadedAuthoritativeCameraTrack = false;" in viewer
    assert "cameraTrack.keyframes.length > 1" in viewer
    assert "Boolean(keyframe.source)" in viewer
    assert "window.cadsceneHasLoadedCameraTrack = function ()" in viewer


def test_viewer_keeps_requested_frame_for_keyframe_save_when_video_seeks_nearby() -> None:
    viewer = _read("viewer_legacy.js")
    seek = viewer[viewer.index("async function seekVideoToFrame") : viewer.index("function goToFrame", viewer.index("async function seekVideoToFrame"))]

    assert "manualFrameOverride = null;" not in seek
    assert "已跳转到帧 ${targetFrame}" in seek


def test_workflow_prefers_sfm_artifacts_over_stale_failed_job_stage_and_restores_manual_track() -> None:
    workflow = _read("workflow.js")
    paths = _read("paths.js")
    viewer = _read("viewer_legacy.js")

    assert "detectWorkflowStageFromArtifacts()" in workflow
    assert "latestJobStatus?.status !== \"running\"" in workflow
    assert '"01_keyframes", "camera_track_manual.json"' in paths
    assert "trackFallbacks" in paths
    assert "TRACK_FALLBACKS" in viewer
    assert "loadTrackFromPaths" in viewer


def test_route_fitting_lives_in_keyframe_stage_not_quality_stage() -> None:
    html = _read("index.html")
    keyframe_start = html.index('class="workflow-stage-panel" data-stage="keyframes"')
    quality_start = html.index('class="workflow-stage-panel" data-stage="quality"')
    render_start = html.index('class="workflow-stage-panel" data-stage="render"')
    keyframes = html[keyframe_start:quality_start]
    quality = html[quality_start:render_start]

    assert "路线拟合" in keyframes
    assert 'id="workflowRunAlignment"' in keyframes
    assert "路线拟合" not in quality
    assert "运行质量检测" in quality


def test_keyframe_plan_is_created_after_initial_route_fit_and_can_continue_pending_work() -> None:
    html = _read("index.html")
    script = _read("workflow.js")
    keyframe_start = html.index('class="workflow-stage-panel" data-stage="keyframes"')
    quality_start = html.index('class="workflow-stage-panel" data-stage="quality"')
    keyframes = html[keyframe_start:quality_start]

    assert keyframes.index('id="workflowRunAlignment"') < keyframes.index('id="workflowKeyframeStep"')
    assert 'id="workflowContinueKeyframes"' in keyframes
    assert "/api/workflow/generate-keyframe-plan" in script
    assert "loadKeyframePlan" in script
    assert "jumpToNextPendingKeyframe" in script
    assert "Stage 4A mock" not in script


def test_generating_keyframe_plan_refreshes_route_fit_and_timeline_from_saved_plan() -> None:
    script = _read("workflow.js")
    start = script.index("async function generateKeyframePlan()")
    end = script.index("async function finishKeyframePlan()", start)
    generate = script[start:end]

    # 路线拟合刚完成时，浏览器中的 readiness 不能成为阻断计划生成的旧状态。
    assert "await refreshAlignmentArtifactState();" in generate
    # 生成接口成功后必须从实际保存的 artifact 回读，确保质量时间轴拿到同一份计划数据。
    assert "await loadKeyframePlan({ suppressErrors: false });" in generate
    assert "关键帧计划生成失败" in generate


def test_viewer_recovers_manifest_video_and_cad_paths_for_direct_dataset_run_urls() -> None:
    script = _read("workflow.js")

    assert "function ensureManifestViewerPaths" in script
    assert 'target.searchParams.set("video", videoUrl);' in script
    assert 'target.searchParams.set("cad", cadUrl);' in script
    assert "window.location.replace(target.toString());" in script


def test_keyframe_plan_shows_progress_without_counting_pending_frames_as_manual_anchors() -> None:
    html = _read("index.html")
    workflow = _read("workflow.js")
    viewer = _read("viewer_legacy.js")

    assert 'id="workflowKeyframePlanStatus"' in html
    assert "计划：已完成" in workflow
    assert "待标定" in workflow
    assert "继续未完成标定" in html
    assert "人工关键帧：${manualKeyframes().length}" in viewer
    assert "window.CadsceneKeyframes.isConfirmedManualKeyframe(keyframe)" in viewer
    assert "window.CadsceneKeyframes.confirmedManualKeyframes(" in viewer
    assert "window.CadsceneKeyframes.confirmedManualKeyframes(" in workflow


def test_completed_keyframe_plan_refits_then_opens_quality_with_a_return_path() -> None:
    html = _read("index.html")
    script = _read("workflow.js")
    quality_start = html.index('class="workflow-stage-panel" data-stage="quality"')
    render_start = html.index('class="workflow-stage-panel" data-stage="render"')

    assert "startAlignmentStage" in script[script.index("async function finishKeyframePlan"):]
    assert "cadscenePostAlignmentStage" in script
    assert 'id="workflowReturnKeyframes"' in html[quality_start:render_start]
    assert 'setWorkflowStage("keyframes")' in script


def test_workflow_has_compact_log_and_cancel_controls() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert 'id="workflowCancel"' in html
    assert 'id="workflowLog"' in html
    assert "<details" in html
    assert "max-height:" in css[css.index(".workflow-log") :]


def test_workflow_log_does_not_overlay_the_video_or_three_view() -> None:
    css = _read("style.css")
    log_rule = css[css.index(".workflow-log {") : css.index(".workflow-log summary")]

    assert "position: static;" in log_rule
    assert "position: fixed;" not in log_rule


def test_workflow_upload_combines_cad_selection_and_parse() -> None:
    html = _read("index.html")

    assert ">上传 CAD<" in html
    assert ">解析 CAD<" not in html
    assert 'id="workflowParseCad"' not in html


def test_viewer_uses_manifest_backed_trajectory_mode_without_product_badge() -> None:
    html = _read("index.html")
    workflow = _read("workflow.js")

    assert 'id="workflowTrajectoryMode"' not in html
    assert "dataset-manifest" in workflow
    assert "interface_only" in workflow
    assert "isInterfaceOnlyTrajectoryWorkflow" in workflow
    assert "轨迹功能尚未启用" in workflow
    assert 'class="workspace"' in html


def test_project_workbench_can_save_and_return_without_cancelling_background_jobs() -> None:
    html = _read("index.html")
    script = _read("workflow.js")

    assert 'id="workbenchReturnButton"' in html
    assert 'id="workbenchReturnDialog"' in html
    assert "推理和渲染任务会继续在后台运行" in html
    assert "async function persistWorkbenchDraftForReturn" in script
    assert "async function returnToProjectWorkspace" in script
    return_flow = script[
        script.index("async function returnToProjectWorkspace") :
        script.index("window.addEventListener(\"pagehide\"")
    ]
    assert "/close`" in return_flow
    assert "projectWorkbenchInternalNavigation = true" in return_flow
    assert "window.location.assign(projectWorkbenchSession.return_to)" in return_flow
    assert "/cancel" not in return_flow


def test_workbench_shares_theme_and_uses_product_copy() -> None:
    html = _read("index.html")
    script = _read("workflow.js")
    css = _read("style.css")

    assert "CAD航拍视频叠加工作台" in html
    assert "虚拟相机参数设置" in html
    assert "虚拟 UAV 相机" not in html
    assert 'id="workbenchThemeToggle"' in html
    assert 'const THEME_STORAGE_KEY = "mediaflow-theme"' in script
    assert 'setAttribute("data-theme", theme)' in script
    assert ':root[data-theme="light"]' in css
    button_start = css.index("button,\n.file-button {")
    button_rule = css[button_start : css.index("button:hover", button_start)]
    assert "align-items: center" in button_rule
    assert "justify-content: center" in button_rule


def test_workbench_light_theme_uses_semantic_component_surfaces() -> None:
    css = _read("style.css")

    for variable in (
        "--active-bg",
        "--media-stage",
        "--floating-panel",
        "--dialog-backdrop",
        "--input-bg",
    ):
        assert css.count(variable) >= 3
    assert "background: var(--active-bg)" in css
    assert "background: var(--media-stage)" in css
    assert "background: var(--floating-panel)" in css
    assert "background: var(--input-bg)" in css


def test_project_render_runtime_log_drives_the_visible_progress_bar() -> None:
    script = _read("workflow.js")
    wait_start = script.index("async function waitForProjectWorkbenchRender")
    wait_end = script.index("async function startRenderStage", wait_start)
    wait = script[wait_start:wait_end]

    assert 'updateRenderProgressFromLog("render", runtime.lines || [])' in wait
    assert "function projectRenderPreflightCopy" in script
    assert 'stateLabel.textContent = "无法开始渲染"' in script


def test_project_render_progress_is_monotonic_and_only_completes_on_success() -> None:
    script = _read("workflow.js")
    wait_start = script.index("async function waitForProjectWorkbenchRender")
    wait_end = script.index("async function startRenderStage", wait_start)
    wait = script[wait_start:wait_end]
    helper_start = script.index("function setProjectRenderVisibleProgress")
    helper_end = script.index("\n  function ", helper_start + 1)
    helper = script[helper_start:helper_end]

    assert "Math.max(projectRenderVisibleProgress" in helper
    assert "complete ? 1 : Math.min(0.99" in helper
    assert "await renderStatus(runtime.workflow_status)" not in wait
    assert "setProjectRenderVisibleProgress(1, { complete: true })" in wait
    assert 'message.textContent = "正在封装并验证渲染结果"' in wait


def test_upload_stage_uses_real_streaming_upload_apis_and_hides_advanced_fields() -> None:
    html = _read("index.html")
    script = _read("workflow.js")

    for control_id in (
        "workflowDatasetName",
        "workflowCadScale",
        "workflowOriginX",
        "workflowOriginY",
        "workflowUploadProgress",
        "workflowUploadStatus",
    ):
        assert f'id="{control_id}"' in html
    upload_start = html.index('class="workflow-stage-panel active" data-stage="upload"')
    upload_end = html.index('class="workflow-stage-panel" data-stage="sfm"')
    upload_panel = html[upload_start:upload_end]
    advanced_start = upload_panel.index('class="workflow-upload-advanced dev-only-control"')
    advanced = upload_panel[advanced_start:]
    for control_id in ("workflowDatasetName", "workflowCadScale", "workflowOriginX", "workflowOriginY"):
        assert f'id="{control_id}"' in advanced
    assert 'id="workflowAdvancedCadInput"' in advanced
    assert ".json,.zip" in advanced
    product_cad = upload_panel[upload_panel.index('id="workflowCadInput"') - 100 : upload_panel.index('id="workflowCadInput"') + 120]
    assert ".dxf,.dwg" in product_cad
    assert ".json" not in product_cad and ".zip" not in product_cad
    assert "/api/workflow/create-dataset" in script
    assert "/api/workflow/upload-video" in script
    assert "/api/workflow/upload-cad" in script
    assert "/api/workflow/dataset-manifest" in script
    assert "XMLHttpRequest" in script
    assert "xhr.upload.addEventListener" in script
    assert "（本地预览）" not in script
    assert "deriveDatasetFromVideo" in script
    assert "import_" in script
    assert 'params.get("debug") === "1"' in script
    assert ".dev-only-control" in _read("style.css")


def test_workflow_panel_does_not_duplicate_legacy_keyframe_controls() -> None:
    html = _read("index.html")
    workflow = html[html.index('id="workflowBar"') : html.index('<section class="workspace">')]

    assert "上一关键帧" not in workflow
    assert "下一关键帧" not in workflow
    assert "保存当前关键帧" not in workflow
    assert 'id="previousKeyframe"' in html
    assert 'id="nextKeyframe"' in html
    assert 'id="addKeyframe"' in html


def test_quality_panel_only_contains_pipeline_level_actions() -> None:
    html = _read("index.html")
    start = html.index('class="workflow-stage-panel" data-stage="quality"')
    end = html.index('class="workflow-stage-panel" data-stage="render"')
    quality = html[start:end]

    assert "运行质量检测" in quality
    assert "重新检测" in quality
    assert "路线拟合" not in quality
    assert "查看建议补帧" not in quality
    assert "忽略当前建议" not in quality


def test_quality_can_finish_into_render_and_dormant_review_controls_are_hidden() -> None:
    html = _read("index.html")
    workflow = _read("workflow.js")
    quality_start = html.index('class="workflow-stage-panel" data-stage="quality"')
    render_start = html.index('class="workflow-stage-panel" data-stage="render"')
    quality = html[quality_start:render_start]

    assert 'id="workflowFinishQuality"' in quality
    assert 'setWorkflowStage("render")' in workflow
    for control_id in ("reviewStatus", "acceptPrediction", "saveAdjustedKeyframe", "rejectReview"):
        assert f'id="{control_id}"' in html
    assert html.count("workflow-hidden-control") >= 4


def test_viewer_honors_authoritative_initial_frame_from_workbench_url() -> None:
    viewer = _read("viewer_legacy.js")

    assert 'get("initialFrame")' in viewer
    assert "Number.parseInt" in viewer
    assert "function waitForVideoMetadata" in viewer
    assert 'video.addEventListener("error", onError, { once: true })' in viewer
    assert "初始定位失败：视频元数据无法加载" in viewer
    assert "await waitForVideoMetadata()" in viewer
    assert "await goToFrame(initialFrame)" in viewer


def test_sfm_workflow_reenables_finish_quality_after_manifest_mode_is_confirmed() -> None:
    script = _read("workflow.js")

    assert "function refreshSupplementalWorkflowActionAvailability" in script
    assert 'document.querySelector("#workflowFinishQuality")' in script
    assert "refreshSupplementalWorkflowActionAvailability(isRunning);" in script
    assert "refreshSupplementalWorkflowActionAvailability();" in script


def test_render_panel_refreshes_output_after_render_job_success() -> None:
    script = _read("workflow.js")

    assert "refreshRenderOutputState" in script
    assert 'operation === "render"' in script or 'operation !== "render"' in script
    assert 'method: "HEAD"' in script
    assert 'cache: "no-store"' in script


def test_render_preview_uses_in_page_dialog_instead_of_new_window() -> None:
    html = _read("index.html")
    source = _read("workflow.js")

    assert 'id="workflowRenderPreviewDialog"' in html
    assert 'id="workflowRenderPreviewVideo"' in html
    assert "showModal" in source
    assert "window.open(renderPath" not in source


def test_render_log_updates_visible_frame_progress() -> None:
    source = _read("workflow.js")

    assert "updateRenderProgressFromLog" in source
    assert r"\[render\]\s+frame" in source
    assert "正在渲染" in source
    assert "renderProgressState" in source
    assert 'operation === "render"' in source


def test_suggestion_actions_live_in_bottom_uav_controls() -> None:
    html = _read("index.html")
    workflow_end = html.index('<section class="workspace">')
    controls = html[html.index('id="cameraSettingsDetails"') :]

    assert "查看当前建议帧" not in html[:workflow_end]
    assert 'id="viewCurrentSuggestion"' in controls
    assert 'id="ignoreCurrentSuggestion"' in controls
    assert "查看下一建议帧" in controls
    assert "忽略当前建议" in controls


def test_quality_suggestions_browse_chronologically_and_cycle_after_ignore() -> None:
    html = _read("index.html")
    script = _read("workflow.js")

    assert html.index("suggestion_sequence.js") < html.index("workflow.js")
    assert "window.CadsceneSuggestionSequence" in script
    assert "suggestionSequence.ordered(list)" in script
    assert "suggestionSequence.next(available, selectedSuggestionFrame)" in script
    assert "suggestionSequence.firstAfter(availableSuggestions(), frameIndex)" in script
    assert "jumpToSuggestion(nextSuggestion)" in script
    assert "查看下一建议帧（${nextIndex + 1}/${available.length}）" in script
    assert "scoreB - scoreA" not in script


def test_development_import_buttons_are_hidden_without_debug_mode() -> None:
    html = _read("index.html")
    script = _read("workflow.js")

    assert html.count("dev-only-control") >= 2
    assert ".dev-only-control" in _read("style.css")
    assert "debug=1" in script
    assert "is-debug-visible" in script


def test_workflow_task_has_stable_compact_height() -> None:
    css = _read("style.css")
    task_rule = css[css.index(".workflow-task {") : css.index(".workflow-task-head")]

    assert "height:" in task_rule or ("min-height:" in task_rule and "max-height:" in task_rule)
    assert "overflow:" in task_rule


def test_only_active_workflow_stage_panel_is_visible() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert html.count("workflow-stage-panel") == 5
    assert ".workflow-stage-panel {" in css
    assert "display: none;" in css[css.index(".workflow-stage-panel {") :]
    active_rule = css[css.index(".workflow-stage-panel.active") :]
    assert "display: block;" in active_rule


def test_workflow_uses_no_frontend_build_chain() -> None:
    root = Path(".")

    assert not (root / "package.json").exists()
    assert not (root / "node_modules").exists()
    assert not (APP / "node_modules").exists()


def test_workflow_javascript_is_valid() -> None:
    result = subprocess.run(
        ["node", "--check", str(APP / "workflow.js")],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
