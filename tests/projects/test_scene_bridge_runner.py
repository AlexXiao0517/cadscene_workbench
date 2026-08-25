from __future__ import annotations

import csv
from fractions import Fraction
import json
from pathlib import Path

from cadscene.projects.scene_bridge_runner import (
    SceneBridgeInputs,
    run_scene_bridge,
    validate_scene_bridge_candidate,
)
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp


PTS = tuple(range(0, 9000, 1000))


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_frame_map(path: Path, clip_id: str, points: tuple[int, ...]) -> None:
    _write_json(
        path,
        {
            "schema_version": 1,
            "interval_semantics": "half_open",
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "source_start_pts": 0,
            "source_end_pts_exclusive": 9000,
            "clips": [
                {
                    "clip_id": clip_id,
                    "source_start_pts": points[0],
                    "source_end_pts_exclusive": points[-1] + 1000,
                    "frames": [
                        {"ordinal": PTS.index(pts), "pts": pts} for pts in points
                    ],
                }
            ],
        },
    )


def _trajectory(path: Path, frame_count: int) -> None:
    _write_json(
        path,
        {
            "schema_version": "cadscene_sfm_trajectory_v1",
            "fps": 1.0,
            "poses": [
                {"frame_index": frame, "registered": True}
                for frame in range(frame_count)
            ],
        },
    )


def _camera_row(frame: int) -> dict[str, object]:
    return {
        "camera_x": float(frame),
        "camera_y": float(frame + 1),
        "camera_z": float(frame + 2),
        "yaw": float(frame + 3),
        "pitch": 20.0,
        "roll": 1.0,
        "fov": 70.0,
        "status": "ok",
        "frame_index": frame,
        "path_source": "segment_anchor",
    }


def _write_camera_path(path: Path, frame_count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_camera_row(frame) for frame in range(frame_count)]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _flag(command: tuple[str, ...], name: str) -> str:
    return command[command.index(name) + 1]


class FakeCommands:
    def __init__(self) -> None:
        self.stage_names: list[str] = []
        self.commands: dict[str, tuple[str, ...]] = {}

    def __call__(self, phase: str, command: tuple[str, ...]) -> None:
        self.stage_names.append(phase)
        self.commands[phase] = command
        if phase == "source_alignment":
            run = (
                Path(_flag(command, "--output-root"))
                / _flag(command, "--dataset")
                / _flag(command, "--run-id")
            )
            _write_camera_path(run / "03_alignment/sfm_camera_path.csv", 5)
            _write_json(run / "03_alignment/alignment.json", {"sim3": {}})
            _write_json(run / "05_viewer_scene/sfm_viewer_scene.json", {})
            return
        if phase == "solve_export":
            output = Path(_flag(command, "--output-dir"))
            manifest = json.loads(Path(_flag(command, "--manifest")).read_text())
            clip = manifest["clips"][0]
            points = tuple(
                pts
                for pts in PTS
                if clip["source_start_pts"] <= pts < clip["source_end_pts_exclusive"]
            )
            output.mkdir(parents=True, exist_ok=True)
            (output / "target-solve.mp4").write_bytes(b"solve-video")
            _write_frame_map(output / "clip_frame_map.json", "target-solve", points)
            return
        if phase == "target_sfm":
            run = (
                Path(_flag(command, "--output-root"))
                / _flag(command, "--dataset")
                / _flag(command, "--run-id")
            )
            _trajectory(run / "02_sfm/camera_trajectory.json", 7)
            sparse = run / "02_sfm/sparse_points.ply"
            sparse.parent.mkdir(parents=True, exist_ok=True)
            sparse.write_bytes(b"ply")
            return
        if phase == "solve_alignment":
            run = (
                Path(_flag(command, "--output-root"))
                / _flag(command, "--dataset")
                / _flag(command, "--run-id")
            )
            _write_camera_path(run / "03_alignment/sfm_camera_path.csv", 7)
            _write_json(run / "03_alignment/alignment.json", {"sim3": {}})
            _write_json(run / "05_viewer_scene/sfm_viewer_scene.json", {})
            return
        if phase == "core_alignment":
            run = (
                Path(_flag(command, "--output-root"))
                / _flag(command, "--dataset")
                / _flag(command, "--run-id")
            )
            _write_camera_path(run / "03_alignment/sfm_camera_path.csv", 5)
            _write_json(run / "03_alignment/alignment.json", {"sim3": {}})
            _write_json(
                run / "03_alignment/camera_track_pred.json",
                {"keyframes": [{"frame": 0}, {"frame": 4}]},
            )
            _write_json(run / "05_viewer_scene/sfm_viewer_scene.json", {})
            return
        raise AssertionError(f"unexpected phase: {phase}")


def _source_index() -> DecodedFrameIndex:
    return DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(index, pts, 1000, "pts")
            for index, pts in enumerate(PTS)
        ),
    )


