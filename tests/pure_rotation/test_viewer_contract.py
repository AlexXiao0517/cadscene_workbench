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
    assert "运行旋转轨迹恢复" in html
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

    assert 'const pureRunButton = document.querySelector("#workflowStartPureRotation")' in workflow
    assert "pureRunButton.disabled = false" in workflow
    assert 'pureRunButton.removeAttribute("title")' in workflow


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

    assert "pure_rotation_math.js?v=20260728-so3-v3" in html
    assert "viewer_legacy.js?v=20260728-so3-v3" in html
    assert "workflow.js?v=20260728-so3-v3" in html
    assert '"Cache-Control", "no-store"' in server


def test_pure_rotation_job_status_occupies_sfm_workflow_slot() -> None:
    runner = Path("cadscene/workflow/job_runner.py").read_text(encoding="utf-8")
    assert '"pure_rotation": "sfm"' in runner


def test_pure_rotation_uses_four_product_stages_and_explicit_recovery_transition() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    for identifier in (
        "pureRotationRecoveryActions",
        "workflowStartPureRotation",
        "workflowRerunPureRotation",
        "workflowEnterPureCalibration",
    ):
        assert f'id="{identifier}"' in html
    assert 'document.querySelector(\'#workflowSteps li[data-stage="quality"]\')' in workflow
    assert 'toggleAttribute("hidden", pure)' in workflow
    assert 'renderOrdinal.textContent = pure ? "4" : "5"' in workflow
    pure_next = workflow.split("const pureNext =", 1)[1].split("};", 1)[0]
    assert 'sfm: "keyframes"' in pure_next
    assert 'keyframes: "render"' in pure_next
    assert "quality" not in pure_next
    assert 'document.querySelector("#workflowEnterPureCalibration")' in workflow
    assert "updatePureRotationRecoveryActions" in workflow


def test_pure_rotation_quality_copy_is_not_used_for_calibration_completion() -> None:
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")

    pure_finish = workflow.split(
        'document.querySelector("#workflowPureFinishKeyframes")', 1
    )[1].split("});", 1)[0]
    assert "quality" not in pure_finish.lower()
    assert "render" in pure_finish.lower()


def test_pure_rotation_calibration_controls_live_below_camera_parameters() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")

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
    ):
        assert f'id="{identifier}"' in html


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
