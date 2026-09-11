from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from cadscene.sfm.adaptive_frame_preparation import (
    preparation_identity,
    prepare_adaptive_candidates,
    publish_selected_candidates,
    validate_prepared_images,
)


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

    payload["identity"]["output_size"] = [10, 6]
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        validate_prepared_images(
            tmp_path / "images",
            source_frames=[0, 3],
            expected_identity=identity,
            expected_size=(8, 6),
        )
