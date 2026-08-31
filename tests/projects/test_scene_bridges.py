from __future__ import annotations

from fractions import Fraction

import pytest

from cadscene.projects.scene_bridges import (
    FrameMap,
    SceneBridgeUnavailable,
    build_core_seed,
    derive_scene_solve_interval,
    derive_solve_interval,
    remap_core_track_to_solve,
    scene_bridge_neighbor,
    select_overlap_anchors,
)
from cadscene.projects.models import ClipDefinition
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp


def _index(points: tuple[int, ...], *, duration: int = 40) -> DecodedFrameIndex:
    return DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(
                ordinal=index,
                pts=pts,
                duration_pts=duration,
                timestamp_source="pts",
            )
            for index, pts in enumerate(points)
        ),
    )


def _map(
    clip_id: str,
    points: tuple[int, ...],
    *,
    source_ordinals: tuple[int, ...] | None = None,
) -> FrameMap:
    ordinals = source_ordinals or tuple(range(len(points)))
    return FrameMap.from_dict(
        {
            "schema_version": 1,
            "interval_semantics": "half_open",
            "source_time_base": {"numerator": 1, "denominator": 1000},
            "clips": [
                {
                    "clip_id": clip_id,
                    "source_start_pts": points[0],
                    "source_end_pts_exclusive": points[-1] + 40,
                    "frames": [
                        {"ordinal": ordinal, "pts": pts}
                        for ordinal, pts in zip(ordinals, points, strict=True)
                    ],
                }
            ],
        },
        clip_id=clip_id,
    )


def _camera(value: float) -> dict[str, float]:
    return {
        "x": value,
        "y": value + 1.0,
        "z": value + 2.0,
        "yaw": value + 3.0,
        "pitch": -20.0,
        "roll": 1.0,
        "fov": 70.0,
    }


def _clip(
    clip_id: str,
    *,
    scene_index: int,
    segment_index: int,
    start_pts: int,
    end_pts_exclusive: int,
    render_order: int,
) -> ClipDefinition:
    return ClipDefinition.from_analysis(
        {
            "clip_id": clip_id,
            "analysis_revision": "analysis-1",
            "scene_index": scene_index,
            "segment_index": segment_index,
            "render_order": render_order,
            "source_start_pts": start_pts,
            "source_end_pts_exclusive": end_pts_exclusive,
            "recommended_workflow": "sfm_only",
        }
    )


def _second_index(end_pts_exclusive: int) -> DecodedFrameIndex:
    return _index(tuple(range(0, end_pts_exclusive, 1000)), duration=1000)


def test_middle_clip_expands_four_seconds_each_side_inside_its_scene() -> None:
    clips = (
        _clip("clip-1", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=60_000, render_order=0),
        _clip("clip-2", scene_index=1, segment_index=2, start_pts=60_000, end_pts_exclusive=120_000, render_order=1),
        _clip("clip-3", scene_index=1, segment_index=3, start_pts=120_000, end_pts_exclusive=180_000, render_order=2),
        _clip("clip-4", scene_index=2, segment_index=1, start_pts=180_000, end_pts_exclusive=240_000, render_order=3),
    )

    interval = derive_scene_solve_interval(
        clips=clips,
        clip_id="clip-2",
        source_frame_index=_second_index(240_000),
    )

    assert interval.start_pts == 56_000
    assert interval.end_pts_exclusive == 124_000
    assert interval.core_start_index == 4
    assert interval.core_end_index_exclusive == 64


def test_scene_edge_never_uses_frames_from_the_next_scene() -> None:
    clips = (
        _clip("scene-1-a", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=6_000, render_order=0),
        _clip("scene-1-b", scene_index=1, segment_index=2, start_pts=6_000, end_pts_exclusive=12_000, render_order=1),
        _clip("scene-2-a", scene_index=2, segment_index=1, start_pts=12_000, end_pts_exclusive=18_000, render_order=2),
        _clip("scene-2-b", scene_index=2, segment_index=2, start_pts=18_000, end_pts_exclusive=24_000, render_order=3),
    )

    interval = derive_scene_solve_interval(
        clips=clips,
        clip_id="scene-1-b",
        source_frame_index=_second_index(24_000),
    )

    assert interval.start_pts == 2_000
    assert interval.end_pts_exclusive == 12_000
    assert 12_000 not in interval.frame_pts


def test_single_segment_scene_has_core_only_and_no_neighbors() -> None:
    clips = (
        _clip("single", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=6_000, render_order=0),
        _clip("next-a", scene_index=2, segment_index=1, start_pts=6_000, end_pts_exclusive=12_000, render_order=1),
        _clip("next-b", scene_index=2, segment_index=2, start_pts=12_000, end_pts_exclusive=18_000, render_order=2),
    )

    interval = derive_scene_solve_interval(
        clips=clips,
        clip_id="single",
        source_frame_index=_second_index(18_000),
    )

    assert interval.start_pts == interval.core_start_pts == 0
    assert interval.end_pts_exclusive == interval.core_end_pts_exclusive == 6_000
    assert scene_bridge_neighbor(clips, "single", "up") is None
    assert scene_bridge_neighbor(clips, "single", "down") is None


