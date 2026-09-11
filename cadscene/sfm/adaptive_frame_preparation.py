from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence
from uuid import uuid4

import numpy as np


PREPARATION_SCHEMA = "adaptive_sfm_prepared_images_v1"
PLANNER_VERSION = "2"
MANIFEST_NAME = "prepared_images_manifest.json"


def frame_image_name(frame_index: int) -> str:
    return f"frame_{int(frame_index):06d}.png"


@dataclass(frozen=True)
class PreparedCandidates:
    frames: tuple[int, ...]
    sharpness: np.ndarray
    paths: tuple[Path, ...]
    pts_time_sec: tuple[float | None, ...]
    output_size: tuple[int, int]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preparation_identity(
    *,
    video: str | Path,
    srt: str | Path,
    frame_map: str | Path,
    output_size: tuple[int, int],
    planner_settings: Mapping[str, object],
) -> dict[str, object]:
    paths = {
        "video_sha256": _sha256(Path(video)),
        "srt_sha256": _sha256(Path(srt)),
        "frame_map_sha256": _sha256(Path(frame_map)),
    }
    return {
        "planner_version": PLANNER_VERSION,
        **paths,
        "output_size": [int(output_size[0]), int(output_size[1])],
        "planner_settings": json.loads(
            json.dumps(dict(planner_settings), sort_keys=True, ensure_ascii=False)
        ),
    }


