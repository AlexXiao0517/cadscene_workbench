from __future__ import annotations

import importlib
import sys
from pathlib import Path

import numpy as np
import pytest

from cadscene.sfm.colmap_cli import ColmapCommandResult, ColmapTextModelExport
from cadscene.sfm.reconstruction import (
    ReconstructionConfig,
    ReconstructionResult,
    build_camera_trajectory,
    build_sfm_report,
    build_sfm_stats,
    configure_incremental_mapping_options,
    frame_indices_for_config,
    mask_path_for_frame,
    write_reconstruction_outputs,
)


def test_frame_indices_respect_start_count_and_step() -> None:
    config = ReconstructionConfig(start_frame=3, num_frames=8, frame_step=3)

    assert frame_indices_for_config(config, frame_count=20) == [3, 6, 9]


def test_mask_path_matches_colmap_image_name(tmp_path: Path) -> None:
    assert mask_path_for_frame(tmp_path, 12).name == "frame_000012.png"


def test_camera_trajectory_schema_uses_original_frame_indices() -> None:
    poses = [
        {
            "frame_index": 5,
            "registered": True,
            "center": [1.0, 2.0, 3.0],
            "cam_from_world_quat_wxyz": [1.0, 0.0, 0.0, 0.0],
            "num_observations": 12,
        }
    ]

    trajectory = build_camera_trajectory(
        fps=25.0,
        width=1920,
        height=1080,
        intrinsics=[{"model": "OPENCV", "width": 1920, "height": 1080, "params": [1000.0]}],
        poses=poses,
    )

    assert trajectory["poses"][0]["frame_index"] == 5
    assert trajectory["poses"][0]["registered"] is True
    assert trajectory["poses"][0]["cam_from_world_quat_wxyz"] == [1.0, 0.0, 0.0, 0.0]


def test_stats_and_chinese_report_contain_required_fields(tmp_path: Path) -> None:
    config = ReconstructionConfig(frame_step=5, start_frame=0, num_frames=21, use_mask=False)
    stats = build_sfm_stats(
        frame_count=100,
        extracted_frame_count=5,
        registered_count=4,
        point_count=30,
        mean_reprojection_error=0.7,
        config=config,
        backend="mock",
    )
    report = build_sfm_report(
        video_path=tmp_path / "demo.mp4",
        output_dir=tmp_path / "02_sfm",
        stats=stats,
        outputs={"trajectory": "camera_trajectory.json", "points": "sparse_points.ply"},
    )

    assert stats["registered_ratio"] == pytest.approx(0.8)
    assert stats["used_mask"] is False
    assert stats["backend"] == "mock"
    assert stats["suitable_for_3d"] is False
    assert stats["low_parallax_or_rotation_suspected"] is True
    assert "不适合三维重建" in report
    assert "明显平移" in report
    assert "输入视频" in report
    assert "DINOv3 segmentation 尚未迁移" in report


def test_bounded_ba_defaults_are_recorded_in_stats() -> None:
    config = ReconstructionConfig()
    stats = build_sfm_stats(
        frame_count=10,
        extracted_frame_count=5,
        registered_count=5,
        point_count=100,
        mean_reprojection_error=0.5,
        config=config,
        backend="colmap_cli",
    )

    assert stats["ba_global_frames_ratio"] == 2.0
    assert stats["ba_global_points_ratio"] == 2.0
    assert stats["ba_global_frames_freq"] == 1000
    assert stats["ba_global_points_freq"] == 1000000
    assert stats["ba_global_max_num_iterations"] == 25
    assert stats["ba_global_max_refinements"] == 2


def test_stats_mark_well_supported_reconstruction_as_suitable() -> None:
    stats = build_sfm_stats(
        frame_count=100,
        extracted_frame_count=20,
        registered_count=18,
        point_count=5000,
        mean_reprojection_error=0.5,
        config=ReconstructionConfig(),
        backend="mock",
    )

    assert stats["suitable_for_3d"] is True
    assert stats["three_d_reconstruction_status"] == "suitable"
    assert stats["suitability_reasons"] == []


def test_forward_flight_mapper_options_match_legacy_tuning() -> None:
    class FakeOptions:
        def __init__(self) -> None:
            self.values = None

        def mergedict(self, values) -> None:
            self.values = values

    options = FakeOptions()

    configure_incremental_mapping_options(options, ReconstructionConfig())

    assert options.values == {
        "min_num_matches": 15,
        "multiple_models": True,
        "min_model_size": 5,
        "mapper": {
            "init_min_tri_angle": 2.0,
            "init_max_forward_motion": 1.0,
            "init_min_num_inliers": 50,
            "filter_min_tri_angle": 1.0,
        },
        "triangulation": {"min_angle": 1.0},
    }


