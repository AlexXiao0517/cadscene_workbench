from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import numpy as np

from cadscene.core.io import write_json
from cadscene.core.config import load_dataset_config


def _write_video(path: Path) -> None:
    import cv2

    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48))
    for _ in range(5):
        writer.write(np.zeros((48, 64, 3), dtype=np.uint8))
    writer.release()


def _write_inputs(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    video = data / "video.mp4"
    _write_video(video)
    sparse = data / "points.ply"
    sparse.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 4",
                "property float x",
                "property float y",
                "property float z",
                "property uchar red",
                "property uchar green",
                "property uchar blue",
                "end_header",
                "0 0 0 255 0 0",
                "2 0 0 0 255 0",
                "4 0 0 0 0 255",
                "6 0 0 255 255 255",
            ]
        ),
        encoding="utf-8",
    )
    trajectory = data / "camera_trajectory.json"
    poses = [
        {"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
        {"frame_index": 2, "registered": True, "center": [2, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
        {"frame_index": 4, "registered": True, "center": [4, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]},
    ]
    write_json(trajectory, {"fps": 5, "poses": poses})
    track = data / "camera_track.json"
    write_json(
        track,
        {
            "keyframes": [
                {"frame": 0, "source": "manual_keyframe", "camera": {"x": 0, "y": 0, "z": 2, "yaw": 90, "pitch": 0, "roll": 0, "fov": 70}},
                {"frame": 4, "source": "confirmed_keyframe", "camera": {"x": 4, "y": 0, "z": 2, "yaw": 90, "pitch": 0, "roll": 0, "fov": 70}},
            ]
        },
    )
    cad_dir = data / "cad"
    cad_dir.mkdir()
    write_json(cad_dir / "road_center.json", [{"points": [[0, 0], [10, 0]]}])
    dataset = tmp_path / "dataset.yaml"
    dataset.write_text(
        f"""
dataset_name: synthetic
video_path: {video.as_posix()}
cad_dir: {cad_dir.as_posix()}
cad_scale: 1.0
origin_xy: [0, 0]
default_track: {track.as_posix()}
trajectory_path: {trajectory.as_posix()}
sparse_ply_path: {sparse.as_posix()}
fps: 5.0
""",
        encoding="utf-8",
    )
    pipeline = tmp_path / "pipeline.yaml"
    pipeline.write_text(
        """
pipeline_name: synthetic_existing_sfm
stages:
  alignment:
    enabled: true
    output_subdir: 03_alignment
    inputs:
      trajectory: "${dataset.trajectory_path}"
      web_camera_track: "${dataset.default_track}"
      cad_dir: "${dataset.cad_dir}"
    params:
      cad_scale: "${dataset.cad_scale}"
      origin_xy: "${dataset.origin_xy}"
      fov: 70.0
      fov_from: trajectory
      frontend_track_step: 2
      frame_step: 1
  quality:
    enabled: true
    output_subdir: 04_quality
    inputs:
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      alignment: "${run.03_alignment.alignment}"
      web_camera_track: "${dataset.default_track}"
      trajectory: "${dataset.trajectory_path}"
      cad_dir: "${dataset.cad_dir}"
    params:
      cad_scale: "${dataset.cad_scale}"
      origin_xy: "${dataset.origin_xy}"
      quality_mode: qa
      no_suggestion_samples: true
  viewer_scene:
    enabled: true
    output_subdir: 05_viewer_scene
    inputs:
      sparse_ply: "${dataset.sparse_ply_path}"
      trajectory: "${dataset.trajectory_path}"
      alignment: "${run.03_alignment.alignment}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      quality_timeline: "${run.04_quality.quality_timeline}"
      suggestions: "${run.04_quality.keyframe_suggestions}"
    params:
      cad_scale: "${dataset.cad_scale}"
      origin_xy: "${dataset.origin_xy}"
      max_points: 100
      point_sample_mode: voxel
      voxel_size: 0.5
  road_surface:
    enabled: true
    output_subdir: 06_road_surface
    inputs:
      sparse_ply: "${dataset.sparse_ply_path}"
      trajectory: "${dataset.trajectory_path}"
      alignment: "${run.03_alignment.alignment}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
      web_camera_track: "${dataset.default_track}"
      cad_dir: "${dataset.cad_dir}"
    params:
      cad_scale: "${dataset.cad_scale}"
      origin_xy: "${dataset.origin_xy}"
      max_points: 100
      point_sample_mode: voxel
      voxel_size: 0.5
      station_bin_m: 5
      road_corridor_width: 5
      export_viewer_scene: true
  render:
    enabled: true
    output_subdir: 08_render
    inputs:
      video: "${dataset.video_path}"
      cad_dir: "${dataset.cad_dir}"
      sfm_camera_path: "${run.03_alignment.sfm_camera_path}"
    params:
      debug_scale: 1.0
      overlay_linewidth: 2
      overlay_alpha: 0.8
      faded_overlay: true
      max_distance_m: 100
      fade_start_m: 10
""",
        encoding="utf-8",
    )
    return dataset, pipeline


def test_run_pipeline_help_runs() -> None:
    result = subprocess.run([sys.executable, "-m", "cadscene.cli.run_pipeline", "--help"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "--dry-run" in result.stdout
    assert "--skip-render" in result.stdout


def test_run_pipeline_dry_run_writes_inputs_commands_and_manifest(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.run_pipeline", "--dataset", str(dataset), "--config", str(pipeline), "--run-id", "dry", "--output-root", str(output_root), "--dry-run"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_dir = output_root / "synthetic" / "dry"
    assert (run_dir / "00_inputs" / "dataset.resolved.json").exists()
    assert (run_dir / "00_inputs" / "pipeline.resolved.json").exists()
    assert (run_dir / "logs" / "commands.txt").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["stages"][-1]["stage_name"] == "dry_run"


def test_run_pipeline_accepts_uploaded_dataset_without_static_yaml(tmp_path: Path) -> None:
    dataset_path, pipeline = _write_inputs(tmp_path)
    dataset = load_dataset_config(dataset_path)
    output_root = tmp_path / "runs"
    dynamic_name = "uploaded-dynamic"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            dynamic_name,
            "--config",
            str(pipeline),
            "--run-id",
            "route-fit",
            "--output-root",
            str(output_root),
            "--trajectory",
            str(dataset["trajectory_path"]),
            "--sparse-ply",
            str(dataset["sparse_ply_path"]),
            "--web-camera-track",
            str(dataset["default_track"]),
            "--video",
            str(dataset["video_path"]),
            "--cad-dir",
            str(dataset["cad_dir"]),
            "--cad-scale",
            "1.0",
            "--origin-xy",
            "0",
            "0",
            "--stages",
            "alignment,viewer_scene",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    resolved = json.loads(
        (output_root / dynamic_name / "route-fit/00_inputs/dataset.resolved.json").read_text(encoding="utf-8")
    )
    assert resolved["dataset_name"] == dynamic_name
    assert resolved["default_track"] == dataset["default_track"]


def test_run_pipeline_synthetic_full_pipeline_and_viewer_url(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    progress_file = tmp_path / "adapter_progress.json"
    result = subprocess.run(
        [sys.executable, "-m", "cadscene.cli.run_pipeline", "--dataset", str(dataset), "--config", str(pipeline), "--run-id", "full", "--output-root", str(output_root), "--progress-file", str(progress_file)],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_dir = output_root / "synthetic" / "full"
    assert (run_dir / "03_alignment" / "alignment.json").exists()
    assert (run_dir / "04_quality" / "quality_timeline.csv").exists()
    assert (run_dir / "05_viewer_scene" / "sfm_viewer_scene.json").exists()
    assert (run_dir / "06_road_surface" / "sfm_geometry_summary.json").exists()
    assert (run_dir / "08_render" / "sfm_align_overlay.mp4").exists()
    progress = json.loads(progress_file.read_text(encoding="utf-8"))
    assert progress["stage"] == "rendering_frames"
    assert progress["fraction"] == 0.95
    assert "dataset=synthetic&runId=full" in (run_dir / "reports" / "viewer_url.txt").read_text(encoding="utf-8")


def test_pipeline_without_road_centerline_skips_diagnostics_but_keeps_main_flow(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    cad_dir = tmp_path / "data" / "cad"
    (cad_dir / "road_center.json").unlink()
    write_json(
        cad_dir / "design.json",
        {
            "meta": {"coordinate_mode": "cad_world"},
            "layers": [
                {
                    "name": "future_building",
                    "kind": "ref",
                    "entities": [
                        {"world_points": [[0, 0], [10, 0], [10, 5], [0, 5], [0, 0]]}
                    ],
                }
            ],
        },
    )
    output_root = tmp_path / "runs"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            str(pipeline),
            "--run-id",
            "no-centerline",
            "--output-root",
            str(output_root),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_dir = output_root / "synthetic" / "no-centerline"
    assert (run_dir / "03_alignment" / "alignment.json").exists()
    assert (run_dir / "04_quality" / "quality_timeline.csv").exists()
    assert (run_dir / "05_viewer_scene" / "sfm_viewer_scene.json").exists()
    assert (run_dir / "08_render" / "sfm_align_overlay.mp4").exists()
    assert not (run_dir / "06_road_surface").exists()

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    road_stage = next(stage for stage in manifest["stages"] if stage["stage_name"] == "road_surface")
    assert road_stage["status"] == "skipped"
    assert road_stage["metrics"]["has_road_centerline"] is False
    assert road_stage["metrics"]["road_centerline_source"] == "none"

    status = json.loads((run_dir / "job_status.json").read_text(encoding="utf-8"))
    assert "未检测到道路中心线" in status["message"]
    assert status["status"] != "failed"
    summary = (run_dir / "reports" / "run_summary.md").read_text(encoding="utf-8")
    assert "已跳过道路表面诊断" in summary


def test_run_pipeline_stages_and_skip_render(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            str(pipeline),
            "--run-id",
            "partial",
            "--output-root",
            str(output_root),
            "--stages",
            "alignment,quality",
            "--skip-render",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_dir = output_root / "synthetic" / "partial"
    assert (run_dir / "03_alignment" / "alignment.json").exists()
    assert (run_dir / "04_quality" / "quality_timeline.csv").exists()
    assert not (run_dir / "08_render").exists()


def test_alignment_and_viewer_scene_do_not_require_quality_suggestions(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            str(pipeline),
            "--run-id",
            "alignment-only",
            "--output-root",
            str(output_root),
            "--stages",
            "alignment,viewer_scene",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    run_dir = output_root / "synthetic" / "alignment-only"
    scene = json.loads((run_dir / "05_viewer_scene" / "sfm_viewer_scene.json").read_text(encoding="utf-8"))
    assert scene["suggestions"] == []
    assert not (run_dir / "04_quality" / "keyframe_suggestions.json").exists()


def test_run_pipeline_missing_sparse_ply_non_dry_run_has_clear_error(tmp_path: Path) -> None:
    dataset, pipeline = _write_inputs(tmp_path)
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            str(pipeline),
            "--run-id",
            "missing",
            "--output-root",
            str(output_root),
            "--sparse-ply",
            str(tmp_path / "missing.ply"),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "viewer_scene.sparse_ply" in result.stderr or "road_surface.sparse_ply" in result.stderr


def test_with_sfm_pipeline_dry_run_contains_sfm_stage(tmp_path: Path) -> None:
    dataset, _ = _write_inputs(tmp_path)
    config = Path("configs/pipelines/sfm_overlay_with_sfm.yaml")
    output_root = tmp_path / "runs"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            str(config),
            "--run-id",
            "with-sfm",
            "--output-root",
            str(output_root),
            "--stages",
            "sfm",
            "--dry-run",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = (output_root / "synthetic" / "with-sfm" / "logs" / "commands.txt").read_text(encoding="utf-8")
    assert "cadscene.cli.run_sfm" in commands


def test_with_sfm_config_resolves_generated_trajectory_and_points(tmp_path: Path) -> None:
    from cadscene.core.config import load_pipeline_config, resolve_pipeline_references

    pipeline = load_pipeline_config("configs/pipelines/sfm_overlay_with_sfm.yaml")
    resolved = resolve_pipeline_references(
        pipeline,
        {"dataset_name": "demo", "video_path": "video.mp4", "default_track": "track.json", "cad_dir": "cad", "cad_scale": 1.0, "origin_xy": [0, 0]},
        output_root=tmp_path / "runs",
        dataset_name="demo",
        run_id="r1",
    )

    assert resolved["stages"]["alignment"]["inputs"]["trajectory"].endswith(
        "02_sfm/camera_trajectory.json"
    )
    assert resolved["stages"]["viewer_scene"]["inputs"]["sparse_ply"].endswith(
        "02_sfm/sparse_points.ply"
    )


def test_product_pipeline_keeps_dense_viewer_point_cloud() -> None:
    from cadscene.core.config import load_pipeline_config

    for name in ("sfm_overlay_existing_sfm.yaml", "sfm_overlay_with_sfm.yaml"):
        pipeline = load_pipeline_config(Path("configs/pipelines") / name)
        params = pipeline["stages"]["viewer_scene"]["params"]
        assert params["max_points"] == 250000
        assert params["point_sample_mode"] == "uniform"


def test_with_sfm_non_dry_run_missing_video_is_clear(tmp_path: Path) -> None:
    dataset, _ = _write_inputs(tmp_path)
    missing = tmp_path / "missing.mp4"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "cadscene.cli.run_pipeline",
            "--dataset",
            str(dataset),
            "--config",
            "configs/pipelines/sfm_overlay_with_sfm.yaml",
            "--run-id",
            "missing-video",
            "--output-root",
            str(tmp_path / "runs"),
            "--video",
            str(missing),
            "--stages",
            "sfm",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "sfm.video" in result.stderr or "video does not exist" in result.stderr
