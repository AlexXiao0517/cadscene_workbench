from __future__ import annotations

import csv
import json
from pathlib import Path

from cadscene.cli.fuse_srt_sfm import main


def test_cli_writes_fusion_artifacts_from_explicit_inputs(tmp_path: Path, capsys) -> None:
    trajectory = tmp_path / "camera_trajectory.json"
    trajectory.write_text(
        json.dumps(
            {
                "fps": 1.0,
                "width": 10,
                "height": 10,
                "intrinsics": [{"params": [10.0], "width": 10}],
                "poses": [
                    {"frame_index": index, "registered": True, "center": center, "cam_from_world_quat_wxyz": [1, 0, 0, 0]}
                    for index, center in enumerate([[0, 0, 0], [2, 0, 0], [2, 2, 0], [0, 2, 0]])
                ],
            }
        ),
        encoding="utf-8",
    )
    timestamps = tmp_path / "frame_timestamps.csv"
    with timestamps.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["source_frame_index", "extracted_index", "image_name", "pts_time_sec", "timestamp_source", "cfr_confirmed"])
        writer.writeheader()
        for index in range(4):
            writer.writerow({"source_frame_index": index, "extracted_index": index, "image_name": f"frame_{index:06d}.png", "pts_time_sec": index, "timestamp_source": "pts_csv", "cfr_confirmed": "False"})
    srt = tmp_path / "sample.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:00,100\n[latitude: 30.0] [longitude: 120.0] [rel_alt: 10]\n\n"
        "2\n00:00:01,000 --> 00:00:01,100\n[latitude: 30.0] [longitude: 120.00002] [rel_alt: 10]\n\n"
        "3\n00:00:02,000 --> 00:00:02,100\n[latitude: 30.00002] [longitude: 120.00002] [rel_alt: 11]\n\n"
        "4\n00:00:03,000 --> 00:00:03,100\n[latitude: 30.00002] [longitude: 120.0] [rel_alt: 11]\n",
        encoding="utf-8",
    )

    code = main(
        [
            "--dataset", "demo", "--run-id", "r1", "--output-root", str(tmp_path / "runs"),
            "--trajectory", str(trajectory), "--frame-timestamps", str(timestamps), "--srt", str(srt),
            "--min-common-frames", "4", "--min-baseline-m", "1", "--min-smoothing-support", "1",
        ]
    )

    assert code == 0
    assert (tmp_path / "runs/demo/r1/02_fusion/camera_trajectory_fused.json").exists()
    assert "Sim3 RMSE" in capsys.readouterr().out