def _inputs(tmp_path: Path) -> SceneBridgeInputs:
    source_video = tmp_path / "source.mp4"
    source_core_video = tmp_path / "source-core.mp4"
    target_core_video = tmp_path / "target-core.mp4"
    for path in (source_video, source_core_video, target_core_video):
        path.write_bytes(b"video")
    source_map = tmp_path / "source-map.json"
    target_map = tmp_path / "target-map.json"
    _write_frame_map(source_map, "source", (0, 1000, 2000, 3000, 4000))
    _write_frame_map(target_map, "target", (4000, 5000, 6000, 7000, 8000))
    source_trajectory = tmp_path / "source-trajectory.json"
    target_trajectory = tmp_path / "target-trajectory.json"
    _trajectory(source_trajectory, 5)
    _trajectory(target_trajectory, 5)
    source_sparse = tmp_path / "source.ply"
    target_sparse = tmp_path / "target.ply"
    source_sparse.write_bytes(b"ply")
    target_sparse.write_bytes(b"ply")
    source_track = tmp_path / "source-track.json"
    _write_json(
        source_track,
        {
            "version": 1,
            "fps": 1.0,
            "keyframes": [
                {
                    "frame": frame,
                    "time": float(frame),
                    "source": "manual_anchor",
                    "camera": {
                        "x": float(frame),
                        "y": 1.0,
                        "z": 2.0,
                        "yaw": 3.0,
                        "pitch": -20.0,
                        "roll": 1.0,
                        "fov": 70.0,
                    },
                }
                for frame in (0, 4)
            ],
        },
    )
    cad_dir = tmp_path / "cad"
    cad_dir.mkdir()
    (cad_dir / "design.json").write_text("{}", encoding="utf-8")
    application_root = tmp_path / "application"
    pipeline = application_root / "configs/pipelines/sfm_overlay_existing_sfm.yaml"
    pipeline.parent.mkdir(parents=True)
    pipeline.write_text("stages: {}\n", encoding="utf-8")
    return SceneBridgeInputs(
        identity={
            "schema_version": 1,
            "algorithm_version": "scene-overlap-v1",
            "project_id": "project-1",
            "source_clip_id": "source",
            "target_clip_id": "target",
            "direction": "down",
            "operation_id": "operation-1",
        },
        application_root=application_root,
        attempt_directory=tmp_path / "attempt",
        source_video_path=source_video,
        source_core_video_path=source_core_video,
        source_core_frame_map_path=source_map,
        source_manual_track_path=source_track,
        source_trajectory_path=source_trajectory,
        source_sparse_ply_path=source_sparse,
        target_core_video_path=target_core_video,
        target_core_frame_map_path=target_map,
        target_core_trajectory_path=target_trajectory,
        target_core_sparse_ply_path=target_sparse,
        cad_dir=cad_dir,
        cad_scale=1.0,
        origin_xy=(0.0, 0.0),
        overlap_seconds=Fraction(2, 1),
    )


def test_bridge_runner_reuses_alignment_and_stops_at_route_refinement(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()

    candidate = run_scene_bridge(
        _inputs(tmp_path),
        command_runner=commands,
        source_frame_probe=lambda _path: _source_index(),
    )

    manifest = json.loads((candidate / "scene_bridge_manifest.json").read_text())
    seed = json.loads((candidate / "camera_track_seed.json").read_text())
    assert manifest["status"] == "awaiting_route_refinement"
    assert manifest["anchor_source_pts"] == [2000, 4000]
    assert commands.stage_names == [
        "source_alignment",
        "solve_export",
        "target_sfm",
        "solve_alignment",
        "core_alignment",
    ]
    assert _flag(commands.commands["solve_export"], "--max-duration-seconds") == "8"
    assert [row["source_pts"] for row in seed["keyframes"]] == [4000, 8000]
    assert (candidate / "core_alignment/03_alignment/alignment.json").is_file()
    assert (
        candidate / "core_alignment/03_alignment/camera_track_pred.json"
    ).is_file()
    assert not (candidate / "core_alignment/04_quality").exists()


def test_candidate_validation_rejects_duplicate_anchor_pts(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(
        inputs,
        command_runner=FakeCommands(),
        source_frame_probe=lambda _path: _source_index(),
    )
    manifest_path = candidate / "scene_bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["anchor_source_pts"] = [2000, 2000]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = validate_scene_bridge_candidate(candidate, inputs.identity)

    assert result.status == "failed"
    assert "distinct" in str(result.error)


def test_candidate_validation_rejects_missing_core_alignment(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(
        inputs,
        command_runner=FakeCommands(),
        source_frame_probe=lambda _path: _source_index(),
    )
    (candidate / "core_alignment/03_alignment/alignment.json").unlink()

    result = validate_scene_bridge_candidate(candidate, inputs.identity)

    assert result.status == "failed"
    assert "missing" in str(result.error)


def test_candidate_validation_rejects_changed_fitted_camera_track(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(
        inputs,
        command_runner=FakeCommands(),
        source_frame_probe=lambda _path: _source_index(),
    )
    (candidate / "core_alignment/03_alignment/camera_track_pred.json").write_text(
        '{"keyframes": []}', encoding="utf-8"
    )

    result = validate_scene_bridge_candidate(candidate, inputs.identity)

    assert result.status == "failed"
    assert "hash mismatch" in str(result.error)


def test_scene_bridge_inputs_round_trip_exact_fraction_and_paths(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    path = tmp_path / "inputs.json"
    inputs.write_json(path)

    restored = SceneBridgeInputs.from_json(path)

    assert restored == inputs
    assert restored.overlap_seconds == Fraction(2, 1)


def test_runner_returns_failed_validation_for_changed_identity(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(
        inputs,
        command_runner=FakeCommands(),
        source_frame_probe=lambda _path: _source_index(),
    )

    result = validate_scene_bridge_candidate(
        candidate,
        {**inputs.identity, "operation_id": "changed"},
    )

    assert result.status == "failed"
    assert "identity" in str(result.error)


def test_runner_rejects_command_failure_without_candidate(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    def fail_sfm(phase: str, command: tuple[str, ...]) -> None:
        if phase == "target_sfm":
            raise RuntimeError("sfm failed")
        FakeCommands()(phase, command)

    try:
        run_scene_bridge(
            inputs,
            command_runner=fail_sfm,
            source_frame_probe=lambda _path: _source_index(),
        )
    except RuntimeError as exc:
        assert "sfm failed" in str(exc)
    else:  # pragma: no cover - explicit assertion keeps failure readable
        raise AssertionError("target SfM failure must escape")
    assert not (inputs.attempt_directory / "candidate").exists()
