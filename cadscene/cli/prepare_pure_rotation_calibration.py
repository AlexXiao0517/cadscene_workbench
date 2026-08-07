from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import sys
from typing import Iterable

import cv2


def scale_camera_parameters(
    *,
    model: str,
    source_width: int,
    source_height: int,
    target_width: int,
    target_height: int,
    parameters: Iterable[float],
) -> tuple[float, ...]:
    values = tuple(float(value) for value in parameters)
    scale_x = target_width / source_width
    scale_y = target_height / source_height
    if not math.isclose(scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-9):
        raise ValueError("calibration scaling requires an unchanged aspect ratio")
    if model in {"SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"}:
        if len(values) < 3:
            raise ValueError(f"invalid {model} camera parameters")
        return (
            values[0] * scale_x,
            values[1] * scale_x,
            values[2] * scale_y,
            *values[3:],
        )
    if model in {"PINHOLE", "OPENCV"}:
        if len(values) < 4:
            raise ValueError(f"invalid {model} camera parameters")
        return (
            values[0] * scale_x,
            values[1] * scale_y,
            values[2] * scale_x,
            values[3] * scale_y,
            *values[4:],
        )
    raise ValueError(f"unsupported calibration model: {model}")


def _camera_parameters(camera: object) -> tuple[float, ...]:
    model = str(getattr(camera, "model"))
    core = (
        (float(camera.fx), float(camera.cx), float(camera.cy))
        if model in {"SIMPLE_PINHOLE", "SIMPLE_RADIAL", "RADIAL"}
        else (
            float(camera.fx),
            float(camera.fy),
            float(camera.cx),
            float(camera.cy),
        )
    )
    return (*core, *(float(value) for value in camera.distortion))


def _video_size(video: Path) -> tuple[int, int]:
    capture = cv2.VideoCapture(str(video))
    try:
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
    finally:
        capture.release()
    if width <= 0 or height <= 0:
        raise ValueError("unable to read pure-rotation video dimensions")
    return width, height


def prepare_calibration(
    *,
    backend_root: Path,
    source_root: Path,
    video: Path,
    output_dir: Path,
) -> Path:
    sys.path.insert(0, str(backend_root))
    try:
        from src.camera_model import read_colmap_cameras
    finally:
        sys.path.pop(0)
    target_width, target_height = _video_size(video)
    target_aspect = target_width / target_height
    candidates: list[tuple[float, Path, object]] = []
    paths = sorted(source_root.rglob("cameras.bin")) + sorted(
        source_root.rglob("cameras.txt")
    )
    for path in paths:
        try:
            cameras = read_colmap_cameras(path)
        except (OSError, ValueError):
            continue
        for camera in cameras.values():
            source_aspect = int(camera.width) / int(camera.height)
            if not math.isclose(
                source_aspect, target_aspect, rel_tol=1e-6, abs_tol=1e-9
            ):
                continue
            scale = target_width / int(camera.width)
            center_error = math.hypot(
                float(camera.cx) / int(camera.width) - 0.5,
                float(camera.cy) / int(camera.height) - 0.5,
            )
            candidates.append(
                (abs(math.log(scale)) + center_error, path, camera)
            )
    if not candidates:
        raise RuntimeError("no same-aspect COLMAP calibration candidate was found")
    _score, source_path, camera = min(candidates, key=lambda item: item[0])
    parameters = scale_camera_parameters(
        model=str(camera.model),
        source_width=int(camera.width),
        source_height=int(camera.height),
        target_width=target_width,
        target_height=target_height,
        parameters=_camera_parameters(camera),
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    line = " ".join(
        [
            "1",
            str(camera.model),
            str(target_width),
            str(target_height),
            *(format(value, ".17g") for value in parameters),
        ]
    )
    cameras_path = output_dir / "cameras.txt"
    cameras_path.write_text(line + "\n", encoding="utf-8")
    report = {
        "schema_version": "1.0",
        "intrinsics_verified": False,
        "source_camera_path": str(source_path),
        "source_resolution": [int(camera.width), int(camera.height)],
        "target_resolution": [target_width, target_height],
        "scale": target_width / int(camera.width),
        "model": str(camera.model),
    }
    temporary = output_dir / ".calibration_report.json.tmp"
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output_dir / "calibration_report.json")
    return cameras_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend-root", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    prepare_calibration(
        backend_root=args.backend_root.resolve(),
        source_root=args.source_root.resolve(),
        video=args.video.resolve(),
        output_dir=args.output_dir.resolve(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
