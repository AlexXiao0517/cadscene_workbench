from pathlib import Path


def test_viewer_has_isolated_pure_rotation_controls_and_no_sfm_path_contract() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    for identifier in ("pureRotationCalibrationPanel", "pureRotationPlacementSection", "pureRotationCorrectionSection"):
        assert identifier in html
    assert "pureRotationTrack" not in html
    assert "pureRotationModePlacement" not in html
    assert "pureRotationModeCorrection" not in html
    assert "/api/pure-rotation/trajectory" in script
    assert "pts_time_sec" in script
    assert "pure_rotation" in script
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")
    assert "cadsceneApplyPureRotationPose" in legacy
    assert 'apiPost("/api/pure-rotation/placement"' in script
    assert 'apiPost("/api/pure-rotation/corrections"' in script
    assert "pureRotationDeleteCorrection" in script
    server = Path("cadscene/cli/serve_viewer.py").read_text(encoding="utf-8")
    assert "global_camera_placement.json" in server
    assert "rotation_correction_keyframes.json" in server
    assert "correction_lineage.json" in server
    assert '"base_sha256"' in server
    assert '"corrected_sha256"' in server


def test_pure_rotation_viewer_focus_is_automatic_not_a_debug_toolbar_action() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    assert 'id="pureRotationFocusCamera"' not in html
    assert "cadsceneFocusVirtualCamera" in legacy
    assert 'document.querySelector("#pureRotationFocusCamera")' not in workflow
    assert "focusPureRotationCameraOnce" in workflow


def test_pure_rotation_replaces_only_sfm_stage_and_skips_quality() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    style = Path("apps/web_camera_viewer/style.css").read_text(encoding="utf-8")

    for identifier in (
        "workflowStartPureRotation",
        "pureRotationKeyframeActions",
        "pureRotationCalibrationPanel",
        "pureRotationPlacementSection",
        "pureRotationCorrectionSection",
        "pureRotationSavePlacement",
        "pureRotationAddCorrection",
    ):
        assert f'id="{identifier}"' in html
    assert ">开始运行<" in html
    assert html.index('id="pureRotationCalibrationPanel"') > html.index('id="cameraControls"')
    assert 'id="pureRotationPanel"' not in html
    assert "applyPureRotationWorkflowLayout" in workflow
    assert 'li[data-stage="quality"]' in workflow
    assert 'toggleAttribute("hidden", pure)' in workflow
    assert 'renderOrdinal.textContent = pure ? "4" : "5"' in workflow
    assert ".workflow-steps li[hidden]" in style
    assert ".workflow-bar [hidden]" in style
    assert "display: none !important" in style
    assert 'apiPost("/api/pure-rotation/run"' in workflow
    assert 'run_dir / "02_pure_rotation" / "camera_rotation_raw.json"' not in workflow
    assert "cadsceneSetPureRotationEditMode" in workflow
    assert "cadsceneGetCurrentCameraPose" in workflow
    sfm_init = workflow.split("function maybeAutoApplySfmCameraInit()", 1)[1].split(
        "async function refreshAlignmentArtifactState", 1
    )[0]
    assert "isPureRotationWorkflow()" in sfm_init


def test_pure_rotation_run_button_is_reenabled_after_manifest_resolution() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")

    assert 'const pureRunButton = document.querySelector("#workflowStartPureRotation")' in workflow
    assert "pureRunButton.disabled = false" in workflow
    assert 'pureRunButton.removeAttribute("title")' in workflow
    assert 'id="workflowStartPureRotation"' in html
    assert ">开始运行<" in html
    recovery_actions = workflow.split(
        "function updatePureRotationRecoveryActions", 1
    )[1].split("function applyPureRotationWorkflowLayout", 1)[0]
    assert "runButton.hidden = false" in recovery_actions
    assert "rerunButton.disabled = !ready || running || blocked" in recovery_actions


