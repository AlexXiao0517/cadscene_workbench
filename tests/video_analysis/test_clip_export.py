from __future__ import annotations

import errno
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import cadscene.video_analysis.clip_export as clip_export
from cadscene.video_analysis.clip_export import export_video_clips, load_export_clips
from cadscene.video_analysis.pts import probe_video_pts, resolve_ffmpeg_executable


def _write_manifest(tmp_path: Path, clips: object) -> Path:
    manifest = tmp_path / "clip_manifest.json"
    manifest.write_text(json.dumps({"clips": clips}), encoding="utf-8")
    return manifest


def _make_ffv1_video(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = resolve_ffmpeg_executable()
    video = tmp_path / "source.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=4",
            "-c:v",
            "ffv1",
            "-y",
            str(video),
        ],
        check=True,
    )
    return video, ffmpeg


def _make_nonzero_pts_ffv1_video(tmp_path: Path) -> tuple[Path, Path]:
    ffmpeg = resolve_ffmpeg_executable()
    video = tmp_path / "nonzero-pts-source.mkv"
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x48:rate=10:duration=4",
            "-vf",
            "setpts=PTS+10/TB",
            "-copyts",
            "-c:v",
            "ffv1",
            "-y",
            str(video),
        ],
        check=True,
    )
    index = probe_video_pts(video, ffmpeg_executable=ffmpeg)
    assert 9.9 < index.source_start_pts_sec < 10.1
    return video, ffmpeg


def _probe_duration(video_path: Path, ffmpeg: Path) -> float:
    process = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-i", str(video_path), "-f", "null", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert process.returncode == 0, process.stderr
    match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", process.stderr)
    assert match is not None, process.stderr
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _two_clip_manifest(tmp_path: Path) -> Path:
    return _write_manifest(
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
                "source_end_pts_sec": 4.0,
            },
        ],
    )


def test_x264_presets_are_public_and_ordered() -> None:
    assert clip_export.X264_PRESETS == (
        "ultrafast",
        "superfast",
        "veryfast",
        "faster",
        "fast",
        "medium",
        "slow",
        "slower",
        "veryslow",
        "placebo",
    )


def test_export_video_clips_reencodes_source_pts_ranges_atomically(
    tmp_path: Path,
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)
    output = tmp_path / "clips"

    paths = export_video_clips(video, manifest, output, ffmpeg_executable=ffmpeg)

    assert [path.name for path in paths] == ["clip-0001.mp4", "clip-0002.mp4"]
    assert all(abs(_probe_duration(path, ffmpeg) - 2.0) < 0.25 for path in paths)
    assert output.is_dir()
    assert not list(tmp_path.glob(".clips.tmp-*"))


def test_export_video_clips_seeks_absolute_nonzero_source_pts(tmp_path: Path) -> None:
    video, ffmpeg = _make_nonzero_pts_ffv1_video(tmp_path)
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts_sec": 10.0,
                "source_end_pts_sec": 12.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts_sec": 12.0,
                "source_end_pts_sec": 14.0,
            },
        ],
    )

    paths = export_video_clips(
        video, manifest, tmp_path / "clips", ffmpeg_executable=ffmpeg
    )

    assert all(abs(_probe_duration(path, ffmpeg) - 2.0) < 0.25 for path in paths)


def test_export_video_clips_refuses_existing_output_directory(tmp_path: Path) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)
    output = tmp_path / "clips"
    output.mkdir()

    with pytest.raises(FileExistsError):
        export_video_clips(video, manifest, output, ffmpeg_executable=ffmpeg)


def test_export_video_clips_cleans_up_failed_temp_publication(tmp_path: Path) -> None:
    video = tmp_path / "corrupt.mkv"
    video.write_bytes(b"not a video")
    manifest = _two_clip_manifest(tmp_path)
    output = tmp_path / "clips"

    with pytest.raises(RuntimeError, match="FFmpeg clip export failed"):
        export_video_clips(video, manifest, output)

    assert not output.exists()
    assert not list(tmp_path.glob(".clips.tmp-*"))


def test_export_video_clips_preserves_destination_created_during_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)
    output = tmp_path / "clips"
    original_publish = clip_export._publish_output_directory

    def create_destination_before_publish(
        temporary_dir: Path, destination: Path, *, strategy: str
    ) -> None:
        destination.mkdir()
        (destination / "sentinel.txt").write_text("keep", encoding="utf-8")
        original_publish(temporary_dir, destination, strategy=strategy)

    monkeypatch.setattr(clip_export, "_publish_output_directory", create_destination_before_publish)

    with pytest.raises(FileExistsError):
        export_video_clips(video, manifest, output, ffmpeg_executable=ffmpeg)

    assert (output / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not list(output.glob("clip-*.mp4"))
    assert not list(tmp_path.glob(".clips.tmp-*"))


def test_export_video_clips_reports_temporary_cleanup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "corrupt.mkv"
    video.write_bytes(b"not a video")
    manifest = _two_clip_manifest(tmp_path)

    def fail_remove(*_: object, ignore_errors: bool = False, **__: object) -> None:
        assert not ignore_errors
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr("cadscene.video_analysis.clip_export.shutil.rmtree", fail_remove)

    with pytest.raises(OSError, match="simulated cleanup failure"):
        export_video_clips(video, manifest, tmp_path / "clips")

    assert not (tmp_path / "clips").exists()
    assert list(tmp_path.glob(".clips.tmp-*"))


def test_export_video_clips_releases_reservation_when_temp_creation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)

    def fail_make_temp(*_: object, **__: object) -> str:
        raise OSError("simulated temp creation failure")

    monkeypatch.setattr(
        "cadscene.video_analysis.clip_export.tempfile.mkdtemp", fail_make_temp
    )

    with pytest.raises(OSError, match="simulated temp creation failure"):
        export_video_clips(video, manifest, tmp_path / "clips", ffmpeg_executable=ffmpeg)

    assert not list(tmp_path.glob(".clips.lock"))


