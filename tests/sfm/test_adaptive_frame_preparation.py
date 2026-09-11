from __future__ import annotations

import json
from pathlib import Path
import shutil

import numpy as np
import pytest

from cadscene.sfm.adaptive_frame_preparation import (
    hydrate_prepared_cache,
    preparation_identity,
    prepare_adaptive_candidates,
    publish_prepared_cache,
    publish_selected_candidates,
    validate_prepared_images,
)


def test_content_addressed_cache_hydrates_a_new_attempt(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    candidate_dir = tmp_path / "candidates"
    candidate_dir.mkdir()
    paths = []
    for frame, value in ((0, 20), (3, 80)):
        path = candidate_dir / f"frame_{frame:06d}.png"
        assert cv2.imwrite(
            str(path), np.full((6, 8, 3), value, dtype=np.uint8)
        )
        paths.append(path)
    from cadscene.sfm.adaptive_frame_preparation import PreparedCandidates

    prepared = PreparedCandidates(
        frames=(0, 3),
        sharpness=np.asarray([1.0, 2.0]),
        paths=tuple(paths),
        pts_time_sec=(0.0, 0.12),
        output_size=(8, 6),
    )
    identity = {"planner_version": "2", "output_size": [8, 6]}
    first_images = tmp_path / "attempt-1" / "images"
    manifest = publish_selected_candidates(
        prepared,
        selected_frames=[0, 3],
        images_dir=first_images,
        identity=identity,
    )
    first_plan = tmp_path / "attempt-1" / "plan.json"
    first_plan.write_text(
        json.dumps(
            {
                "schema_version": "adaptive_sfm_frame_plan_v1",
                "source_frames": [0, 3],
                "prepared_images_dir": str(first_images),
                "prepared_images_manifest": str(manifest),
                "preparation_identity": identity,
            }
        ),
        encoding="utf-8",
    )

    cache_entry = publish_prepared_cache(
        first_plan,
        first_images,
        tmp_path / "project-cache",
        identity=identity,
        output_size=(8, 6),
    )
    second_plan = tmp_path / "attempt-2" / "plan.json"
    second_images = tmp_path / "attempt-2" / "images"
    reused = hydrate_prepared_cache(
        tmp_path / "project-cache",
        second_plan,
        second_images,
        identity=identity,
        output_size=(8, 6),
    )

    assert cache_entry.is_dir()
    assert reused is not None
    assert reused["source_frames"] == [0, 3]
    assert Path(reused["prepared_images_dir"]) == second_images
    validate_prepared_images(
        second_images,
        source_frames=[0, 3],
        expected_identity=identity,
        expected_size=(8, 6),
    )
    assert hydrate_prepared_cache(
        tmp_path / "project-cache",
        tmp_path / "attempt-3" / "plan.json",
        tmp_path / "attempt-3" / "images",
        identity={**identity, "output_size": [10, 6]},
        output_size=(10, 6),
    ) is None


class _SequentialCapture:
    def __init__(self, frames: list[np.ndarray]) -> None:
        self.frames = frames
        self.read_count = 0
        self.released = False
        self.set_count = 0

    def isOpened(self) -> bool:
        return True

    def set(self, *_args: object) -> None:
        self.set_count += 1
        raise AssertionError("adaptive preparation must not randomly seek")

    def read(self) -> tuple[bool, np.ndarray | None]:
        if self.read_count >= len(self.frames):
            return False, None
        frame = self.frames[self.read_count]
        self.read_count += 1
        return True, frame.copy()

    def get(self, _property: int) -> float:
        return float(self.read_count * 100)

    def release(self) -> None:
        self.released = True


def test_candidate_scoring_and_images_use_one_monotonic_decode(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    frames = [
        np.full((12, 16, 3), value * 20, dtype=np.uint8)
        for value in range(6)
    ]
    capture = _SequentialCapture(frames)

    prepared = prepare_adaptive_candidates(
        tmp_path / "source.mp4",
        [0, 2, 5],
        tmp_path / "candidates",
        output_size=(8, 6),
        capture_factory=lambda _path: capture,
        cv2_module=cv2,
    )

    assert capture.set_count == 0
    assert capture.read_count == 6
    assert capture.released is True
    assert prepared.frames == (0, 2, 5)
    assert prepared.sharpness.tolist() == [1.0, 1.0, 1.0]
    assert [cv2.imread(str(path)).shape[:2] for path in prepared.paths] == [
        (6, 8),
        (6, 8),
        (6, 8),
    ]


def test_published_manifest_removes_unselected_candidates_and_fails_closed(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "source.mp4"
    srt = tmp_path / "flight.srt"
    frame_map = tmp_path / "frame-map.json"
    video.write_bytes(b"video")
    srt.write_text("srt", encoding="utf-8")
    frame_map.write_text('{"frames": []}', encoding="utf-8")
    capture = _SequentialCapture(
        [np.full((6, 8, 3), value * 20, dtype=np.uint8) for value in range(4)]
    )
    prepared = prepare_adaptive_candidates(
        video,
        [0, 2, 3],
        tmp_path / "candidates",
        output_size=(8, 6),
        capture_factory=lambda _path: capture,
        cv2_module=cv2,
    )
    identity = preparation_identity(
        video=video,
        srt=srt,
        frame_map=frame_map,
        output_size=(8, 6),
        planner_settings={"candidate_spacing_sec": 0.5},
    )

    manifest_path = publish_selected_candidates(
        prepared,
        selected_frames=[0, 3],
        images_dir=tmp_path / "images",
        identity=identity,
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["source_frames"] == [0, 3]
    assert sorted(path.name for path in (tmp_path / "images").glob("*.png")) == [
        "frame_000000.png",
        "frame_000003.png",
    ]
    assert not (tmp_path / "candidates" / "frame_000002.png").exists()
    validated = validate_prepared_images(
        tmp_path / "images",
        source_frames=[0, 3],
        expected_identity=identity,
        expected_size=(8, 6),
    )
    assert [row["source_frame_index"] for row in validated] == [0, 3]

    escaped = tmp_path / "frame_000000.png"
    shutil.copy2(tmp_path / "images" / "frame_000000.png", escaped)
    payload["images"][0]["image_name"] = "../frame_000000.png"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="image name"):
        validate_prepared_images(
            tmp_path / "images",
            source_frames=[0, 3],
            expected_identity=identity,
            expected_size=(8, 6),
        )

    payload["images"][0]["image_name"] = "frame_000000.png"
    payload["identity"]["output_size"] = [10, 6]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        validate_prepared_images(
            tmp_path / "images",
            source_frames=[0, 3],
            expected_identity=identity,
            expected_size=(8, 6),
        )