def test_pure_rotation_global_placement_recovers_before_single_frame_render() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    pose_function = legacy.split("window.cadsceneApplyPureRotationPose = function (pose)", 1)[1].split(
        "window.cadsceneFocusVirtualCamera", 1
    )[0]
    assert "threeScene.ensureCameraNearCad(nextCamera)" in pose_function
    assert pose_function.index("threeScene.ensureCameraNearCad(nextCamera)") < pose_function.index("updateViews(")
    apply_function = workflow.split("function applyPureRotationPose()", 1)[1].split(
        "const pureRotationVideo", 1
    )[0]
    assert "cadsceneEnsureVirtualCameraNearCad" not in apply_function
    assert "focusInspectOnCameraAndCad" in legacy


def test_pure_rotation_playback_owns_the_animation_loop_without_sfm_pose_writes() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    assert "window.cadsceneRefreshPureRotationPose = applyPureRotationPose" in workflow
    track_apply = legacy.split("function applyTrackPoseForCurrentFrame()", 1)[1].split(
        "function addOrUpdateKeyframe()", 1
    )[0]
    assert "pureRotationPlaybackActive" in track_apply
    assert "cadsceneRefreshPureRotationPose" in track_apply
    assert track_apply.index("pureRotationPlaybackActive") < track_apply.index("poseForFrame(")


def test_pure_rotation_euler_display_does_not_show_false_360_degree_wrap() -> None:
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")
    pose_function = legacy.split("window.cadsceneApplyPureRotationPose = function (pose)", 1)[1].split(
        "window.cadsceneFocusVirtualCamera", 1
    )[0]

    assert "unwrapDegreesNear(euler.yaw, camera.yaw)" in pose_function
    assert "unwrapDegreesNear(euler.roll, camera.roll)" in pose_function


def test_pure_rotation_rendering_uses_authoritative_matrix_without_euler_round_trip() -> None:
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")
    axes_function = legacy.split("function getCameraAxes(params)", 1)[1].split(
        "function worldToCamera", 1
    )[0]
    pose_function = legacy.split("window.cadsceneApplyPureRotationPose = function (pose)", 1)[1].split(
        "window.cadsceneFocusVirtualCamera", 1
    )[0]

    assert "pureRotationAuthoritativeMatrix" in legacy
    assert "axesFromAuthoritativeRotation" in axes_function
    assert axes_function.index("axesFromAuthoritativeRotation") < axes_function.index("const yaw")
    assert "pureRotationAuthoritativeMatrix = rotation" in pose_function
    assert "clearPureRotationAuthoritativeMatrix" in legacy


def test_unsaved_current_pts_placement_is_captured_on_play_and_refreshed_at_boundaries() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "capturePureRotationDraftPlacement" in workflow
    assert '"play", capturePureRotationDraftPlacement' in workflow
    for event_name in ("loadedmetadata", "seeked", "pause", "ended"):
        assert f'"{event_name}", applyPureRotationPose' in workflow
    assert "applyDraftPlacement" in workflow


def test_internal_track_layers_are_not_exposed_and_fov_source_is_visible() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    server = Path("cadscene/cli/serve_viewer.py").read_text(encoding="utf-8")

    assert "未标定局部旋转（调试）" not in html
    assert "固定相机放置预览" not in html
    assert "姿态关键帧预览" not in html
    assert 'loadPureRotationTrack("raw")' in workflow
    assert '"corrected" : "base"' in workflow
    assert 'id="pureRotationFovSource"' in html
    assert "horizontalFovDeg" in workflow
    assert '"summary.json"' in server


def test_pure_rotation_so3_assets_are_cache_busted_and_not_stored() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    server = Path("cadscene/cli/serve_viewer.py").read_text(encoding="utf-8")

    assert "style.css?v=20260810-background-ux" in html
    assert "pure_rotation_math.js?v=20260729-local-camera-v8" in html
    assert "viewer_legacy.js?v=20260729-local-camera-v8" in html
    assert "workflow.js?v=20260810-background-ux" in html
    assert '"Cache-Control", "no-store"' in server


def test_pure_rotation_job_status_occupies_sfm_workflow_slot() -> None:
    runner = Path("cadscene/workflow/job_runner.py").read_text(encoding="utf-8")
    assert '"pure_rotation": "sfm"' in runner


