from pathlib import Path


def test_viewer_has_isolated_pure_rotation_controls_and_no_sfm_path_contract() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    script = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    for identifier in ("pureRotationTrack", "pureRotationModePlacement", "pureRotationModeCorrection"):
        assert identifier in html
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


def test_pure_rotation_viewer_can_focus_the_fixed_virtual_camera() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    legacy = Path("apps/web_camera_viewer/viewer_legacy.js").read_text(encoding="utf-8")

    assert 'id="pureRotationFocusCamera"' in html
    assert "cadsceneFocusVirtualCamera" in legacy
    assert 'document.querySelector("#pureRotationFocusCamera")' in workflow
    assert "focusPureRotationCameraOnce" in workflow


def test_pure_rotation_replaces_only_sfm_stage_and_skips_quality() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    style = Path("apps/web_camera_viewer/style.css").read_text(encoding="utf-8")

    for identifier in (
        "workflowStartPureRotation",
        "pureRotationKeyframeActions",
        "pureRotationModePlacement",
        "pureRotationModeCorrection",
        "pureRotationSavePlacement",
        "pureRotationAddCorrection",
    ):
        assert f'id="{identifier}"' in html
    assert "OpenGV 旋转轨迹恢复" in html
    assert html.index('id="pureRotationModePlacement"') > html.index('<section class="control-panel">')
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
        'document.querySelector("#pureRotationTrack")', 1
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


def test_track_names_explain_preview_semantics_and_fov_source_is_visible() -> None:
    html = Path("apps/web_camera_viewer/index.html").read_text(encoding="utf-8")
    workflow = Path("apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    server = Path("cadscene/cli/serve_viewer.py").read_text(encoding="utf-8")

    assert "未标定局部旋转（调试）" in html
    assert "固定相机放置预览" in html
    assert "姿态关键帧预览" in html
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