def test_scene_bridge_neighbors_are_only_same_scene_adjacent_segments() -> None:
    clips = (
        _clip("a", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=6_000, render_order=0),
        _clip("b", scene_index=1, segment_index=2, start_pts=6_000, end_pts_exclusive=12_000, render_order=1),
        _clip("c", scene_index=2, segment_index=1, start_pts=12_000, end_pts_exclusive=18_000, render_order=2),
    )

    assert scene_bridge_neighbor(clips, "a", "down") == clips[1]
    assert scene_bridge_neighbor(clips, "b", "up") == clips[0]
    assert scene_bridge_neighbor(clips, "b", "down") is None
    with pytest.raises(ValueError, match="direction must be up or down"):
        scene_bridge_neighbor(clips, "a", "left")


def test_vfr_scene_expansion_snaps_outward_to_real_decoded_pts() -> None:
    clips = (
        _clip("a", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=5_200, render_order=0),
        _clip("b", scene_index=1, segment_index=2, start_pts=5_200, end_pts_exclusive=8_000, render_order=1),
        _clip("c", scene_index=1, segment_index=3, start_pts=8_000, end_pts_exclusive=15_000, render_order=2),
    )
    points = (
        0,
        700,
        1_000,
        1_300,
        5_200,
        5_850,
        6_600,
        7_300,
        7_950,
        8_000,
        9_100,
        10_250,
        11_950,
        12_200,
        14_900,
    )

    interval = derive_scene_solve_interval(
        clips=clips,
        clip_id="b",
        source_frame_index=_index(points, duration=100),
    )

    assert interval.start_pts == 1_000
    assert interval.end_pts_exclusive == 12_200
    assert interval.frame_pts[0] == 1_000
    assert interval.frame_pts[-1] == 11_950


def test_scene_contract_rejects_duplicate_segment_indices() -> None:
    clips = (
        _clip("a", scene_index=1, segment_index=1, start_pts=0, end_pts_exclusive=6_000, render_order=0),
        _clip("b", scene_index=1, segment_index=1, start_pts=6_000, end_pts_exclusive=12_000, render_order=1),
    )

    with pytest.raises(SceneBridgeUnavailable, match="segment indices must be unique"):
        derive_scene_solve_interval(
            clips=clips,
            clip_id="a",
            source_frame_index=_second_index(12_000),
        )


def test_manual_core_track_remaps_to_solve_ordinals_by_exact_source_pts() -> None:
    core_map = _map("core", (4_000, 5_000, 6_000))
    solve_map = _map("solve", (2_000, 3_000, 4_000, 5_000, 6_000, 7_000))
    track = {
        "fps": 1.0,
        "keyframes": [
            {"frame": 0, "time": 0.0, "camera": _camera(1.0)},
            {"frame": 2, "time": 2.0, "camera": _camera(2.0)},
        ],
    }

    remapped = remap_core_track_to_solve(track, core_map, solve_map)

    assert [item["frame"] for item in remapped["keyframes"]] == [2, 4]
    assert [item["source_pts"] for item in remapped["keyframes"]] == [4_000, 6_000]
    assert [item["time"] for item in remapped["keyframes"]] == [2.0, 4.0]


def test_manual_core_track_rejects_pts_missing_from_solve_map() -> None:
    with pytest.raises(SceneBridgeUnavailable, match="missing from solve frame map"):
        remap_core_track_to_solve(
            {
                "fps": 1.0,
                "keyframes": [
                    {"frame": 1, "time": 1.0, "camera": _camera(1.0)}
                ],
            },
            _map("core", (4_000, 5_000, 6_000)),
            _map("solve", (4_000, 6_000)),
        )


def test_solve_interval_uses_exact_vfr_pts_and_clamps_to_source_bounds() -> None:
    frame_index = _index((0, 40, 85, 125, 170, 210, 255, 295))

    interval = derive_solve_interval(
        core_start_pts=85,
        core_end_pts_exclusive=255,
        source_frame_index=frame_index,
        overlap_seconds=Fraction(1, 20),
    )

    assert interval.start_pts == 40
    assert interval.end_pts_exclusive == 335
    assert interval.core_start_index == 1
    assert interval.core_end_index_exclusive == 5
    assert interval.frame_pts == (40, 85, 125, 170, 210, 255, 295)


