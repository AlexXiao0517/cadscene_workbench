from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.sfm.trajectory_partition import partition_sfm_trajectory


def _trajectory(path: Path, frames: tuple[int, ...]) -> Path:
    path.write_text(
        json.dumps(
            {
                "video_width": 1920,
                "video_height": 1080,
                "fps": 25.0,
                "poses": [
                    {
                        "frame_index": frame,
                        "registered": True,
                        "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
                    }
                    for frame in frames
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def _frame_map(path: Path, clip_id: str, points: tuple[int, ...]) -> Path:
    path.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "clips": [
                    {
                        "clip_id": clip_id,
                        "source_start_pts": points[0],
                        "source_end_pts_exclusive": points[-1] + 10,
                        "frames": [
                            {
                                "output_frame_ordinal": ordinal,
                                "source_decoded_frame_ordinal": ordinal + 8,
                                "source_pts": pts,
                            }
                            for ordinal, pts in enumerate(points)
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_partition_binds_solve_poses_to_source_pts_and_reindexes_core(
    tmp_path: Path,
) -> None:
    raw = _trajectory(tmp_path / "raw.json", (0, 5, 10, 15))
    solve_map = _frame_map(
        tmp_path / "solve-map.json",
        "clip-solve",
        tuple(range(80, 240, 10)),
    )
    core_map = _frame_map(
        tmp_path / "core-map.json",
        "clip-core",
        tuple(range(120, 210, 10)),
    )

    result = partition_sfm_trajectory(
        raw,
        solve_map,
        core_map,
        solve_output_path=tmp_path / "camera_trajectory_solve.json",
        core_output_path=tmp_path / "camera_trajectory.json",
    )

    solve = json.loads(result.solve_path.read_text(encoding="utf-8"))
    core = json.loads(result.core_path.read_text(encoding="utf-8"))
    assert [pose["source_pts"] for pose in solve["poses"]] == [80, 130, 180, 230]
    assert [
        (pose["frame_index"], pose["source_pts"]) for pose in core["poses"]
    ] == [(1, 130), (6, 180)]
    assert solve["video_width"] == core["video_width"] == 1920
    assert result.solve_pose_count == 4
    assert result.core_registered_pose_count == 2


def test_partition_rejects_pose_frame_outside_solve_map(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside solve frame map"):
        partition_sfm_trajectory(
            _trajectory(tmp_path / "raw.json", (0, 99)),
            _frame_map(tmp_path / "solve-map.json", "solve", (80, 90, 100)),
            _frame_map(tmp_path / "core-map.json", "core", (90, 100)),
            solve_output_path=tmp_path / "solve.json",
            core_output_path=tmp_path / "core.json",
        )


def test_partition_requires_two_registered_core_poses(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="two registered core poses"):
        partition_sfm_trajectory(
            _trajectory(tmp_path / "raw.json", (0, 2)),
            _frame_map(tmp_path / "solve-map.json", "solve", (80, 90, 100)),
            _frame_map(tmp_path / "core-map.json", "core", (90, 100)),
            solve_output_path=tmp_path / "solve.json",
            core_output_path=tmp_path / "core.json",
        )
