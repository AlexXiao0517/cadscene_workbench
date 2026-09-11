from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.cli import plan_srt_adaptive_frames as planner
from cadscene.cli.plan_srt_adaptive_frames import build_parser


def test_planner_accepts_solve_image_output_and_dimensions() -> None:
    args = build_parser().parse_args(
        [
            "--video", "source.mp4",
            "--srt", "flight.srt",
            "--frame-map", "frame-map.json",
            "--config", "config.json",
            "--output", "plan.json",
            "--images-output", "02_sfm/images",
            "--reconstruct-width", "1920",
            "--reconstruct-height", "1080",
            "--progress-file", "adapter_progress.json",
            "--cache-root", "project-cache/adaptive-sfm",
        ]
    )

    assert args.images_output.name == "images"
    assert (args.reconstruct_width, args.reconstruct_height) == (1920, 1080)
    assert args.progress_file.name == "adapter_progress.json"
    assert args.cache_root == Path("project-cache/adaptive-sfm")


def test_matching_complete_plan_reuses_prepared_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identity = {"planner_version": "2", "output_size": [8, 6]}
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "source_frames": [0, 3],
                "preparation_identity": identity,
                "prepared_images_dir": str(tmp_path / "images"),
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        planner,
        "validate_prepared_images",
        lambda images, **kwargs: calls.append((images, kwargs)) or [{}, {}],
    )

    reused = planner.reuse_prepared_plan(
        plan_path,
        tmp_path / "images",
        identity,
        output_size=(8, 6),
    )

    assert reused is not None
    assert reused["source_frames"] == [0, 3]
    assert calls[0][1]["expected_identity"] == identity