def test_low_registration_without_sparse_point_shortage_recommends_mapper_retry() -> None:
    stats = build_sfm_stats(
        frame_count=1179,
        extracted_frame_count=236,
        registered_count=2,
        point_count=12045,
        mean_reprojection_error=0.4,
        config=ReconstructionConfig(),
        backend="pycolmap",
    )
    report = build_sfm_report(
        video_path="road-flight.mp4",
        output_dir="02_sfm",
        stats=stats,
        outputs={},
    )

    assert stats["low_parallax_or_rotation_suspected"] is False
    assert stats["next_step_recommendation"] == "retry_sfm_with_forward_flight_settings"
    assert "SfM 注册失败" in report
    assert "不能据此判断视频缺少平移" in report


def test_module_import_does_not_import_pycolmap(monkeypatch: pytest.MonkeyPatch) -> None:
    sys.modules.pop("cadscene.sfm.reconstruction", None)
    sys.modules.pop("pycolmap", None)

    importlib.import_module("cadscene.sfm.reconstruction")

    assert "pycolmap" not in sys.modules


def test_missing_pycolmap_error_is_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    reconstruction = importlib.import_module("cadscene.sfm.reconstruction")
    real_import = reconstruction.importlib.import_module

    def fake_import(name: str):
        if name == "pycolmap":
            raise ModuleNotFoundError(name)
        return real_import(name)

    monkeypatch.setattr(reconstruction.importlib, "import_module", fake_import)
    with pytest.raises(RuntimeError, match="pycolmap not installed"):
        reconstruction.load_pycolmap()


