from __future__ import annotations

from fractions import Fraction
import json
from pathlib import Path

import pytest

from cadscene.cli.build_srt_full_pose import main
from cadscene.srt.full_pose import (
    FullPoseBuildConfig,
    build_full_pose_trajectory,
)
from cadscene.srt.georeference import CadGeoreference
from cadscene.srt.parser import load_srt_records


def _georeference(*, confirmed: bool = True) -> CadGeoreference:
    return CadGeoreference.from_dict(
        {
            "schema_version": 1,
            "horizontal_datum": "CGCS2000",
            "projection_family": "gauss_kruger",
            "zone_width_deg": 3,
            "central_meridian_deg": 120.0,
            "epsg": 4549,
            "projected_axis_order": "easting_northing",
            "cad_axis_mapping": "cad_x_easting_cad_y_northing",
            "zone_prefix": False,
            "linear_unit": "metre",
            "source": "user_confirmed",
            "confirmed": confirmed,
            "confidence": 0.99,
            "validation": {"trajectory_inside_cad_ratio": 1.0},
        }
    )


def _write_srt(
    path: Path,
    *,
    complete: bool = True,
    longitude_step: float = 0.00001,
) -> Path:
    blocks: list[str] = []
    for index, (longitude, yaw) in enumerate(
        (
            (120.0, 0.0),
            (120.0 + longitude_step, 45.0),
            (120.0 + longitude_step * 2.0, 90.0),
        ),
        start=1,
    ):
        attitude = (
            f"[gb_yaw: {yaw}] [gb_pitch: -45.0] [gb_roll: 1.0]"
            if complete or index == 1
            else ""
        )
        blocks.append(
            f"{index}\n00:00:0{index - 1},000 --> 00:00:0{index - 1},100\n"
            f"[latitude: 30.0] [longitude: {longitude}] "
            f"[rel_alt: {9.0 + index}] {attitude}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def _frame_map(*, end: int = 2001) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "clips": [
            {
                "clip_id": "clip-1",
                "source_start_pts": 0,
                "source_end_pts_exclusive": end,
                "frames": [
                    {"ordinal": 10, "pts": 0},
                    {"ordinal": 11, "pts": 1000},
                    {"ordinal": 12, "pts": 2000},
                ],
            }
        ],
    }


def _config(*, georeference: CadGeoreference | None = None) -> FullPoseBuildConfig:
    return FullPoseBuildConfig(
        clip_id="clip-1",
        source_start_pts=0,
        source_end_pts_exclusive=2001,
        source_time_base=Fraction(1, 1000),
        georeference=georeference or _georeference(),
        cad_origin_xy=(500_000.0, 3_320_113.3978450196),
        cad_scale=1.0,
        horizontal_fov_deg=82.0,
        cad_z_offset_m=100.0,
        attitude_profile="dji_absolute_ned",
        max_interpolation_gap_sec=1.5,
        minimum_registered_coverage=0.8,
    )


def test_builder_emits_local_cad_metric_centers_and_user_fov(
    tmp_path: Path,
) -> None:
    records = load_srt_records(_write_srt(tmp_path / "flight.srt"))

    result = build_full_pose_trajectory(
        records,
        _frame_map(),
        {"width": 3840, "height": 2160, "fps": 1.0},
        _config(),
        tmp_path / "output",
    )

    payload = json.loads(result.trajectory_path.read_text(encoding="utf-8"))
    assert payload["meta"]["trajectory_mode"] == "srt_full_pose"
    assert payload["meta"]["coordinate_system"] == "cad_local_m"
    assert payload["meta"]["metric_scale_locked"] is True
    assert payload["meta"]["fov_source"] == "user"
    assert payload["intrinsics"][0]["horizontal_fov_deg"] == 82.0
    assert [pose["frame_index"] for pose in payload["poses"]] == [0, 1, 2]
    assert [pose["source_pts"] for pose in payload["poses"]] == [0, 1000, 2000]
    assert payload["poses"][0]["center"] == pytest.approx([0.0, 0.0, 110.0], abs=0.01)
    assert payload["poses"][2]["center"][0] > payload["poses"][0]["center"][0]
    assert all(pose["registered"] for pose in payload["poses"])
    assert result.camera_path_path.is_file()
    assert result.diagnostics_path.is_file()
    assert result.report_path.is_file()
    diagnostics = json.loads(
        result.diagnostics_path.read_text(encoding="utf-8")
    )
    assert diagnostics["horizontal_validation"] == {
        "status": "user_confirmed",
        "confidence": 0.99,
        "warnings": [],
    }
    assert diagnostics["vertical_validation"] == {
        "status": "relative_height_with_user_offset",
        "cad_z_offset_m": 100.0,
        "height_source_counts": {"rel_alt": 3},
        "warnings": [],
    }