def test_solve_interval_at_source_start_does_not_invent_negative_pts() -> None:
    frame_index = _index((0, 40, 80, 120))

    interval = derive_solve_interval(
        core_start_pts=0,
        core_end_pts_exclusive=80,
        source_frame_index=frame_index,
    )

    assert interval.start_pts == 0
    assert interval.end_pts_exclusive == 160
    assert interval.core_start_index == 0


def test_solve_interval_requires_core_boundaries_to_follow_decoded_pts() -> None:
    frame_index = _index((0, 40, 80, 120))

    with pytest.raises(SceneBridgeUnavailable, match="core start PTS"):
        derive_solve_interval(
            core_start_pts=41,
            core_end_pts_exclusive=120,
            source_frame_index=frame_index,
        )


@pytest.mark.parametrize(
    ("direction", "source_points", "target_points", "expected_pts"),
    (
        (
            "down",
            (0, 40, 80, 120, 160),
            (80, 120, 160, 200, 240),
            (80, 160),
        ),
        (
            "up",
            (160, 200, 240, 280, 320),
            (80, 120, 160, 200, 240),
            (160, 240),
        ),
    ),
)
def test_overlap_selects_two_exact_registered_source_pts(
    direction: str,
    source_points: tuple[int, ...],
    target_points: tuple[int, ...],
    expected_pts: tuple[int, int],
) -> None:
    source_map = _map("source", source_points)
    target_map = _map("target-solve", target_points)
    source_path = {
        index: _camera(float(pts)) for index, pts in enumerate(source_points)
    }

    anchors = select_overlap_anchors(
        direction=direction,
        source_core_map=source_map,
        source_camera_path=source_path,
        target_solve_map=target_map,
        target_registered_frames={0, 1, 2, 3, 4},
        min_separation_seconds=Fraction(3, 50),
    )

    assert tuple(item.source_pts for item in anchors) == expected_pts
    assert anchors[0].target_frame < anchors[1].target_frame
    assert anchors[0].camera == _camera(float(expected_pts[0]))


def test_overlap_rejects_one_registered_common_pts_without_extrapolation() -> None:
    source_map = _map("source", (0, 40, 80))
    target_map = _map("target-solve", (40, 80, 120))

    with pytest.raises(SceneBridgeUnavailable, match="two common source PTS"):
        select_overlap_anchors(
            direction="down",
            source_core_map=source_map,
            source_camera_path={0: _camera(0.0), 1: _camera(40.0), 2: _camera(80.0)},
            target_solve_map=target_map,
            target_registered_frames={0},
            min_separation_seconds=Fraction(1, 1000),
        )


def test_overlap_rejects_common_pts_below_minimum_time_separation() -> None:
    source_map = _map("source", (0, 40, 80))
    target_map = _map("target-solve", (0, 40, 80))

    with pytest.raises(SceneBridgeUnavailable, match="too close"):
        select_overlap_anchors(
            direction="down",
            source_core_map=source_map,
            source_camera_path={0: _camera(0.0), 1: _camera(40.0), 2: _camera(80.0)},
            target_solve_map=target_map,
            target_registered_frames={0, 1, 2},
            min_separation_seconds=Fraction(1, 10),
        )


def test_core_seed_uses_registered_endpoints_and_exact_pts_time() -> None:
    core_map = _map("target-core", (200, 240, 280, 320))
    solve_map = _map("target-solve", (120, 160, 200, 240, 280, 320, 360))
    solve_path = {index: _camera(float(index)) for index in range(7)}

    seed = build_core_seed(
        core_map=core_map,
        core_registered_frames={1, 3},
        solve_map=solve_map,
        solve_camera_path=solve_path,
        fps=25.0,
        direction="down",
        source_clip_id="source",
        target_clip_id="target-core",
    )

    assert [row["frame"] for row in seed["keyframes"]] == [1, 3]
    assert [row["source_pts"] for row in seed["keyframes"]] == [240, 320]
    assert [row["time"] for row in seed["keyframes"]] == [0.04, 0.12]
    assert [row["camera"]["x"] for row in seed["keyframes"]] == [3.0, 5.0]
    assert all(
        row["source"] == "scene_overlap_anchor" for row in seed["keyframes"]
    )
    assert seed["scene_bridge"]["direction"] == "down"


def test_core_seed_rejects_missing_exact_solve_pts() -> None:
    core_map = _map("target-core", (200, 240, 280))
    solve_map = _map("target-solve", (160, 200, 280))

    with pytest.raises(SceneBridgeUnavailable, match="core endpoint PTS"):
        build_core_seed(
            core_map=core_map,
            core_registered_frames={1, 2},
            solve_map=solve_map,
            solve_camera_path={0: _camera(0.0), 1: _camera(1.0), 2: _camera(2.0)},
            fps=25.0,
            direction="up",
            source_clip_id="source",
            target_clip_id="target-core",
        )
