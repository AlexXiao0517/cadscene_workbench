from __future__ import annotations

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
        ]
    )

    assert args.images_output.name == "images"
    assert (args.reconstruct_width, args.reconstruct_height) == (1920, 1080)