def test_builder_rejects_frame_map_interval_disagreement(tmp_path: Path) -> None:
    records = load_srt_records(_write_srt(tmp_path / "flight.srt"))

    with pytest.raises(ValueError, match="interval"):
        build_full_pose_trajectory(
            records,
            _frame_map(end=3000),
            {"width": 3840, "height": 2160, "fps": 1.0},
            _config(),
            tmp_path / "output",
        )


def test_builder_rejects_insufficient_complete_pose_coverage(
    tmp_path: Path,
) -> None:
    records = load_srt_records(
        _write_srt(tmp_path / "incomplete.srt", complete=False)
    )

    with pytest.raises(ValueError, match="coverage"):
        build_full_pose_trajectory(
            records,
            _frame_map(),
            {"width": 3840, "height": 2160, "fps": 1.0},
            _config(),
            tmp_path / "output",
        )


def test_builder_rejects_implausible_horizontal_speed(tmp_path: Path) -> None:
    records = load_srt_records(
        _write_srt(tmp_path / "jump.srt", longitude_step=0.01)
    )
    raw_config = _config().to_dict()
    raw_config["max_horizontal_speed_mps"] = 50.0

    with pytest.raises(ValueError, match="horizontal speed"):
        build_full_pose_trajectory(
            records,
            _frame_map(),
            {"width": 3840, "height": 2160, "fps": 1.0},
            FullPoseBuildConfig.from_dict(raw_config),
            tmp_path / "output",
        )


def test_cli_publishes_complete_artifact_directory(tmp_path: Path) -> None:
    srt = _write_srt(tmp_path / "flight.srt")
    frame_map = tmp_path / "frame-map.json"
    frame_map.write_text(json.dumps(_frame_map()), encoding="utf-8")
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"test fixture: metadata comes from the immutable config")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "video_metadata": {"width": 3840, "height": 2160, "fps": 1.0},
                "build": _config().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"

    exit_code = main(
        [
            "--dataset",
            "p1",
            "--run-id",
            "clip-1",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--srt",
            str(srt),
            "--frame-map",
            str(frame_map),
            "--config",
            str(config),
        ]
    )

    output = output_root / "p1" / "clip-1" / "02_srt_full_pose"
    assert exit_code == 0
    assert (output / "camera_trajectory_full_pose.json").is_file()
    assert (output / "camera_path_full_pose.csv").is_file()
    assert (output / "georeference_diagnostics.json").is_file()
    assert (output / "full_pose_report.md").is_file()


def test_cli_failure_does_not_publish_partial_output(tmp_path: Path) -> None:
    srt = _write_srt(tmp_path / "flight.srt", complete=False)
    frame_map = tmp_path / "frame-map.json"
    frame_map.write_text(json.dumps(_frame_map()), encoding="utf-8")
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"fixture")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "video_metadata": {"width": 3840, "height": 2160, "fps": 1.0},
                "build": _config().to_dict(),
            }
        ),
        encoding="utf-8",
    )
    output_root = tmp_path / "runs"

    exit_code = main(
        [
            "--dataset",
            "p1",
            "--run-id",
            "clip-1",
            "--output-root",
            str(output_root),
            "--video",
            str(video),
            "--srt",
            str(srt),
            "--frame-map",
            str(frame_map),
            "--config",
            str(config),
        ]
    )

    output = output_root / "p1" / "clip-1" / "02_srt_full_pose"
    assert exit_code == 1
    assert not output.exists()