def test_extract_frames_selects_requested_original_frames(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "tiny.mp4"
    writer = cv2.VideoWriter(str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (32, 24))
    for value in range(6):
        writer.write(np.full((24, 32, 3), value * 20, dtype=np.uint8))
    writer.release()

    from cadscene.sfm.reconstruction import extract_frames

    extracted = extract_frames(video, tmp_path / "images", [1, 3, 5])

    assert [item.frame_index for item in extracted] == [1, 3, 5]
    assert [item.path.name for item in extracted] == [
        "frame_000001.png",
        "frame_000003.png",
        "frame_000005.png",
    ]


def test_extract_frames_resizes_before_writing_without_changing_frame_indices(
    tmp_path: Path,
) -> None:
    cv2 = pytest.importorskip("cv2")
    video = tmp_path / "resized.mp4"
    writer = cv2.VideoWriter(
        str(video), cv2.VideoWriter_fourcc(*"mp4v"), 5.0, (64, 48)
    )
    for value in range(4):
        writer.write(np.full((48, 64, 3), value * 20, dtype=np.uint8))
    writer.release()

    from cadscene.sfm.reconstruction import extract_frames

    extracted = extract_frames(
        video, tmp_path / "resized-images", [1, 3], output_size=(32, 24)
    )

    assert [item.frame_index for item in extracted] == [1, 3]
    assert cv2.imread(str(extracted[0].path)).shape[:2] == (24, 32)


def test_stats_record_source_and_actual_reconstruction_dimensions() -> None:
    config = ReconstructionConfig(
        reconstruction_height=1080,
        camera_model="PINHOLE",
        camera_params=(1321.32664365, 1321.32664365, 960.0, 540.0),
    )
    stats = build_sfm_stats(
        frame_count=100,
        extracted_frame_count=20,
        registered_count=18,
        point_count=5000,
        mean_reprojection_error=0.5,
        config=config,
        backend="colmap_cli",
        runtime={
            "source_image_size": [3840, 2160],
            "reconstruction_image_size": [1920, 1080],
        },
    )

    assert stats["source_image_size"] == [3840, 2160]
    assert stats["reconstruction_image_size"] == [1920, 1080]
    assert stats["camera_params"] == [
        1321.32664365,
        1321.32664365,
        960.0,
        540.0,
    ]


def test_reconstruction_outputs_persist_source_frame_pts_table(tmp_path: Path) -> None:
    result = ReconstructionResult(
        trajectory={"poses": []},
        intrinsics=[],
        points=np.empty((0, 3), dtype=np.float64),
        colors=None,
        stats={},
        frame_timestamps=[
            {
                "source_frame_index": 42,
                "extracted_index": 0,
                "image_name": "frame_000042.png",
                "pts_time_sec": 1.401,
                "timestamp_source": "opencv_pos_msec",
                "cfr_confirmed": False,
            }
        ],
    )

    outputs = write_reconstruction_outputs(tmp_path, result, video_path=None)

    assert outputs["frame_timestamps"].read_text(encoding="utf-8-sig").splitlines() == [
        "source_frame_index,extracted_index,image_name,pts_time_sec,timestamp_source,cfr_confirmed",
        "42,0,frame_000042.png,1.401,opencv_pos_msec,False",
    ]


def test_load_best_sparse_model_selects_most_registered(tmp_path: Path) -> None:
    from cadscene.sfm.reconstruction import load_best_sparse_model

    sparse = tmp_path / "sparse"
    (sparse / "0").mkdir(parents=True)
    (sparse / "1").mkdir()

    class FakeReconstruction:
        def __init__(self, path: str) -> None:
            self.path = Path(path)

        def num_reg_images(self) -> int:
            return 3 if self.path.name == "0" else 8

    class FakePycolmap:
        Reconstruction = FakeReconstruction

    selected = load_best_sparse_model(FakePycolmap, sparse)

    assert selected.path.name == "1"


def test_colmap_cli_cpu_retry_preserves_all_ba_kwargs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    reconstruction = importlib.import_module("cadscene.sfm.reconstruction")
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    calls = []
    ba_kwargs = {
        "ba_global_frames_ratio": 3.25,
        "ba_global_points_ratio": 4.5,
        "ba_global_frames_freq": 321,
        "ba_global_points_freq": 654321,
        "ba_global_max_num_iterations": 17,
        "ba_global_max_refinements": 3,
    }

    monkeypatch.setattr(
        reconstruction,
        "detect_sfm_environment",
        lambda *args, **kwargs: {
            "colmap_cli_available": True,
            "colmap_path": "colmap.exe",
            "colmap_cuda_confirmed": True,
            "cuda_device_available": True,
            "gpu_names": ["Test GPU"],
            "colmap_version": "test",
            "pycolmap_version": None,
        },
    )
    monkeypatch.setattr(reconstruction, "_video_metadata", lambda path: (3, 25.0, 64, 48))
    extracted_sizes = []

    def fake_extract_frames(
        video_path, images_dir, frame_indices, *, output_size=None
    ):
        extracted_sizes.append(output_size)
        return [
            reconstruction.ExtractedFrame(frame_index=index, path=images_dir / f"frame_{index:06d}.png")
            for index in frame_indices
        ]

    monkeypatch.setattr(reconstruction, "extract_frames", fake_extract_frames)

    def fake_pipeline(executable, paths, **kwargs):
        calls.append(kwargs)
        if kwargs["use_gpu"]:
            raise reconstruction.ColmapCommandError(
                ColmapCommandResult(
                    command=["colmap.exe", "feature_extractor"],
                    returncode=1,
                    output="SiftGPU not supported",
                    elapsed_sec=0.0,
                )
            )
        return {
            "timings": {},
            "feature_extraction_gpu": False,
            "feature_matching_gpu": False,
        }

    monkeypatch.setattr(reconstruction, "run_colmap_cli_pipeline", fake_pipeline)
    monkeypatch.setattr(
        reconstruction,
        "load_pycolmap",
        lambda: (_ for _ in ()).throw(RuntimeError("pycolmap not installed")),
    )
    monkeypatch.setattr(reconstruction, "first_sparse_model_dir", lambda sparse_dir: sparse_dir)
    monkeypatch.setattr(
        reconstruction,
        "convert_colmap_model_to_text",
        lambda executable, model_dir, output_dir: output_dir,
    )
    monkeypatch.setattr(
        reconstruction,
        "export_colmap_text_model",
        lambda *args, **kwargs: ColmapTextModelExport(
            trajectory={
                "poses": [
                    {"frame_index": index, "registered": True}
                    for index in range(3)
                ]
            },
            intrinsics=[],
            points=np.empty((0, 3), dtype=np.float64),
            colors=None,
            mean_reprojection_error=None,
        ),
    )

    reconstruction.run_reconstruction(
        video_path=video,
        output_dir=tmp_path / "output",
        config=ReconstructionConfig(
            frame_step=1,
            min_reg_images=1,
            backend="colmap_cli",
            device="cuda",
            reconstruction_height=720,
            **ba_kwargs,
        ),
    )

    assert extracted_sizes == [(64, 48)]
    assert [call["use_gpu"] for call in calls] == [True, False]
    assert [{name: call[name] for name in ba_kwargs} for call in calls] == [ba_kwargs, ba_kwargs]
