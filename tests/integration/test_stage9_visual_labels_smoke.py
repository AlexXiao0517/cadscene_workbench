from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
import shutil
import subprocess

import cv2
import numpy as np
import pytest

from cadscene.annotations.render_overlay import build_annotation_events
from cadscene.annotations.tracking import (
    OpenCvLkVideoAnchorTracker,
    TrackingFrame,
    VideoTrackingInitialization,
)
from cadscene.cli.package_project_render import main as package_project_render_main
from cadscene.cli.render_annotations import main as render_annotations_main
from cadscene.projects.media import ProjectMediaSpec, probe_media
from cadscene.video_analysis.pts import resolve_ffmpeg_executable


def _source_frame(index: int) -> np.ndarray:
    image = np.zeros((120, 200, 3), dtype=np.uint8)
    x = 22 + index * 8
    for row in range(3):
        for column in range(4):
            cv2.circle(
                image,
                (x + 5 + column * 8, 45 + 5 + row * 8),
                2,
                (255, 255, 255),
                -1,
            )
    cv2.rectangle(image, (x, 45), (x + 36, 73), (180, 180, 180), 1)
    return image


def _encode_source(
    path: Path, frames: list[np.ndarray], ffmpeg: str, *, frame_rate: int = 25
) -> None:
    raw_path = path.with_suffix(".bgr")
    raw_path.write_bytes(b"".join(frame.tobytes() for frame in frames))
    subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "rawvideo",
            "-pixel_format",
            "bgr24",
            "-video_size",
            "200x120",
            "-framerate",
            str(frame_rate),
            "-i",
            str(raw_path),
            "-vf",
            "setsar=1",
            "-frames:v",
            str(len(frames)),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ),
        check=True,
    )


def _decoded_frames(path: Path) -> list[np.ndarray]:
    capture = cv2.VideoCapture(str(path))
    frames = []
    while True:
        success, frame = capture.read()
        if not success:
            break
        frames.append(frame)
    capture.release()
    return frames


def _video_annotation(results: list[dict[str, object]]) -> dict[str, object]:
    return {
        "annotation_id": "video-target",
        "clip_id": "clip-1",
        "anchor_type": "video_track",
        "text": "TRACK",
        "style": {
            "font_size_px": 20,
            "text_color": "#FFFFFFFF",
            "background_color": "#000000FF",
            "border_color": "#FFFFFFFF",
            "font_family": "sans-serif",
            "font_weight": 600,
        },
        "source_pts_range": {
            "start_pts": 1000,
            "end_pts_exclusive": 1240,
            "time_base": {"numerator": 1, "denominator": 1000},
            "semantics": "half_open",
        },
        "screen_offset": [0, -25],
        "visibility_policy": {"min_tracking_confidence": 0.5},
        "user_visible": True,
        "active_tracking_revision": "tracking-2",
    }


