from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.video_analysis.clip_export import load_export_clips


def _write_manifest(tmp_path: Path, clips: object) -> Path:
    manifest = tmp_path / "clip_manifest.json"
    manifest.write_text(json.dumps({"clips": clips}), encoding="utf-8")
    return manifest


def test_load_export_clips_returns_validated_source_pts_ranges(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 2.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts_sec": 2.0,
                "source_end_pts_sec": 4.5,
            },
        ],
    )

    clips = load_export_clips(manifest)

    assert [(item.clip_id, item.duration_sec) for item in clips] == [
        ("clip-0001", 2.0),
        ("clip-0002", 2.5),
    ]


@pytest.mark.parametrize(
    "clips",
    [
        [],
        [
            {
                "clip_id": "../escape",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 2.0,
            }
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": float("nan"),
                "source_end_pts_sec": 2.0,
            }
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": float("inf"),
                "source_end_pts_sec": 2.0,
            }
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": -float("inf"),
                "source_end_pts_sec": 2.0,
            }
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 2.0,
                "source_end_pts_sec": 2.0,
            }
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 2.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts_sec": 1.5,
                "source_end_pts_sec": 3.0,
            },
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 2.0,
            },
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 2.0,
                "source_end_pts_sec": 3.0,
            },
        ],
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 60.0,
            }
        ],
    ],
    ids=[
        "empty",
        "unsafe-id",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "non-positive",
        "overlap",
        "duplicate-id",
        "sixty-seconds",
    ],
)
def test_load_export_clips_rejects_invalid_manifest_ranges(
    tmp_path: Path, clips: object
) -> None:
    manifest = _write_manifest(tmp_path, clips)

    with pytest.raises(ValueError):
        load_export_clips(manifest)