def test_export_video_clips_rejects_unavailable_publication_before_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)

    def unavailable_strategy() -> str:
        raise OSError("atomic no-clobber publication is unavailable")

    def should_not_run(*_: object, **__: object) -> object:
        raise AssertionError("capability failure must occur before encoding work")

    monkeypatch.setattr(
        clip_export, "_publication_strategy", unavailable_strategy, raising=False
    )
    monkeypatch.setattr(clip_export, "resolve_ffmpeg_executable", should_not_run)
    monkeypatch.setattr(clip_export.tempfile, "mkdtemp", should_not_run)

    with pytest.raises(OSError, match="atomic no-clobber publication is unavailable"):
        export_video_clips(video, manifest, tmp_path / "clips", ffmpeg_executable=ffmpeg)

    assert not list(tmp_path.glob(".clips.lock"))
    assert not list(tmp_path.glob(".clips.tmp-*"))


def test_publication_strategy_rejects_linux_without_renameat2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(clip_export, "_linux_renameat2", lambda: None, raising=False)

    with pytest.raises(OSError, match="renameat2"):
        clip_export._publication_strategy("linux")


def test_publish_output_directory_dispatches_linux_no_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = tmp_path / "temporary"
    output = tmp_path / "clips"
    temporary.mkdir()
    calls: list[tuple[Path, Path]] = []

    def record_rename(source: Path, destination: Path) -> None:
        calls.append((source, destination))

    monkeypatch.setattr(
        clip_export, "_linux_rename_noreplace", record_rename, raising=False
    )

    clip_export._publish_output_directory(temporary, output, strategy="linux")

    assert calls == [(temporary, output)]


@pytest.mark.skipif(
    not sys.platform.startswith("linux") or clip_export._linux_renameat2() is None,
    reason="requires Linux renameat2",
)
def test_publish_output_directory_linux_renames_without_clobbering_destination(
    tmp_path: Path,
) -> None:
    temporary = tmp_path / "temporary"
    output = tmp_path / "clips"
    temporary.mkdir()
    (temporary / "clip.mp4").write_bytes(b"clip")

    clip_export._publish_output_directory(temporary, output, strategy="linux")

    assert not temporary.exists()
    assert (output / "clip.mp4").read_bytes() == b"clip"

    next_temporary = tmp_path / "next-temporary"
    next_temporary.mkdir()
    (next_temporary / "new-clip.mp4").write_bytes(b"new clip")
    sentinel = output / "sentinel.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(FileExistsError):
        clip_export._publish_output_directory(next_temporary, output, strategy="linux")

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (output / "new-clip.mp4").exists()
    assert next_temporary.is_dir()


def test_ffmpeg_clip_command_disables_overwrite_and_stdin() -> None:
    command = clip_export._build_ffmpeg_clip_command(
        ffmpeg=Path("ffmpeg"),
        source=Path("source.mp4"),
        clip=clip_export.ExportClip("clip-0001", 1.0, 3.0),
        clip_path=Path("clip-0001.mp4"),
        preset="fast",
        crf=18,
    )

    assert "-n" in command
    assert "-nostdin" in command
    assert command[-1] == "clip-0001.mp4"


@pytest.mark.parametrize("error_number", [errno.EEXIST, errno.ENOTEMPTY])
def test_publish_output_directory_maps_linux_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    temporary = tmp_path / "temporary"
    output = tmp_path / "clips"
    temporary.mkdir()

    def existing_destination(_: Path, __: Path) -> None:
        raise OSError(error_number, "destination exists")

    monkeypatch.setattr(
        clip_export, "_linux_rename_noreplace", existing_destination, raising=False
    )

    with pytest.raises(FileExistsError):
        clip_export._publish_output_directory(temporary, output, strategy="linux")


@pytest.mark.parametrize("preset,crf", [("ultrafast", -1), ("unknown", 18), ("fast", 52)])
def test_export_video_clips_rejects_invalid_x264_options(
    tmp_path: Path, preset: str, crf: int
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)

    with pytest.raises(ValueError):
        export_video_clips(
            video,
            manifest,
            tmp_path / "clips",
            ffmpeg_executable=ffmpeg,
            preset=preset,
            crf=crf,
        )


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


def test_load_export_clips_rejects_casefold_colliding_ids(tmp_path: Path) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "Clip",
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 1.0,
            },
            {
                "clip_id": "clip",
                "source_start_pts_sec": 1.0,
                "source_end_pts_sec": 2.0,
            },
        ],
    )

    with pytest.raises(ValueError, match="duplicated"):
        load_export_clips(manifest)


@pytest.mark.parametrize(
    "clip_id", ["CON", "prn.txt", "Aux.log", "nul.data", "COM1.mp4", "lpt9.csv"]
)
def test_load_export_clips_rejects_windows_reserved_device_basenames(
    tmp_path: Path, clip_id: str
) -> None:
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": clip_id,
                "source_start_pts_sec": 0.0,
                "source_end_pts_sec": 1.0,
            }
        ],
    )

    with pytest.raises(ValueError, match="unsafe"):
        load_export_clips(manifest)


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
