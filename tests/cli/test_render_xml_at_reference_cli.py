from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import numpy as np
import pytest

from cadscene.cli.render_xml_at_reference import (
    build_parser,
    build_render_report,
    render_calibrated_frame,
    resolve_ffmpeg,
    validate_render_interval,
)
from cadscene.core.camera import CameraState
from cadscene.srt.bentley_pose_merge import BentleyCameraModel


def _model() -> BentleyCameraModel:
    return BentleyCameraModel(
        image_size=(100, 50),
        focal_length_mm=10.0,
        sensor_size_mm=20.0,
        principal_point_px=(50.0, 25.0),
        distortion=(0.0, 0.0, 0.0, 0.0, 0.0),
        aspect_ratio=1.0,
        skew=0.0,
    )


def test_parser_uses_approved_reference_interval_defaults() -> None:
    args = build_parser().parse_args(
        [
            "--xml", "poses.xml",
            "--srt", "flight.srt",
            "--video", "flight.mp4",
            "--cad-dir", "cad",
            "--central-meridian", "118.83333333333333",
            "--cad-origin", "484717.5", "3189945.7",
            "--output", "out.mp4",
        ]
    )

    assert args.start_frame == 300
    assert args.end_frame == 11439
    assert args.sample_every == 5
    assert args.output_size == (1920, 1080)


def test_validate_render_interval_rejects_reversed_bounds() -> None:
    with pytest.raises(ValueError, match="end-frame.*start-frame"):
        validate_render_interval(start_frame=20, end_frame=10, sample_every=5)


def test_resolve_ffmpeg_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("cadscene.cli.render_xml_at_reference.shutil.which", lambda _: None)

    with pytest.raises(FileNotFoundError, match="FFmpeg"):
        resolve_ffmpeg("missing-ffmpeg")


def test_render_calibrated_frame_uses_xml_intrinsics_instead_of_camera_fov() -> None:
    frame = np.zeros((50, 100, 3), dtype=np.uint8)
    camera = CameraState(
        camera_x=0.0,
        camera_y=0.0,
        camera_z=1.0,
        yaw_deg=0.0,
        pitch_deg=0.0,
        roll_deg=0.0,
        fov_deg=10.0,
    )
    starts = np.asarray([[0.0, 10.0]], dtype=np.float64)
    ends = np.asarray([[1.0, 10.0]], dtype=np.float64)

    rendered = render_calibrated_frame(
        frame,
        camera,
        starts,
        ends,
        _model(),
        colors_bgr=np.asarray([[0, 255, 0]], dtype=np.uint8),
        max_distance_m=100.0,
        fade_start_m=50.0,
        overlay_alpha=1.0,
        linewidth=1,
    )

    assert rendered[30, 52, 1] > 0
    assert rendered[30, 52, 0] == 0
    assert rendered[30, 52, 2] == 0


def test_build_render_report_identifies_adjusted_xml_and_calibration_sources() -> None:
    report = build_render_report(
        xml_path=Path("poses.xml"),
        srt_path=Path("flight.srt"),
        video_path=Path("flight.mp4"),
        cad_dir=Path("cad"),
        output_path=Path("out.mp4"),
        source_hashes={"xml": "a", "srt": "b", "video": "c"},
        output_hash="d",
        start_frame=300,
        end_frame=11439,
        sample_every=5,
        source_fps=59.94005994,
        rendered_frame_count=2228,
        vertical_reference_m=144.5,
        camera_model=_model(),
    )

    assert report["schema_version"] == "1.0"
    assert report["position_source"] == "xml_adjusted_center"
    assert report["orientation_source"] == "xml_pose_rotation"
    assert report["distortion_source"] == "xml_photogroup"
    assert report["source_interval"] == {"start_frame": 300, "end_frame": 11439}
    assert report["render"]["output_size"] == [100, 50]
    assert report["render"]["duration_sec"] == pytest.approx(
        2228 / (59.94005994 / 5)
    )
