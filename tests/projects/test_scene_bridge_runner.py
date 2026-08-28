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
            _write_camera_path(run / "03_alignment/sfm_camera_path.csv", 7)
            _write_json(run / "03_alignment/alignment.json", {"sim3": {}})
            _write_json(run / "05_viewer_scene/sfm_viewer_scene.json", {})
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


def _inputs(tmp_path: Path) -> SceneBridgeInputs:
    source_solve_video = tmp_path / "source-solve.mp4"
    target_solve_video = tmp_path / "target-solve.mp4"
    target_core_video = tmp_path / "target-core.mp4"
    for path in (source_solve_video, target_solve_video, target_core_video):
        path.write_bytes(b"video")
    source_core_map = tmp_path / "source-core-map.json"
    source_solve_map = tmp_path / "source-solve-map.json"
    target_solve_map = tmp_path / "target-solve-map.json"
    target_core_map = tmp_path / "target-core-map.json"
    _write_frame_map(source_core_map, "source", (0, 1000, 2000, 3000, 4000))
    _write_frame_map(
        source_solve_map, "source", (0, 1000, 2000, 3000, 4000, 5000, 6000)
    )
    _write_frame_map(
        target_solve_map, "target", (2000, 3000, 4000, 5000, 6000, 7000, 8000)
    )
    _write_frame_map(
        target_core_map, "target", (4000, 5000, 6000, 7000, 8000)
    )
    source_solve_trajectory = tmp_path / "source-solve-trajectory.json"
    target_solve_trajectory = tmp_path / "target-solve-trajectory.json"
    target_core_trajectory = tmp_path / "target-core-trajectory.json"
    _trajectory(source_solve_trajectory, 7)
    _trajectory(target_solve_trajectory, 7)
    _trajectory(target_core_trajectory, 5)
    source_sparse = tmp_path / "source.ply"
    target_solve_sparse = tmp_path / "target-solve.ply"
    target_core_sparse = tmp_path / "target-core.ply"
    source_sparse.write_bytes(b"ply")
    target_solve_sparse.write_bytes(b"ply")
    target_core_sparse.write_bytes(b"ply")
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
            "algorithm_version": "scene-overlap-precomputed-v2",
            "project_id": "project-1",
            "source_clip_id": "source",
            "target_clip_id": "target",
            "direction": "down",
            "operation_id": "operation-1",
        },
        application_root=application_root,
        attempt_directory=tmp_path / "attempt",
        source_core_frame_map_path=source_core_map,
        source_manual_track_path=source_track,
        source_solve_video_path=source_solve_video,
        source_solve_frame_map_path=source_solve_map,
        source_solve_trajectory_path=source_solve_trajectory,
        source_sparse_ply_path=source_sparse,
        target_solve_video_path=target_solve_video,
        target_solve_frame_map_path=target_solve_map,
        target_solve_trajectory_path=target_solve_trajectory,
        target_solve_sparse_ply_path=target_solve_sparse,
        target_core_video_path=target_core_video,
        target_core_frame_map_path=target_core_map,
        target_core_trajectory_path=target_core_trajectory,
        target_core_sparse_ply_path=target_core_sparse,
        cad_dir=cad_dir,
        cad_scale=1.0,
        origin_xy=(0.0, 0.0),
        overlap_seconds=Fraction(2, 1),
    )


def test_bridge_runner_reuses_alignment_and_stops_at_route_refinement(
    tmp_path: Path,
) -> None:
    commands = FakeCommands()

    candidate = run_scene_bridge(_inputs(tmp_path), command_runner=commands)

    manifest = json.loads((candidate / "scene_bridge_manifest.json").read_text())
    seed = json.loads((candidate / "camera_track_seed.json").read_text())
    assert manifest["status"] == "awaiting_route_refinement"
    assert manifest["anchor_source_pts"] == [2000, 6000]
    assert commands.stage_names == [
        "source_alignment",
        "solve_alignment",
        "core_alignment",
    ]
    flat = [token for command in commands.commands.values() for token in command]
    assert "cadscene.cli.run_sfm" not in flat
    assert "cadscene.cli.export_video_clips" not in flat
    assert [row["source_pts"] for row in seed["keyframes"]] == [4000, 8000]
    assert (candidate / "core_alignment/03_alignment/alignment.json").is_file()
    assert (
        candidate / "core_alignment/03_alignment/camera_track_pred.json"
    ).is_file()
    assert not (candidate / "core_alignment/04_quality").exists()


def test_candidate_validation_rejects_duplicate_anchor_pts(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(inputs, command_runner=FakeCommands())
    manifest_path = candidate / "scene_bridge_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["anchor_source_pts"] = [2000, 2000]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = validate_scene_bridge_candidate(candidate, inputs.identity)

    assert result.status == "failed"
    assert "distinct" in str(result.error)


def test_candidate_validation_rejects_missing_core_alignment(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(inputs, command_runner=FakeCommands())
    (candidate / "core_alignment/03_alignment/alignment.json").unlink()

    result = validate_scene_bridge_candidate(candidate, inputs.identity)

    assert result.status == "failed"
    assert "missing" in str(result.error)


def test_candidate_validation_rejects_changed_fitted_camera_track(
    tmp_path: Path,
) -> None:
    inputs = _inputs(tmp_path)
    candidate = run_scene_bridge(inputs, command_runner=FakeCommands())
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
    candidate = run_scene_bridge(inputs, command_runner=FakeCommands())

    result = validate_scene_bridge_candidate(
        candidate,
        {**inputs.identity, "operation_id": "changed"},
    )

    assert result.status == "failed"
    assert "identity" in str(result.error)


def test_runner_rejects_command_failure_without_candidate(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)

    def fail_sfm(phase: str, command: tuple[str, ...]) -> None:
        if phase == "solve_alignment":
            raise RuntimeError("alignment failed")
        FakeCommands()(phase, command)

    try:
        run_scene_bridge(inputs, command_runner=fail_sfm)
    except RuntimeError as exc:
        assert "alignment failed" in str(exc)
    else:  # pragma: no cover - explicit assertion keeps failure readable
        raise AssertionError("target solve alignment failure must escape")
    assert not (inputs.attempt_directory / "candidate").exists()