def test_pure_rotation_uses_four_product_stages_without_manual_recovery_transition() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    for identifier in (
        "pureRotationRecoveryActions",
        "workflowStartPureRotation",
        "workflowRerunPureRotation",
    ):
        assert f'id="{identifier}"' in html
    assert 'id="workflowEnterPureCalibration"' not in html
    assert 'document.querySelector(\'#workflowSteps li[data-stage="quality"]\')' in workflow
    assert 'toggleAttribute("hidden", pure)' in workflow
    assert 'renderOrdinal.textContent = pure ? "4" : "5"' in workflow
    pure_next = workflow.split("const pureNext =", 1)[1].split("};", 1)[0]
    assert 'sfm: "keyframes"' in pure_next
    assert 'keyframes: "render"' in pure_next
    assert "quality" not in pure_next
    assert 'document.querySelector("#workflowEnterPureCalibration")' not in workflow
    assert "updatePureRotationRecoveryActions" in workflow


def test_reused_pure_rotation_result_auto_enters_debug() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    reused = workflow.split(
        'if (!force && await resourceExists(runPath("02_pure_rotation/camera_rotation_raw.json")))', 1
    )[1].split('const result = await apiPost("/api/pure-rotation/run"', 1)[0]

    assert 'setWorkflowStage("keyframes")' in reused
    assert "进入关键帧标定" not in reused


def test_pure_rotation_workflow_uses_single_step_badge_and_auto_enters_debug() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    style = Path("apps/web_camera_viewer/style.css").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert 'id="workflowKeyframeStageLabel"' in html
    assert ".workflow-steps li > span:first-child" in style
    assert ".workflow-steps li span {" not in style
    layout = workflow.split("function applyPureRotationWorkflowLayout(mode)", 1)[1].split(
        "function pureRotationPoseAtPts", 1
    )[0]
    assert 'keyframeLabel.textContent = pure ? "调试" : "关键帧标定"' in layout
    status = workflow.split("async function renderStatus(payload)", 1)[1].split(
        "async function pollJobStatus", 1
    )[0]
    assert 'operation === "pure_rotation"' in status
    assert 'setWorkflowStage("keyframes")' in status


def test_loading_pure_rotation_refreshes_an_already_selected_stage_title() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    layout = workflow.split("function applyPureRotationWorkflowLayout(mode)", 1)[1].split(
        "function pureRotationPoseAtPts", 1
    )[0]

    assert "if (selectedWorkflowStage)" in layout
    assert "renderWorkflowPanel(selectedWorkflowStage)" in layout


def test_project_trajectory_transition_does_not_close_its_workbench_session() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    completion = workflow.split("async function waitForProjectWorkbenchTrajectory(jobId)", 1)[1].split(
        "async function runProjectWorkbenchTrajectory()", 1
    )[0]
    pagehide = workflow.split('window.addEventListener("pagehide"', 1)[1].split(
        "function updateKeyframePlanUi", 1
    )[0]

    assert "projectWorkbenchInternalNavigation = true" in completion
    assert completion.index("projectWorkbenchInternalNavigation = true") < completion.index(
        "window.location.replace"
    )
    assert "projectWorkbenchInternalNavigation" in pagehide


def test_pure_rotation_debug_actions_are_compact_and_mode_stable() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "<legend>固定相机初始设置</legend>" in html
    assert 'id="pureRotationUseGizmo"' not in html
    strip = html.split('<div class="track-strip">', 1)[1].split(
        '<div id="cameraControls"', 1
    )[0]
    for identifier in (
        "pureRotationAddCorrection",
        "pureRotationDeleteCorrection",
        "pureRotationPreviousCorrection",
        "pureRotationNextCorrection",
        "pureRotationUndoDraft",
    ):
        assert f'id="{identifier}"' in strip
    correction_section = html.split('id="pureRotationCorrectionSection"', 1)[1].split(
        "</fieldset>", 1
    )[0]
    assert "pureRotationAddCorrection" not in correction_section
    layout = workflow.split("function applyPureRotationWorkflowLayout(mode)", 1)[1].split(
        "function pureRotationPoseAtPts", 1
    )[0]
    assert '#viewCurrentSuggestion, #ignoreCurrentSuggestion' in layout
    save = workflow.split("async function savePureRotationPlacement()", 1)[1].split(
        "async function restorePureRotationPlacement", 1
    )[0]
    assert 'setPureRotationEditMode("correction")' not in save
    assert 'document.querySelector("#pureRotationUseGizmo")' not in workflow


