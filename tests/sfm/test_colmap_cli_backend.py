from __future__ import annotations

import os
import subprocess
from pathlib import Path

from cadscene.sfm.colmap_cli import (
    ColmapCliPaths,
    build_colmap_cli_commands,
    export_colmap_text_model,
    is_cuda_failure,
    parse_gpu_execution,
    run_colmap_command,
)


FEATURE_HELP = """
--FeatureExtraction.use_gpu arg (=1)
--FeatureExtraction.gpu_index arg (=-1)
--FeatureExtraction.max_image_size arg (=3200)
--FeatureExtraction.max_num_features arg (=8192)
--ImageReader.mask_path arg
"""

MATCH_HELP = """
--FeatureMatching.use_gpu arg (=1)
--FeatureMatching.gpu_index arg (=-1)
--SequentialMatching.overlap arg (=10)
"""


def test_cli_commands_use_current_help_options_and_gpu_index(tmp_path: Path) -> None:
    paths = ColmapCliPaths(
        images_dir=tmp_path / "images",
        masks_dir=tmp_path / "masks",
        database_path=tmp_path / "database.db",
        sparse_dir=tmp_path / "sparse",
    )

    commands = build_colmap_cli_commands(
        "colmap.exe",
        paths,
        camera_model="OPENCV",
        max_image_size=2048,
        max_num_features=12000,
        sequential_overlap=15,
        init_min_tri_angle=2.0,
        ba_global_frames_ratio=2.0,
        ba_global_points_ratio=2.0,
        ba_global_frames_freq=1000,
        ba_global_points_freq=1000000,
        ba_global_max_num_iterations=25,
        ba_global_max_refinements=2,
        use_mask=False,
        use_gpu=True,
        gpu_index="0",
        feature_help=FEATURE_HELP,
        matching_help=MATCH_HELP,
    )

    feature, matching, mapper = commands
    assert isinstance(feature, list)
    assert feature[0:2] == ["colmap.exe", "feature_extractor"]
    assert feature[feature.index("--FeatureExtraction.use_gpu") + 1] == "1"
    assert feature[feature.index("--FeatureExtraction.gpu_index") + 1] == "0"
    assert matching[matching.index("--FeatureMatching.use_gpu") + 1] == "1"
    assert matching[matching.index("--FeatureMatching.gpu_index") + 1] == "0"
    assert mapper[1] == "mapper"
    assert mapper[mapper.index("--Mapper.ba_global_frames_ratio") + 1] == "2.0"
    assert mapper[mapper.index("--Mapper.ba_global_points_ratio") + 1] == "2.0"
    assert mapper[mapper.index("--Mapper.ba_global_frames_freq") + 1] == "1000"
    assert mapper[mapper.index("--Mapper.ba_global_points_freq") + 1] == "1000000"
    assert mapper[mapper.index("--Mapper.ba_global_max_num_iterations") + 1] == "25"
    assert mapper[mapper.index("--Mapper.ba_global_max_refinements") + 1] == "2"


def test_gpu_execution_requires_positive_log_evidence() -> None:
    assert parse_gpu_execution("Feature extraction completed", requested=True) is False
    assert parse_gpu_execution("Creating SIFT GPU feature extractor using CUDA device 0", requested=True) is True
    assert parse_gpu_execution(
        "Bind FeatureExtractorWorker to GPU device 0",
        requested=True,
    ) is True
    assert parse_gpu_execution(
        "Bind FeatureMatcherWorker to GPU device 0",
        requested=True,
    ) is True
    assert parse_gpu_execution("CUDA unavailable; falling back to CPU", requested=True) is False
    assert is_cuda_failure("SiftGPU was compiled without CUDA support") is True
    assert is_cuda_failure("mapper failed: no initial image pair") is False


def test_command_runner_uses_list_without_shell_true() -> None:
    calls = []

    class FakeProcess:
        returncode = 0
        stdout = iter(["CUDA device 0\n"])

        def wait(self):
            return self.returncode

    def fake_popen(command, **kwargs):
        calls.append((command, kwargs))
        return FakeProcess()

    result = run_colmap_command(["colmap.exe", "-h"], popen_factory=fake_popen)

    assert result.returncode == 0
    assert calls[0][0] == ["colmap.exe", "-h"]
    assert calls[0][1].get("shell", False) is False
    if os.name == "nt":
        assert calls[0][1]["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_text_model_exports_existing_trajectory_schema(tmp_path: Path) -> None:
    (tmp_path / "cameras.txt").write_text(
        "1 OPENCV 640 480 500 500 320 240 0 0 0 0\n",
        encoding="utf-8",
    )
    (tmp_path / "images.txt").write_text(
        "1 1 0 0 0 0 0 0 1 frame_000000.png\n"
        "10 10 7 20 20 -1\n",
        encoding="utf-8",
    )
    (tmp_path / "points3D.txt").write_text(
        "7 1 2 3 10 20 30 0.5 1 0\n",
        encoding="utf-8",
    )

    exported = export_colmap_text_model(
        tmp_path,
        frame_indices=[0, 5],
        fps=25.0,
        width=640,
        height=480,
        video_path="video.mp4",
    )

    assert exported.trajectory["schema_version"] == "cadscene_sfm_trajectory_v1"
    assert exported.trajectory["poses"][0]["registered"] is True
    assert exported.trajectory["poses"][0]["cam_from_world_quat_wxyz"] == [1.0, 0.0, 0.0, 0.0]
    assert exported.trajectory["poses"][1] == {"frame_index": 5, "registered": False}
    assert exported.points.tolist() == [[1.0, 2.0, 3.0]]
    assert exported.colors.tolist() == [[10, 20, 30]]