def _cad_annotation() -> dict[str, object]:
    return {
        "annotation_id": "cad-anchor",
        "clip_id": "clip-1",
        "anchor_type": "cad_anchor",
        "text": "K12+340",
        "anchor": {"cad_world_xyz": [0, 10, 1]},
        "style": {
            "font_size_px": 20,
            "text_color": "#FFFF00FF",
            "background_color": "#000000FF",
            "border_color": "#FFFF00FF",
            "font_family": "sans-serif",
            "font_weight": 600,
        },
        "source_pts_range": {
            "start_pts": 1000,
            "end_pts_exclusive": 1240,
            "time_base": {"numerator": 1, "denominator": 1000},
            "semantics": "half_open",
        },
        "screen_offset": [-35, -30],
        "visibility_policy": {},
        "user_visible": True,
        "active_tracking_revision": None,
    }


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is required for the real Stage 9 smoke",
)
def test_real_cad_and_video_labels_preserve_frame_partition(tmp_path: Path) -> None:
    try:
        ffmpeg = str(resolve_ffmpeg_executable())
    except (FileNotFoundError, RuntimeError):
        pytest.skip("an H.264-capable FFmpeg is unavailable")

    source_frames = [_source_frame(index) for index in range(6)]
    source_pts = [1000, 1040, 1080, 1120, 1160, 1200]
    tracker = OpenCvLkVideoAnchorTracker(min_features=4)
    first_revision = tracker.track(
        tuple(
            TrackingFrame(source_pts=pts, image_bgr=frame)
            for pts, frame in zip(source_pts[:3], source_frames[:3])
        ),
        VideoTrackingInitialization(source_pts=1000, bbox=(22, 45, 36, 28)),
    )
    correction_revision = tracker.track(
        tuple(
            TrackingFrame(source_pts=pts, image_bgr=frame)
            for pts, frame in zip(source_pts[4:], source_frames[4:])
        ),
        VideoTrackingInitialization(source_pts=1160, bbox=(54, 45, 36, 28)),
    )
    tracking_results = [item.to_dict() for item in first_revision]
    tracking_results.append(
        {
            "source_pts": 1120,
            "bbox": None,
            "anchor_xy": None,
            "confidence": 0.0,
            "visibility": False,
            "tracking_status": "lost",
            "diagnostic": "target left view before correction",
        }
    )
    tracking_results.extend(item.to_dict() for item in correction_revision)

    frames = [
        {
            "source_decoded_frame_ordinal": index,
            "source_pts": pts,
            "duration_pts": 40,
        }
        for index, pts in enumerate(source_pts)
    ]
    bundle = {
        "schema_version": 1,
        "project_id": "project-1",
        "clip_id": "clip-1",
        "video_width": 200,
        "video_height": 120,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "source_frames": frames,
        "annotations": [_cad_annotation(), _video_annotation(tracking_results)],
        "tracking_revisions": {"tracking-2": {"results": tracking_results}},
    }
    camera_rows = tuple(
        {
            "frame_index": index,
            "camera_x": 0,
            "camera_y": 0,
            "camera_z": 1,
            "yaw": 0 if index < 3 else 180,
            "pitch": 0,
            "roll": 0,
            "fov": 90,
        }
        for index in range(6)
    )
    events = build_annotation_events(bundle, camera_rows=camera_rows)
    assert {
        item.source_pts for item in events if item.annotation_id == "cad-anchor"
    } == {
        1000,
        1040,
        1080,
    }
    assert {
        item.source_pts for item in events if item.annotation_id == "video-target"
    } == {
        1000,
        1040,
        1080,
        1160,
        1200,
    }

    source = tmp_path / "source.mp4"
    _encode_source(source, source_frames, ffmpeg)
    bundle_path = tmp_path / "annotation_render_bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    camera_path = tmp_path / "camera_path.csv"
    camera_path.write_text(
        "\ufeffframe_index,camera_x,camera_y,camera_z,yaw,pitch,roll,fov\n"
        + "\n".join(
            f"{row['frame_index']},0,0,1,{row['yaw']},0,0,90" for row in camera_rows
        )
        + "\n",
        encoding="utf-8",
    )
    annotated = tmp_path / "annotated.mp4"
    assert (
        render_annotations_main(
            [
                "--input",
                str(source),
                "--output",
                str(annotated),
                "--bundle",
                str(bundle_path),
                "--camera-path",
                str(camera_path),
                "--ffmpeg",
                ffmpeg,
            ]
        )
        == 0
    )
    annotated_frames = _decoded_frames(annotated)
    decoded_source_frames = _decoded_frames(source)
    assert len(annotated_frames) == len(decoded_source_frames) == 6
    changed_pixels = [
        int(np.sum(cv2.absdiff(before, after) > 30))
        for before, after in zip(decoded_source_frames, annotated_frames)
    ]
    assert changed_pixels[0] > 1000
    assert changed_pixels[3] < 100
    assert changed_pixels[4] > 500

    source_map = tmp_path / "source_frame_map.json"
    source_map.write_text(
        json.dumps(
            {
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "clips": [
                    {
                        "clip_id": "clip-1",
                        "frames": [
                            {"ordinal": index, "pts": pts}
                            for index, pts in enumerate(source_pts)
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    media_spec_path = tmp_path / "media_spec.json"
    media_spec_path.write_text(
        json.dumps(
            ProjectMediaSpec(
                width=200,
                height=120,
                display_orientation_baked=True,
                sample_aspect_ratio=Fraction(1, 1),
                pixel_format="yuv420p",
                codec_name="h264",
                profile="High",
                time_base=Fraction(1, 1000),
                color_range="tv",
                color_space="bt709",
                color_transfer="bt709",
                color_primaries="bt709",
                nominal_frame_rate=Fraction(25, 1),
            ).to_dict()
        ),
        encoding="utf-8",
    )
    packaged = tmp_path / "packaged"
    packaged.mkdir()
    assert (
        package_project_render_main(
            [
                "--input",
                str(annotated),
                "--source-frame-map",
                str(source_map),
                "--media-spec",
                str(media_spec_path),
                "--output-dir",
                str(packaged),
                "--ffmpeg",
                ffmpeg,
            ]
        )
        == 0
    )
    render_map = json.loads((packaged / "render_frame_map.json").read_text())
    assert probe_media(packaged / "rendered.mp4").video.frame_count == 6
    assert [item["source_pts"] for item in render_map["frames"]] == source_pts
    assert [item["output_frame_ordinal"] for item in render_map["frames"]] == list(
        range(6)
    )


@pytest.mark.parametrize("frame_rate", (30, 60))
def test_rgba_overlay_preserves_final_frame_at_common_frame_rates(
    tmp_path: Path, frame_rate: int
) -> None:
    try:
        ffmpeg = str(resolve_ffmpeg_executable())
    except (FileNotFoundError, RuntimeError):
        pytest.skip("an H.264-capable FFmpeg is unavailable")
    frames = [_source_frame(index) for index in range(6)]
    source = tmp_path / f"source-{frame_rate}.mp4"
    _encode_source(source, frames, ffmpeg, frame_rate=frame_rate)
    tracking_results = [
        {
            "source_pts": index,
            "anchor_xy": [40 + index * 4, 60],
            "bbox": [25 + index * 4, 50, 30, 20],
            "confidence": 0.9,
            "visibility": True,
            "tracking_status": "tracked",
        }
        for index in range(6)
    ]
    annotation = _video_annotation(tracking_results)
    annotation["source_pts_range"] = {
        "start_pts": 0,
        "end_pts_exclusive": 6,
        "time_base": {"numerator": 1, "denominator": frame_rate},
        "semantics": "half_open",
    }
    bundle = {
        "video_width": 200,
        "video_height": 120,
        "source_time_base": {"numerator": 1, "denominator": frame_rate},
        "source_frames": [
            {"source_pts": index, "duration_pts": 1} for index in range(6)
        ],
        "annotations": [annotation],
        "tracking_revisions": {"tracking-2": {"results": tracking_results}},
    }
    bundle_path = tmp_path / f"bundle-{frame_rate}.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    output = tmp_path / f"annotated-{frame_rate}.mp4"

    assert (
        render_annotations_main(
            [
                "--input",
                str(source),
                "--output",
                str(output),
                "--bundle",
                str(bundle_path),
                "--ffmpeg",
                ffmpeg,
            ]
        )
        == 0
    )
    assert len(_decoded_frames(output)) == len(frames)


def test_rgba_overlay_preserves_irregular_authoritative_frame_durations(
    tmp_path: Path,
) -> None:
    try:
        ffmpeg = str(resolve_ffmpeg_executable())
    except (FileNotFoundError, RuntimeError):
        pytest.skip("an H.264-capable FFmpeg is unavailable")
    frames = [_source_frame(index) for index in range(6)]
    durations = [8, 9, 8, 10, 7, 9]
    source_pts = [1000]
    for duration in durations[:-1]:
        source_pts.append(source_pts[-1] + duration)
    png_paths = []
    for index, frame in enumerate(frames):
        path = tmp_path / f"vfr-{index}.png"
        assert cv2.imwrite(str(path), frame)
        png_paths.append(path)
    concat_lines = ["ffconcat version 1.0"]
    for path, duration in zip(png_paths, durations):
        concat_lines.extend(
            (
                f"file '{path.as_posix()}'",
                "option framerate 1000",
                f"duration {duration / 1000:.12f}",
            )
        )
    concat_lines.extend((f"file '{png_paths[-1].as_posix()}'", "option framerate 1000"))
    concat_path = tmp_path / "source.ffconcat"
    concat_path.write_text("\n".join(concat_lines) + "\n", encoding="utf-8")
    source = tmp_path / "source-vfr.mp4"
    subprocess.run(
        (
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-frames:v",
            str(len(frames)),
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ),
        check=True,
    )
    tracking_results = [
        {
            "source_pts": pts,
            "anchor_xy": [40 + index * 4, 60],
            "bbox": [25 + index * 4, 50, 30, 20],
            "confidence": 0.9,
            "visibility": True,
            "tracking_status": "tracked",
        }
        for index, pts in enumerate(source_pts)
    ]
    annotation = _video_annotation(tracking_results)
    annotation["source_pts_range"] = {
        "start_pts": source_pts[0],
        "end_pts_exclusive": source_pts[-1] + durations[-1],
        "time_base": {"numerator": 1, "denominator": 1000},
        "semantics": "half_open",
    }
    bundle = {
        "video_width": 200,
        "video_height": 120,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "source_frames": [
            {"source_pts": pts, "duration_pts": duration}
            for pts, duration in zip(source_pts, durations)
        ],
        "annotations": [annotation],
        "tracking_revisions": {"tracking-2": {"results": tracking_results}},
    }
    bundle_path = tmp_path / "bundle-vfr.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    output = tmp_path / "annotated-vfr.mp4"

    assert (
        render_annotations_main(
            [
                "--input",
                str(source),
                "--output",
                str(output),
                "--bundle",
                str(bundle_path),
                "--ffmpeg",
                ffmpeg,
            ]
        )
        == 0
    )
    assert len(_decoded_frames(output)) == len(frames)