def test_pure_rotation_quality_copy_is_not_used_for_calibration_completion() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    pure_finish = workflow.split(
        "async function finishPureRotationCalibration()", 1
    )[1].split("async function previewPureRotationFittedTrack", 1)[0]
    assert "quality" not in pure_finish.lower()
    assert "render" in pure_finish.lower()


def test_pure_rotation_calibration_controls_live_below_camera_parameters() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    toolbar = html.split('<div class="panel-title">', 1)[1].split(
        '<p id="pureRotationControlNotice"', 1
    )[0]
    for removed_id in (
        "pureRotationTrackControl",
        "pureRotationModePlacement",
        "pureRotationModeCorrection",
        "pureRotationFocusCamera",
    ):
        assert f'id="{removed_id}"' not in toolbar

    camera_controls_position = html.index('id="cameraControls"')
    calibration_panel_position = html.index('id="pureRotationCalibrationPanel"')
    assert calibration_panel_position > camera_controls_position
    for identifier in (
        "pureRotationPlacementSection",
        "pureRotationCorrectionSection",
        "pureRotationSavePlacement",
        "pureRotationRestorePlacement",
        "pureRotationAddCorrection",
        "pureRotationDeleteCorrection",
        "pureRotationPreviousCorrection",
        "pureRotationNextCorrection",
        "pureRotationUndoDraft",
        "translateMode",
        "rotateMode",
    ):
        assert f'id="{identifier}"' in html
    pure_layout = workflow.split("function applyPureRotationWorkflowLayout(mode)", 1)[1].split(
        "function pureRotationPoseAtPts", 1
    )[0]
    assert 'document.querySelector("#cameraToolbar")?.toggleAttribute("hidden", pure)' not in pure_layout
    for hidden_control in (
        "resetCamera",
        "addKeyframe",
        "deleteKeyframe",
        "previousKeyframe",
        "nextKeyframe",
        "toggleGizmo",
        "toggleCadText",
        "exportCamera",
    ):
        assert hidden_control in pure_layout
    toolbar_hiding = pure_layout.split("for (const id of [", 1)[1].split("])", 1)[0]
    assert "translateMode" not in toolbar_hiding
    assert "rotateMode" not in toolbar_hiding


def test_entering_pure_rotation_calibration_defaults_to_translatable_global_placement() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    stage_entry = workflow.split("function setWorkflowStage(stage)", 1)[1].split(
        "async function refreshQualityArtifactsAfterSuccess", 1
    )[0]
    assert 'setPureRotationEditMode("placement")' in stage_entry
    assert 'pureRotationHasPlacement ? "correction" : "placement"' not in stage_entry
    edit_mode = legacy.split(
        "window.cadsceneSetPureRotationEditMode = function (mode)", 1
    )[1].split("window.cadsceneGetCurrentCameraPose", 1)[0]
    assert 'threeScene.setMode(correctionMode ? "rotate" : "translate")' in edit_mode
    assert 'button.disabled = correctionMode' in edit_mode


def test_recovery_and_global_placement_do_not_auto_apply_saved_corrections() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    initialization = workflow.split(
        "async function initializePureRotationViewer()", 1
    )[1].split("function updatePureRotationFovSource", 1)[0]
    assert 'pureRotationTrajectory = await loadPureRotationTrack("raw")' in initialization
    assert '"corrected"' not in initialization
    edit_mode = workflow.split("function setPureRotationEditMode(mode)", 1)[1].split(
        "async function savePureRotationPlacement", 1
    )[0]
    assert "pureRotationTrajectoryKind === target" in edit_mode
    assert 'pureRotationHasPlacement ? "base" : "raw"' in edit_mode
    restore = workflow.split(
        "async function restorePureRotationPlacement()", 1
    )[1].split("async function refreshPureRotationFittedPreview", 1)[0]
    assert 'loadPureRotationTrack("base")' in restore
    assert '"corrected"' not in restore