def prepare_adaptive_candidates(
    video_path: str | Path,
    candidate_frames: Sequence[int],
    output_dir: str | Path,
    *,
    output_size: tuple[int, int],
    progress: Callable[[int, int], None] | None = None,
    capture_factory: Callable[[str], object] | None = None,
    cv2_module: object | None = None,
) -> PreparedCandidates:
    cv2 = cv2_module or importlib.import_module("cv2")
    targets = tuple(sorted({int(frame) for frame in candidate_frames}))
    if not targets or targets[0] < 0:
        raise ValueError("candidate_frames must contain non-negative frame indices")
    width, height = (int(output_size[0]), int(output_size[1]))
    if width <= 0 or height <= 0:
        raise ValueError("output_size must contain positive dimensions")

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob("frame_*.png"):
        stale.unlink()

    factory = capture_factory or cv2.VideoCapture
    capture = factory(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    target_set = set(targets)
    paths: dict[int, Path] = {}
    scores: dict[int, float] = {}
    timestamps: dict[int, float | None] = {}
    frame_index = 0
    try:
        while frame_index <= targets[-1]:
            ok, image = capture.read()
            if not ok or image is None:
                break
            if frame_index in target_set:
                output_image = image
                if image.shape[1] != width or image.shape[0] != height:
                    output_image = cv2.resize(
                        image,
                        (width, height),
                        interpolation=cv2.INTER_AREA,
                    )
                gray = cv2.cvtColor(output_image, cv2.COLOR_BGR2GRAY)
                sharpness_scale = min(1.0, 480.0 / max(1.0, float(gray.shape[1])))
                if sharpness_scale < 1.0:
                    gray = cv2.resize(
                        gray,
                        None,
                        fx=sharpness_scale,
                        fy=sharpness_scale,
                        interpolation=cv2.INTER_AREA,
                    )
                scores[frame_index] = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                path = output / frame_image_name(frame_index)
                if output_image.size == 0 or not cv2.imwrite(str(path), output_image):
                    raise RuntimeError(f"failed to write prepared frame: {path}")
                paths[frame_index] = path
                pts_msec = float(capture.get(cv2.CAP_PROP_POS_MSEC))
                timestamps[frame_index] = (
                    pts_msec / 1000.0
                    if np.isfinite(pts_msec) and pts_msec >= 0.0
                    else None
                )
                if progress is not None:
                    progress(len(paths), len(targets))
            frame_index += 1
    finally:
        capture.release()

    missing = [frame for frame in targets if frame not in paths]
    if missing:
        raise RuntimeError(
            f"video ended before adaptive candidates were decoded: {missing[:8]}"
        )
    sharpness = np.asarray([scores[frame] for frame in targets], dtype=np.float64)
    if not np.any(sharpness > 0.0):
        sharpness[:] = 1.0
    return PreparedCandidates(
        frames=targets,
        sharpness=sharpness,
        paths=tuple(paths[frame] for frame in targets),
        pts_time_sec=tuple(timestamps[frame] for frame in targets),
        output_size=(width, height),
    )


def publish_selected_candidates(
    prepared: PreparedCandidates,
    *,
    selected_frames: Sequence[int],
    images_dir: str | Path,
    identity: Mapping[str, object],
) -> Path:
    selected = tuple(sorted({int(frame) for frame in selected_frames}))
    available = {frame: index for index, frame in enumerate(prepared.frames)}
    missing = [frame for frame in selected if frame not in available]
    if not selected or missing:
        raise ValueError(f"selected frames are unavailable: {missing}")
    output = Path(images_dir)
    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob("frame_*.png"):
        stale.unlink()

    image_rows: list[dict[str, object]] = []
    selected_set = set(selected)
    for frame, source_path in zip(prepared.frames, prepared.paths):
        if frame not in selected_set:
            source_path.unlink(missing_ok=True)
            continue
        destination = output / frame_image_name(frame)
        os.replace(source_path, destination)
        candidate_index = available[frame]
        image_rows.append(
            {
                "source_frame_index": frame,
                "image_name": destination.name,
                "sha256": _sha256(destination),
                "width": prepared.output_size[0],
                "height": prepared.output_size[1],
                "pts_time_sec": prepared.pts_time_sec[candidate_index],
            }
        )
    try:
        Path(prepared.paths[0]).parent.rmdir()
    except OSError:
        pass

    payload = {
        "schema_version": PREPARATION_SCHEMA,
        "complete": True,
        "identity": dict(identity),
        "source_frames": list(selected),
        "images": image_rows,
    }
    manifest = output / MANIFEST_NAME
    temporary = manifest.with_name(f".{manifest.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, manifest)
    return manifest


def validate_prepared_images(
    images_dir: str | Path,
    *,
    source_frames: Sequence[int],
    expected_identity: Mapping[str, object] | None = None,
    expected_size: tuple[int, int] | None = None,
) -> list[dict[str, object]]:
    cv2 = importlib.import_module("cv2")
    output = Path(images_dir)
    manifest = output / MANIFEST_NAME
    if not manifest.exists():
        raise ValueError(f"prepared image manifest is missing: {manifest}")
    payload = json.loads(manifest.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != PREPARATION_SCHEMA or payload.get("complete") is not True:
        raise ValueError("prepared image manifest is incomplete")
    if expected_identity is not None and payload.get("identity") != dict(expected_identity):
        raise ValueError("prepared image identity does not match the current inputs")
    expected_frames = [int(frame) for frame in source_frames]
    if payload.get("source_frames") != expected_frames:
        raise ValueError("prepared image source frames do not match the frame plan")
    rows = payload.get("images")
    if not isinstance(rows, list) or len(rows) != len(expected_frames):
        raise ValueError("prepared image manifest does not contain every selected frame")
    for expected_frame, row in zip(expected_frames, rows):
        if not isinstance(row, dict) or int(row.get("source_frame_index", -1)) != expected_frame:
            raise ValueError("prepared image rows are out of order")
        path = output / str(row.get("image_name", ""))
        if path.name != frame_image_name(expected_frame) or not path.exists():
            raise ValueError(f"prepared image is missing for frame {expected_frame}")
        if _sha256(path) != row.get("sha256"):
            raise ValueError(f"prepared image hash mismatch for frame {expected_frame}")
        if expected_size is not None:
            image = cv2.imread(str(path))
            if image is None or (image.shape[1], image.shape[0]) != expected_size:
                raise ValueError(f"prepared image dimensions mismatch for frame {expected_frame}")
    return rows


def _cache_key(identity: Mapping[str, object]) -> str:
    canonical = json.dumps(
        dict(identity), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_cache_plan(
    cache_entry: Path,
    *,
    identity: Mapping[str, object],
    output_size: tuple[int, int],
) -> dict[str, object] | None:
    plan_path = cache_entry / "adaptive_frame_plan.json"
    try:
        payload = json.loads(plan_path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            return None
        source_frames = payload.get("source_frames")
        if (
            payload.get("preparation_identity") != dict(identity)
            or not isinstance(source_frames, list)
        ):
            return None
        validate_prepared_images(
            cache_entry / "images",
            source_frames=[int(frame) for frame in source_frames],
            expected_identity=identity,
            expected_size=output_size,
        )
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def publish_prepared_cache(
    plan_path: str | Path,
    images_dir: str | Path,
    cache_root: str | Path,
    *,
    identity: Mapping[str, object],
    output_size: tuple[int, int],
) -> Path:
    """Publish one verified plan as an immutable project-level cache entry."""

    source_plan = Path(plan_path)
    source_images = Path(images_dir)
    payload = json.loads(source_plan.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("adaptive frame plan must be an object")
    source_frames = payload.get("source_frames")
    if (
        payload.get("preparation_identity") != dict(identity)
        or not isinstance(source_frames, list)
    ):
        raise ValueError("adaptive frame plan does not match the cache identity")
    validate_prepared_images(
        source_images,
        source_frames=[int(frame) for frame in source_frames],
        expected_identity=identity,
        expected_size=output_size,
    )

    root = Path(cache_root)
    root.mkdir(parents=True, exist_ok=True)
    entry = root / _cache_key(identity)
    if _load_cache_plan(entry, identity=identity, output_size=output_size) is not None:
        return entry

    temporary = root / f".{entry.name}.{uuid4().hex}.tmp"
    temporary_images = temporary / "images"
    temporary_images.mkdir(parents=True)
    try:
        for source in source_images.glob("frame_*.png"):
            shutil.copy2(source, temporary_images / source.name)
        shutil.copy2(source_images / MANIFEST_NAME, temporary_images / MANIFEST_NAME)
        cached_payload = {
            **payload,
            "prepared_images_dir": str(temporary_images),
            "prepared_images_manifest": str(temporary_images / MANIFEST_NAME),
        }
        _write_json_atomic(temporary / "adaptive_frame_plan.json", cached_payload)
        if _load_cache_plan(
            temporary, identity=identity, output_size=output_size
        ) is None:
            raise ValueError("new adaptive frame cache entry failed validation")
        if entry.exists():
            quarantine = root / f".{entry.name}.invalid.{uuid4().hex}"
            os.replace(entry, quarantine)
        os.replace(temporary, entry)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return entry


def hydrate_prepared_cache(
    cache_root: str | Path,
    plan_path: str | Path,
    images_dir: str | Path,
    *,
    identity: Mapping[str, object],
    output_size: tuple[int, int],
) -> dict[str, object] | None:
    """Copy a verified cache entry into a new attempt and rewrite local paths."""

    entry = Path(cache_root) / _cache_key(identity)
    cached = _load_cache_plan(entry, identity=identity, output_size=output_size)
    if cached is None:
        return None
    source_frames = [int(frame) for frame in cached["source_frames"]]
    output = Path(images_dir)
    output.mkdir(parents=True, exist_ok=True)
    for stale in output.glob("frame_*.png"):
        stale.unlink()
    (output / MANIFEST_NAME).unlink(missing_ok=True)
    for source in (entry / "images").glob("frame_*.png"):
        shutil.copy2(source, output / source.name)
    shutil.copy2(entry / "images" / MANIFEST_NAME, output / MANIFEST_NAME)
    validate_prepared_images(
        output,
        source_frames=source_frames,
        expected_identity=identity,
        expected_size=output_size,
    )
    hydrated = {
        **cached,
        "prepared_images_dir": str(output),
        "prepared_images_manifest": str(output / MANIFEST_NAME),
    }
    _write_json_atomic(Path(plan_path), hydrated)
    return hydrated
