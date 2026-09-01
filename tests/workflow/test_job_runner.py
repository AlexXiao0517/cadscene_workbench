from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from cadscene.workflow import job_runner as job_runner_module
from cadscene.workflow.job_runner import JobAlreadyRunningError, JobRunner, build_stage_command, resolve_stage_inputs, save_camera_track
from cadscene.workflow.keyframe_plan import validate_quality_plan


def _wait_for_status(runner: JobRunner, dataset: str, run_id: str, expected: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = runner.query(dataset, run_id)
        if payload and payload["status"] == expected:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"job did not reach {expected}")


def test_manual_keyframe_count_excludes_unconfirmed_initial_seed(tmp_path: Path) -> None:
    track = tmp_path / "camera_track_manual.json"
    track.write_text(
        json.dumps(
            {
                "keyframes": [
                    {"frame": 0, "camera": {"x": 100.0}},
                    {"frame": 10, "source": "manual_anchor", "camera": {"x": 101.0}},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert job_runner_module._manual_keyframe_count(track) == 1


def test_runner_starts_short_command_and_writes_log_and_process_file(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)

    started = runner.start(
        "demo",
        "r1",
        "sfm",
        [sys.executable, "-c", "print('runner-ok', flush=True)"],
    )
    finished = _wait_for_status(runner, "demo", "r1", "success")

    assert started["pid"] > 0
    assert finished["returncode"] == 0
    assert Path(finished["log_file"]).read_text(encoding="utf-8").strip() == "runner-ok"
    process = json.loads((tmp_path / "runs/demo/r1/job_process.json").read_text(encoding="utf-8"))
    assert process["stage"] == "sfm"
    assert process["status"] == "success"


def test_runner_rejects_second_running_job(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)
    runner.start("demo", "r2", "sfm", [sys.executable, "-c", "import time; time.sleep(5)"])

    with pytest.raises(JobAlreadyRunningError, match="已有任务正在运行"):
        runner.start("demo", "r2", "render", [sys.executable, "-c", "print('no')"])

    runner.cancel("demo", "r2")


def test_runner_cancel_updates_process_and_job_status(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)
    runner.start("demo", "r3", "sfm", [sys.executable, "-c", "import time; time.sleep(5)"])

    cancelled = runner.cancel("demo", "r3")

    assert cancelled["status"] == "cancelled"
    job_status = json.loads((tmp_path / "runs/demo/r3/job_status.json").read_text(encoding="utf-8"))
    assert job_status["status"] == "cancelled"


def test_runner_failed_command_records_error_and_log(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)
    runner.start(
        "demo",
        "r4",
        "quality",
        [sys.executable, "-c", "import sys; print('bad-job'); sys.exit(3)"],
    )

    failed = _wait_for_status(runner, "demo", "r4", "failed")

    assert failed["returncode"] == 3
    assert "return code 3" in failed["error"]
    assert failed["detail"] == "bad-job"
    assert "bad-job" in Path(failed["log_file"]).read_text(encoding="utf-8")
    job_status = json.loads((tmp_path / "runs/demo/r4/job_status.json").read_text(encoding="utf-8"))
    assert job_status["log_file"] == failed["log_file"]
    assert job_status["stages"]["quality"]["log_file"] == failed["log_file"]
    assert job_status["stages"]["quality"]["error"] == "bad-job"


def test_runner_publishes_terminal_process_only_after_terminal_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_stage_started = threading.Event()
    release_terminal_stage = threading.Event()
    original = job_runner_module.JobStatusStore.update_stage

    def delay_terminal_stage(store, stage, **kwargs):
        if kwargs.get("status") == "failed":
            terminal_stage_started.set()
            assert release_terminal_stage.wait(timeout=5)
        return original(store, stage, **kwargs)

    monkeypatch.setattr(
        job_runner_module.JobStatusStore,
        "update_stage",
        delay_terminal_stage,
    )
    runner = JobRunner(tmp_path)
    runner.start(
        "demo",
        "terminal-order",
        "quality",
        [sys.executable, "-c", "import sys; print('ordered'); sys.exit(2)"],
    )

    try:
        assert terminal_stage_started.wait(timeout=5)
        assert runner.query("demo", "terminal-order")["status"] == "running"
    finally:
        release_terminal_stage.set()

    failed = _wait_for_status(runner, "demo", "terminal-order", "failed")
    status = json.loads(
        (tmp_path / "runs/demo/terminal-order/job_status.json").read_text(
            encoding="utf-8"
        )
    )
    assert failed["detail"] == "ordered"
    assert status["stages"]["quality"]["status"] == "failed"
    assert status["stages"]["quality"]["error"] == "ordered"


def test_runner_mirrors_quality_sidecar_progress_before_success(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)
    progress_file = tmp_path / "runs/demo/r-progress/logs/workflow/quality_progress.json"
    script = (
        "import json,sys,time; from pathlib import Path; "
        "p=Path(sys.argv[sys.argv.index('--progress-file')+1]); "
        "p.parent.mkdir(parents=True,exist_ok=True); "
        "p.write_text(json.dumps({'schema_version':'1.0','stage':'quality_frames',"
        "'message':'正在分析质量帧 42/100','fraction':0.42}),encoding='utf-8'); "
        "time.sleep(0.8)"
    )

    runner.start(
        "demo",
        "r-progress",
        "quality",
        [sys.executable, "-c", script, "--progress-file", str(progress_file)],
    )

    deadline = time.time() + 3.0
    observed = None
    status_path = tmp_path / "runs/demo/r-progress/job_status.json"
    while time.time() < deadline:
        status = json.loads(status_path.read_text(encoding="utf-8"))
        quality = status["stages"]["quality"]
        if quality["status"] == "running" and quality["progress"] >= 0.42:
            observed = quality
            break
        time.sleep(0.05)

    assert observed is not None
    assert observed["message"] == "正在分析质量帧 42/100"
    _wait_for_status(runner, "demo", "r-progress", "success")
    deadline = time.time() + 2.0
    finished = None
    while time.time() < deadline:
        try:
            candidate = json.loads(status_path.read_text(encoding="utf-8"))
        except OSError:
            time.sleep(0.02)
            continue
        if candidate["stages"]["quality"]["status"] == "success":
            finished = candidate
            break
        time.sleep(0.02)
    assert finished is not None
    assert finished["stages"]["quality"]["progress"] == 1.0


def test_resolve_sfm_python_prefers_configured_interpreter_with_pycolmap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = tmp_path / "difusser" / "python.exe"
    configured.parent.mkdir()
    configured.write_bytes(b"")
    monkeypatch.setenv("CADSCENE_SFM_PYTHON", str(configured))
    monkeypatch.setattr(
        job_runner_module,
        "_python_has_pycolmap",
        lambda path: Path(path) == configured,
    )

    assert job_runner_module.resolve_sfm_python() == configured


def test_runner_uses_command_list_without_shell_true() -> None:
    source = inspect.getsource(JobRunner.start)

    assert "shell=True" not in source


def test_background_process_options_hide_windows_console() -> None:
    options = job_runner_module._background_process_options(new_process_group=True)

    if os.name == "nt":
        assert options["creationflags"] & subprocess.CREATE_NO_WINDOW
        assert options["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert options == {}


def test_child_process_environment_forces_utf8() -> None:
    environment = job_runner_module._child_process_environment({"PATH": "test-path"})

    assert environment["PATH"] == "test-path"
    assert environment["PYTHONUTF8"] == "1"
    assert environment["PYTHONIOENCODING"] == "utf-8"
    assert environment["CADSCENE_WORKFLOW_LOG_ENCODING"] == "utf-8"


def test_tail_log_decodes_existing_windows_gbk_output(tmp_path: Path) -> None:
    runner = JobRunner(tmp_path)
    log_path = tmp_path / "runs/demo/gbk/logs/workflow/sfm.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_bytes("后端=pycolmap，设备=cpu\n正在提取特征\n".encode("gbk"))

    payload = runner.tail_log("demo", "gbk", "sfm")

    assert payload["lines"] == ["后端=pycolmap，设备=cpu", "正在提取特征"]


def test_sfm_completion_message_distinguishes_registration_failure_from_low_parallax() -> None:
    registration_failure = job_runner_module._sfm_completion_message(
        {
            "suitable_for_3d": False,
            "low_parallax_or_rotation_suspected": False,
            "registered_count": 2,
            "extracted_frame_count": 236,
        }
    )
    low_parallax = job_runner_module._sfm_completion_message(
        {
            "suitable_for_3d": False,
            "low_parallax_or_rotation_suspected": True,
        }
    )

    assert "注册失败" in registration_failure
    assert "重新拍摄" not in registration_failure
    assert "明显平移" in low_parallax


def test_saving_unchanged_track_does_not_invalidate_final_route_fit(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/demo/final-fit"
    plan_path = run_dir / "01_keyframes/keyframe_plan.json"
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": "cadscene_keyframe_plan_v1",
                "frames": [{"frame_index": 0, "status": "completed"}],
                "completed_count": 1,
                "pending_count": 0,
            }
        ),
        encoding="utf-8",
    )
    aligned_path = run_dir / "03_alignment/sfm_camera_path.csv"
    aligned_path.parent.mkdir(parents=True)
    aligned_path.write_text("\ufeffframe_index\n", encoding="utf-8")
    plan_stat_before = plan_path.stat().st_mtime_ns
    track = {
        "keyframes": [
            {"frame": 0, "source": "manual_anchor", "camera": {"x": 0.0}},
        ]
    }

    save_camera_track(tmp_path, "demo", "final-fit", track)

    assert plan_path.stat().st_mtime_ns == plan_stat_before
    validate_quality_plan(plan_path, aligned_path)


def test_saving_identical_camera_track_preserves_artifact_mtime(tmp_path: Path) -> None:
    track = {
        "fps": 25.0,
        "keyframes": [
            {"frame": 0, "source": "manual_anchor", "camera": {"x": 1.0, "y": 2.0}},
        ],
    }
    output = save_camera_track(tmp_path, "demo", "same-track", track)
    original_mtime = output.stat().st_mtime_ns

    save_camera_track(tmp_path, "demo", "same-track", json.loads(json.dumps(track)))

    assert output.stat().st_mtime_ns == original_mtime


def test_quality_command_uses_saved_manual_track(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r5"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    manual = run_dir / "01_keyframes/camera_track_manual.json"
    manual.write_text(
        '{"keyframes":['
        '{"frame":0,"source":"manual_anchor","camera":{"x":0,"y":0,"z":10}},'
        '{"frame":10,"source":"manual_anchor","camera":{"x":20,"y":0,"z":10}}'
        ']}',
        encoding="utf-8",
    )
    # 新流程下 quality 在路线拟合之后运行，需要 alignment 产物作为前置件。
    (run_dir / "03_alignment").mkdir()
    (run_dir / "03_alignment/alignment.json").write_text("{}", encoding="utf-8")
    (run_dir / "01_keyframes/keyframe_plan.json").write_text(
        '{"schema_version":"cadscene_keyframe_plan_v1","frames":[],"completed_count":0,"pending_count":0}',
        encoding="utf-8",
    )
    (run_dir / "03_alignment/sfm_camera_path.csv").write_text("\ufeffframe_index\n", encoding="utf-8")
    data = root / "data/demo"
    data.mkdir(parents=True)
    video = data / "demo.mp4"
    video.write_bytes(b"video")
    cad = data / "cad"
    cad.mkdir()
    config = root / "configs/datasets"
    config.mkdir(parents=True)
    (config / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\n"
        "cad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipeline = root / "configs/pipelines"
    pipeline.mkdir()
    (pipeline / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "r5", "quality", {})

    track_index = command.index("--web-camera-track") + 1
    assert Path(command[track_index]) == manual
    # 质量检测只运行 quality/viewer_scene/road_surface，不重跑路线拟合。
    stages_index = command.index("--stages") + 1
    assert command[stages_index] == "quality,viewer_scene,road_surface"
    assert "--skip-render" not in command
    progress_index = command.index("--progress-file") + 1
    assert Path(command[progress_index]) == run_dir / "logs/workflow/quality_progress.json"


def test_alignment_command_uses_manual_track_and_exports_viewer_scene(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r-fit"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    manual = run_dir / "01_keyframes/camera_track_manual.json"
    manual.write_text(
        '{"keyframes":['
        '{"frame":0,"source":"manual_anchor","camera":{"x":0,"y":0,"z":10}},'
        '{"frame":10,"source":"manual_anchor","camera":{"x":20,"y":0,"z":10}}'
        ']}',
        encoding="utf-8",
    )
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "design.json").write_text("{}", encoding="utf-8")
    (data / "cad").mkdir()
    (data / "cad/design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\ncad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipeline = root / "configs/pipelines"
    pipeline.mkdir()
    (pipeline / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "r-fit", "alignment", {})

    track_index = command.index("--web-camera-track") + 1
    stages_index = command.index("--stages") + 1
    assert Path(command[track_index]) == manual
    assert command[stages_index] == "alignment,viewer_scene"


def test_full_pose_alignment_resolves_semantic_trajectory_without_sparse_ply(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "design.json").write_text("{}", encoding="utf-8")
    (data / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset": "demo",
                "video": {"path": "data/demo/demo.mp4"},
                "cad": {"design_json": "data/demo/design.json", "status": "ready"},
                "defaults": {"cad_scale": 1.0, "origin_xy": [499000, 3319000]},
                "workflow": {
                    "trajectory_mode": "srt_full_pose",
                    "implementation_status": "ready",
                },
            }
        ),
        encoding="utf-8",
    )
    run = tmp_path / "runs/demo/full-pose"
    trajectory = run / "02_srt_full_pose/camera_trajectory_full_pose.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text("{}", encoding="utf-8")
    track = run / "01_keyframes/camera_track_manual.json"
    track.parent.mkdir()
    track.write_text(
        '{"keyframes":[{"frame":0,"source":"manual_anchor","camera":'
        '{"x":499000,"y":3319000,"z":60,"yaw":0,"pitch":-90,"roll":0,"fov":120}}]}',
        encoding="utf-8",
    )

    resolved = resolve_stage_inputs(tmp_path, "demo", "full-pose", {})
    command = build_stage_command(
        tmp_path,
        "demo",
        "full-pose",
        "alignment",
        {},
        application_root=Path.cwd(),
    )

    assert resolved["workflow"] == "srt_full_pose"
    assert resolved["trajectory"] == trajectory
    assert resolved["sparse_ply"] is None
    assert command[command.index("--trajectory") + 1] == str(trajectory)
    assert command[command.index("--config") + 1].endswith(
        "configs\\pipelines\\srt_full_pose_overlay.yaml"
    )
    assert "--sparse-ply" not in command


def test_alignment_command_allows_rotation_only_manual_anchor_positions(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r-overlap"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        '{"keyframes":['
        '{"frame":0,"source":"manual_anchor","camera":{"x":12,"y":34,"z":5,"yaw":0}},'
        '{"frame":10,"source":"manual_anchor","camera":{"x":12,"y":34,"z":5,"yaw":45}}'
        ']}',
        encoding="utf-8",
    )
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "cad").mkdir()
    (data / "cad/design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\ncad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipelines = root / "configs/pipelines"
    pipelines.mkdir()
    (pipelines / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "r-overlap", "alignment", {})

    assert command[command.index("--stages") + 1] == "alignment,viewer_scene"

    (run_dir / "02_sfm/sfm_stats.json").write_text(
        '{"suitable_for_3d":false,"low_parallax_or_rotation_suspected":true}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不适合三维重建"):
        build_stage_command(root, "demo", "r-overlap", "alignment", {})


def test_alignment_command_rejects_track_with_fewer_than_two_manual_keyframes(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r-one-anchor"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        '{"keyframes":[{"frame":0,"source":"manual_anchor","camera":{}}]}',
        encoding="utf-8",
    )
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "cad").mkdir()
    (data / "cad/design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\ncad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipelines = root / "configs/pipelines"
    pipelines.mkdir()
    (pipelines / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="至少需要 2 个人工关键帧"):
        build_stage_command(root, "demo", "r-one-anchor", "alignment", {})


def test_quality_command_does_not_rerun_alignment(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r-qa"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "03_alignment").mkdir()
    (run_dir / "03_alignment/alignment.json").write_text("{}", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text('{"keyframes":[]}', encoding="utf-8")
    (run_dir / "01_keyframes/keyframe_plan.json").write_text(
        '{"schema_version":"cadscene_keyframe_plan_v1","frames":[],"completed_count":0,"pending_count":0}',
        encoding="utf-8",
    )
    (run_dir / "03_alignment/sfm_camera_path.csv").write_text("\ufeffframe_index\n", encoding="utf-8")
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "cad").mkdir()
    (data / "cad/design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\ncad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipeline = root / "configs/pipelines"
    pipeline.mkdir()
    (pipeline / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "r-qa", "quality", {})

    stages_index = command.index("--stages") + 1
    assert command[stages_index] == "quality,viewer_scene,road_surface"


def test_sfm_command_requires_video_but_not_cad(tmp_path: Path) -> None:
    video = tmp_path / "data/demo/demo.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"video")

    command = build_stage_command(
        tmp_path,
        "demo",
        "sfm-only",
        "sfm",
        {"video": "/data/demo/demo.mp4"},
    )

    assert "cad-dir" not in " ".join(command)
    assert str(video) in command


def test_stage_inputs_prefer_url_options_then_dataset_manifest_then_config(tmp_path: Path) -> None:
    data = tmp_path / "data/demo"
    data.mkdir(parents=True)
    manifest_video = data / "uploaded.mp4"
    manifest_video.write_bytes(b"manifest-video")
    explicit_video = data / "explicit.mp4"
    explicit_video.write_bytes(b"explicit-video")
    (data / "design.json").write_text("{}", encoding="utf-8")
    (data / "dataset_manifest.json").write_text(
        json.dumps(
            {
                "dataset": "demo",
                "video": {"path": "data/demo/uploaded.mp4"},
                "cad": {"design_json": "data/demo/design.json", "status": "ready"},
                "defaults": {"cad_scale": 0.25, "origin_xy": [12, 34]},
            }
        ),
        encoding="utf-8",
    )
    configs = tmp_path / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: missing.mp4\ncad_dir: missing-cad\ncad_scale: 9\norigin_xy: [9, 9]\n",
        encoding="utf-8",
    )

    from_manifest = resolve_stage_inputs(tmp_path, "demo", "run", {})
    from_options = resolve_stage_inputs(
        tmp_path,
        "demo",
        "run",
        {"video": "/data/demo/explicit.mp4", "cad_scale": 0.5, "origin_xy": [1, 2]},
    )

    assert from_manifest["video"] == manifest_video
    assert from_manifest["cad_dir"] == data
    assert from_manifest["cad_scale"] == 0.25
    assert from_manifest["origin_xy"] == [12.0, 34.0]
    assert from_options["video"] == explicit_video
    assert from_options["cad_scale"] == 0.5
    assert from_options["origin_xy"] == [1.0, 2.0]


def test_render_command_passes_cad_coordinate_conversion_settings(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/r-render"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        '{"keyframes":['
        '{"frame":0,"source":"manual_anchor","camera":{"x":0,"y":0,"z":10}},'
        '{"frame":10,"source":"manual_anchor","camera":{"x":20,"y":0,"z":10}}'
        ']}',
        encoding="utf-8",
    )
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo\ncad_scale: 0.25\norigin_xy: [10, 20]\n",
        encoding="utf-8",
    )
    pipelines = root / "configs/pipelines"
    pipelines.mkdir()
    (pipelines / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "r-render", "render", {})

    assert command[command.index("--cad-scale") + 1] == "0.25"
    origin_index = command.index("--origin-xy") + 1
    assert command[origin_index : origin_index + 2] == ["10.0", "20.0"]


def test_render_command_refits_latest_manual_track_before_rendering(tmp_path: Path) -> None:
    root = tmp_path
    run_dir = root / "runs/demo/latest-render"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    manual = run_dir / "01_keyframes/camera_track_manual.json"
    manual.write_text(
        '{"keyframes":['
        '{"frame":0,"source":"manual_anchor","camera":{"x":0,"y":0,"z":10}},'
        '{"frame":10,"source":"manual_anchor","camera":{"x":20,"y":0,"z":10}}'
        ']}',
        encoding="utf-8",
    )
    data = root / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "design.json").write_text("{}", encoding="utf-8")
    configs = root / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo\n"
        "cad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipelines = root / "configs/pipelines"
    pipelines.mkdir()
    (pipelines / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")

    command = build_stage_command(root, "demo", "latest-render", "render", {})

    assert command[0:3] == [sys.executable, "-m", "cadscene.cli.run_pipeline"]
    assert command[command.index("--stages") + 1] == "alignment,render"
    assert Path(command[command.index("--web-camera-track") + 1]) == manual


def test_job_runner_starts_module_commands_from_source_root(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        pid = 1234

        def poll(self):
            return None

        def wait(self):
            return 0

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr("cadscene.workflow.job_runner.subprocess.Popen", fake_popen)
    monkeypatch.setattr("cadscene.workflow.job_runner.threading.Thread.start", lambda self: None)

    runner = JobRunner(tmp_path)
    runner.start("demo", "run-1", "render", ["python", "-m", "cadscene.cli.render_pure_rotation"])

    expected_source_root = Path(__file__).resolve().parents[2]
    assert Path(str(captured["cwd"])).resolve() == expected_source_root