def test_world_vertical_slider_uses_an_immutable_frame_baseline() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    for identifier in (
        "pureRotationWorldYaw",
        "pureRotationWorldYawNumber",
        "pureRotationResetWorldYaw",
    ):
        assert f'id="{identifier}"' in html
    assert "pureRotationCorrectionDraftBase" in workflow
    assert "rotateAboutWorldUp" in workflow
    assert "refreshPureRotationCorrectionDraftBase" in workflow
    assert "cadsceneApplyManualPureRotationMatrix" in workflow
    assert "window.cadsceneApplyManualPureRotationMatrix" in legacy
    assert "pureRotationEditModeReady" in workflow
    preview_function = workflow.split(
        "async function previewPureRotationWorldYaw", 1
    )[1].split("function capturePureRotationDraftPlacement", 1)[0]
    assert 'await setPureRotationEditMode("correction")' in preview_function


def test_world_vertical_slider_preserves_camera_state_and_dirty_draft_until_save() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "pureRotationCorrectionDraftDirty" in workflow
    edit_mode = workflow.split("function setPureRotationEditMode(mode)", 1)[1].split(
        "async function savePureRotationPlacement", 1
    )[0]
    assert 'nextMode === "correction"' in edit_mode
    assert "cadsceneGetCurrentCameraPose" in edit_mode
    assert "seedPureRotationCorrectionDraftFromManual" in edit_mode

    seed = workflow.split(
        "function seedPureRotationCorrectionDraftFromManual", 1
    )[1].split("function refreshPureRotationCorrectionDraftBase", 1)[0]
    assert "camera_center_web" in seed
    assert "display_fov" in seed
    assert "cadsceneApplyPureRotationPose" in seed

    refresh = workflow.split(
        "function refreshPureRotationCorrectionDraftBase", 1
    )[1].split("function applyPureRotationCorrectionPreview", 1)[0]
    assert "pureRotationCorrectionDraftDirty && sameFrame" in refresh

    preview = workflow.split(
        "async function previewPureRotationWorldYaw", 1
    )[1].split("async function previewPureRotationLocalDelta", 1)[0]
    assert "pureRotationCorrectionDraftDirty = true" in preview

    save = workflow.split("async function addPureRotationCorrection()", 1)[1].split(
        "async function deletePureRotationCorrection", 1
    )[0]
    assert save.index("await apiPost") < save.index("pureRotationCorrectionDraftDirty = false")


def test_pose_corrections_use_camera_local_axes_and_keep_world_up_separate() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    for identifier in (
        "pureRotationLocalYaw",
        "pureRotationLocalPitch",
        "pureRotationLocalRoll",
    ):
        assert f'id="{identifier}"' in html
    assert "applyLocalCameraDelta" in workflow
    edit_mode = legacy.split(
        "window.cadsceneSetPureRotationEditMode = function (mode)", 1
    )[1].split("window.cadsceneGetCurrentCameraPose", 1)[0]
    assert 'threeScene.setTransformSpace(correctionMode ? "local" : "world")' in edit_mode
    assert '["x", "y", "z", "yaw", "pitch", "roll", "fov"]' in edit_mode


def test_authoritative_rotation_matrix_is_saved_without_world_euler_round_trip() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    manual_apply = legacy.split(
        "window.cadsceneApplyManualPureRotationMatrix = function (rotation)", 1
    )[1].split("window.cadsceneFocusVirtualCamera", 1)[0]
    assert "matrixToViewerEuler" not in manual_apply
    current_pose = legacy.split(
        "window.cadsceneGetCurrentCameraPose = function ()", 1
    )[1].split("function applyTrackPayload", 1)[0]
    assert "rotation_cad_from_camera" in current_pose
    assert "manualRotationMatrix" in workflow
    assert workflow.count("viewerEulerToMatrix(manual)") == 1


def test_debug_entry_seeds_a_manual_anchor_instead_of_treating_raw_as_absolute() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    initialization = workflow.split(
        "async function initializePureRotationViewer()", 1
    )[1].split("function updatePureRotationFovSource", 1)[0]
    assert "seedPureRotationDraftPlacement" in initialization
    assert initialization.index("seedPureRotationDraftPlacement") < initialization.index("applyPureRotationPose")
    seed = workflow.split(
        "function seedPureRotationDraftPlacement()", 1
    )[1].split("function updatePureRotationFovSource", 1)[0]
    assert "rotation_local_from_camera" in seed
    assert "cadsceneGetDefaultCameraPose" in seed
    assert "window.cadsceneGetDefaultCameraPose = function ()" in legacy


def test_default_debug_fov_prefers_candidate_intrinsics_over_saved_placement() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    initialization = workflow.split(
        "async function initializePureRotationViewer()", 1
    )[1].split("function seedPureRotationDraftPlacement", 1)[0]

    assert initialization.index("candidateFov") < initialization.index("placement.fov")
    assert 'pureRotationFovSource = "unverified_candidate_intrinsics"' in initialization


def test_correction_mutations_refresh_fitted_preview_before_render_handoff() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "refreshPureRotationFittedPreview" in workflow
    add_function = workflow.split("async function addPureRotationCorrection()", 1)[1].split(
        "async function deletePureRotationCorrection()", 1
    )[0]
    delete_function = workflow.split("async function deletePureRotationCorrection()", 1)[1].split(
        "function jumpPureRotationCorrection", 1
    )[0]
    assert "refreshPureRotationFittedPreview" in add_function
    assert "refreshPureRotationFittedPreview" in delete_function
    assert 'id="workflowPreviewPureFitted"' in html
    assert 'id="workflowReturnPureCalibration"' in html
    assert "finishPureRotationCalibration" in workflow
    finish_function = workflow.split("async function finishPureRotationCalibration()", 1)[1].split(
        "async function", 1
    )[0]
    assert "pureRotationHasPlacement" in finish_function
    assert 'pureRotationEditMode === "placement"' in finish_function
    assert "await savePureRotationPlacement()" in finish_function
    assert "refreshPureRotationFittedPreview" in finish_function
    assert 'setWorkflowStage("render")' in finish_function
    assert finish_function.index("await savePureRotationPlacement()") < finish_function.index(
        "await refreshPureRotationFittedPreview()"
    )
    assert finish_function.index("await refreshPureRotationFittedPreview()") < finish_function.index(
        'setWorkflowStage("render")'
    )


def test_pure_rotation_debug_labels_explain_confirmation_and_render_handoff() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "确认当前相机位置与方向" in html
    assert "撤销未确认的调整" in html
    assert "完成调试并进入渲染" in html
    assert "调整立即预览，播放无需确认" in html
    restore_function = workflow.split(
        "async function restorePureRotationPlacement()", 1
    )[1].split("async function refreshPureRotationFittedPreview", 1)[0]
    assert "尚未保存全局固定相机放置" not in restore_function
    assert "seedPureRotationDraftPlacement()" in restore_function


def test_completed_pure_rotation_job_is_not_reinitialized_on_every_status_poll() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    assert "workflow.js?v=20260810-background-ux" in html
    assert "let pureRotationHandledCompletion = null;" in workflow
    render_status = workflow.split("async function renderStatus(payload)", 1)[1].split(
        "async function refreshAlignmentArtifactState", 1
    )[0]
    assert "pureRotationHandledCompletion !== completionKey" in render_status
    assert render_status.index("pureRotationHandledCompletion = completionKey") < render_status.index(
        "await initializePureRotationViewer()"
    )
    assert 'selectedWorkflowStage === "sfm"' in render_status
    assert render_status.index('selectedWorkflowStage === "sfm"') < render_status.index(
        'setWorkflowStage("keyframes")'
    )


def test_pure_rotation_keyframe_stage_never_shows_sfm_alignment_copy() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    render_panel = workflow.split("function renderWorkflowPanel(stage)", 1)[1].split(
        "function setWorkflowStage(stage)", 1
    )[0]

    assert 'stage === "keyframes" && isPureRotationWorkflow()' in render_panel
    assert "悬停旋转调试：移动固定相机并调整姿态；播放检查旋转效果，完成后直接进入渲染导出。" in render_panel
    sfm_copy_branch = render_panel.split(
        'stage === "keyframes" && isPureRotationWorkflow()', 1
    )[1].split("SfM 已完成", 1)[0]
    assert "!isPureRotationWorkflow()" in sfm_copy_branch
