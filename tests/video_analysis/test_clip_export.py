from __future__ import annotations

import errno
from fractions import Fraction
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import cadscene.video_analysis.clip_export as clip_export
from cadscene.video_analysis.clip_export import export_video_clips, load_export_clips
from cadscene.video_analysis.pts import (
    DecodedFrameIndex,
    DecodedFrameTimestamp,
    probe_video_pts,
    resolve_ffmpeg_executable,
)


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
            "setpts=PTS+100/TB",
            "-copyts",
            "-c:v",
            "ffv1",
            "-y",
            str(video),
        ],
        check=True,
    )
    index = probe_video_pts(video, ffmpeg_executable=ffmpeg)
    assert 99.9 < index.source_start_pts_sec < 100.1
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
                "source_start_pts": 0,
                "source_end_pts_exclusive": 2000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 0.0,
                "source_end_pts_exclusive_sec": 2.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts": 2000,
                "source_end_pts_exclusive": 4000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 2.0,
                "source_end_pts_exclusive_sec": 4.0,
            },
        ],
    )


def _integer_pts_clip(clip_id: str, start: object, end: object) -> dict[str, object]:
    return {
        "clip_id": clip_id,
        "source_start_pts": start,
        "source_end_pts_exclusive": end,
        "source_time_base": {"numerator": 1, "denominator": 1000},
    }


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
    sidecar = json.loads((output / "clip_frame_map.json").read_text(encoding="utf-8"))
    assert [len(item["frames"]) for item in sidecar["clips"]] == [20, 20]
    assert output.is_dir()
    assert not list(tmp_path.glob(".clips.tmp-*"))


def test_export_video_clips_reports_monotonic_real_frame_progress(
    tmp_path: Path,
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _two_clip_manifest(tmp_path)
    updates: list[tuple[str, str, float]] = []

    export_video_clips(
        video,
        manifest,
        tmp_path / "clips-progress",
        ffmpeg_executable=ffmpeg,
        progress_callback=lambda stage, message, fraction: updates.append(
            (stage, message, fraction)
        ),
    )

    fractions = [fraction for _stage, _message, fraction in updates]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0.0
    assert fractions[-1] == 1.0
    assert any(0.1 < fraction < 0.9 for fraction in fractions)
    assert any(stage == "encoding_clip" for stage, _message, _fraction in updates)


def test_ffmpeg_progress_callback_failure_terminates_and_waits_for_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeProcess:
        def __init__(self) -> None:
            self.stdout = iter(["frame=1\n"])
            self.terminated = False
            self.waited = False

        def terminate(self) -> None:
            self.terminated = True

        def wait(self, timeout=None) -> int:
            self.waited = True
            return 1

    process = FakeProcess()
    monkeypatch.setattr(clip_export.subprocess, "Popen", lambda *args, **kwargs: process)

    with pytest.raises(RuntimeError, match="progress publication failed"):
        clip_export._run_ffmpeg_with_progress(
            ["ffmpeg", "input.mp4", "output.mp4"],
            clip_id="clip-0001",
            expected_frames=1,
            completed_frames=0,
            total_frames=1,
            callback=lambda *_args: (_ for _ in ()).throw(
                RuntimeError("progress publication failed")
            ),
        )

    assert process.terminated is True
    assert process.waited is True


def test_export_video_clips_can_publish_one_requested_subinterval(
    tmp_path: Path,
) -> None:
    video, ffmpeg = _make_ffv1_video(tmp_path)
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "clip-0002",
                "source_start_pts": 2000,
                "source_end_pts_exclusive": 4000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
            }
        ],
    )
    output = tmp_path / "selected"

    paths = export_video_clips(
        video,
        manifest,
        output,
        ffmpeg_executable=ffmpeg,
        require_full_source_partition=False,
    )

    assert [path.name for path in paths] == ["clip-0002.mp4"]
    sidecar = json.loads((output / "clip_frame_map.json").read_text(encoding="utf-8"))
    assert sidecar["full_source_partition"] is False
    assert [item["pts"] for item in sidecar["clips"][0]["frames"]] == list(
        range(2000, 4000, 100)
    )


def test_export_video_clips_seeks_absolute_nonzero_source_pts(tmp_path: Path) -> None:
    video, ffmpeg = _make_nonzero_pts_ffv1_video(tmp_path)
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts": 100000,
                "source_end_pts_exclusive": 102000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 100.0,
                "source_end_pts_exclusive_sec": 102.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts": 102000,
                "source_end_pts_exclusive": 104000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 102.0,
                "source_end_pts_exclusive_sec": 104.0,
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

    with pytest.raises(RuntimeError, match="decoded-frame probe failed"):
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

    monkeypatch.setattr(
        clip_export, "_publish_output_directory", create_destination_before_publish
    )

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

    monkeypatch.setattr(
        "cadscene.video_analysis.clip_export.shutil.rmtree", fail_remove
    )
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, 100, "pts")
            for ordinal, pts in enumerate(range(0, 4000, 100))
        ),
    )
    monkeypatch.setattr(
        clip_export, "probe_decoded_frame_index", lambda *_args, **_kwargs: frame_index
    )

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
        export_video_clips(
            video, manifest, tmp_path / "clips", ffmpeg_executable=ffmpeg
        )

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
        export_video_clips(
            video, manifest, tmp_path / "clips", ffmpeg_executable=ffmpeg
        )

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
        clip=clip_export.ExportClip("clip-0001", 1000, 3000, Fraction(1, 1000)),
        clip_path=Path("clip-0001.mp4"),
        preset="fast",
        crf=18,
    )

    assert "-n" in command
    assert "-nostdin" in command
    assert command[-1] == "clip-0001.mp4"


def test_export_command_seeks_before_input_and_keeps_absolute_pts_trim() -> None:
    command = clip_export._build_ffmpeg_clip_command(
        ffmpeg=Path("ffmpeg"),
        source=Path("source.mp4"),
        clip=clip_export.ExportClip("clip-0001", 5000, 9000, Fraction(1, 1000)),
        clip_path=Path("clip-0001.mp4"),
        preset="fast",
        crf=18,
    )

    assert "trim=start_pts=5000:end_pts=9000,setpts=PTS-STARTPTS" in command
    assert "-fps_mode" in command and "passthrough" in command
    assert command.index("-ss") < command.index("-copyts") < command.index("-i")
    assert command[command.index("-ss") + 1] == "0"
    assert "-t" not in command


def test_export_command_prerolls_ten_seconds_for_late_clip() -> None:
    command = clip_export._build_ffmpeg_clip_command(
        ffmpeg=Path("ffmpeg"),
        source=Path("source.mp4"),
        clip=clip_export.ExportClip(
            "clip-0003", 115440, 173160, Fraction(1, 1000)
        ),
        clip_path=Path("clip-0003.mp4"),
        preset="veryfast",
        crf=18,
    )

    assert command[command.index("-ss") + 1] == "105.44"
    assert "trim=start_pts=115440:end_pts=173160,setpts=PTS-STARTPTS" in command


def test_export_command_makes_seek_relative_to_nonzero_source_start() -> None:
    command = clip_export._build_ffmpeg_clip_command(
        ffmpeg=Path("ffmpeg"),
        source=Path("source.mp4"),
        clip=clip_export.ExportClip(
            "clip-0001", 115000, 117000, Fraction(1, 1000)
        ),
        clip_path=Path("clip-0001.mp4"),
        preset="veryfast",
        crf=18,
        source_start_pts_sec=100.0,
    )

    assert command[command.index("-ss") + 1] == "5"
    assert "trim=start_pts=115000:end_pts=117000,setpts=PTS-STARTPTS" in command


def test_source_frame_index_prefers_safe_fast_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        (DecodedFrameTimestamp(0, 0, 40, "packet_pts"),),
    )
    monkeypatch.setattr(
        clip_export,
        "probe_fast_frame_index",
        lambda *_args, **_kwargs: frame_index,
    )
    monkeypatch.setattr(
        clip_export,
        "probe_decoded_frame_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("decoded fallback must not run")
        ),
    )

    assert (
        clip_export._probe_source_frame_index(Path("source.mp4"), Path("ffmpeg"))
        is frame_index
    )


def test_source_frame_index_falls_back_when_fast_probe_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        (DecodedFrameTimestamp(0, 0, 40, "pts"),),
    )
    monkeypatch.setattr(
        clip_export, "probe_fast_frame_index", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        clip_export,
        "probe_decoded_frame_index",
        lambda *_args, **_kwargs: frame_index,
    )

    assert (
        clip_export._probe_source_frame_index(Path("source.mp4"), Path("ffmpeg"))
        is frame_index
    )


def test_output_frame_count_prefers_declared_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        clip_export,
        "probe_declared_video_frame_count",
        lambda *_args, **_kwargs: 1443,
    )
    monkeypatch.setattr(
        clip_export,
        "probe_decoded_frame_index",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("decoded fallback must not run")
        ),
    )

    assert (
        clip_export._probe_output_frame_count(Path("clip.mp4"), Path("ffmpeg"))
        == 1443
    )


def test_output_frame_count_falls_back_when_declaration_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, ordinal * 40, 40, "pts")
            for ordinal in range(3)
        ),
    )
    monkeypatch.setattr(
        clip_export,
        "probe_declared_video_frame_count",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        clip_export,
        "probe_decoded_frame_index",
        lambda *_args, **_kwargs: frame_index,
    )

    assert clip_export._probe_output_frame_count(Path("clip.mp4"), Path("ffmpeg")) == 3


def test_adjacent_export_maps_partition_source_frames_once() -> None:
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, 40, "pts")
            for ordinal, pts in enumerate((5000, 5040, 5080, 5120))
        ),
    )
    clips = [
        clip_export.ExportClip("clip-0001", 5000, 5080, frame_index.time_base),
        clip_export.ExportClip("clip-0002", 5080, 5160, frame_index.time_base),
    ]

    sidecar = clip_export.build_clip_frame_map(frame_index, clips)
    mapped = [
        (frame["ordinal"], frame["pts"])
        for clip in sidecar["clips"]
        for frame in clip["frames"]
    ]

    assert mapped == [(frame.ordinal, frame.pts) for frame in frame_index.frames]


def test_subset_frame_map_keeps_exact_requested_frames_without_full_source_claim() -> (
    None
):
    frame_index = DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, 40, "pts")
            for ordinal, pts in enumerate((5000, 5040, 5080, 5120))
        ),
    )
    selected = [clip_export.ExportClip("clip-0002", 5080, 5160, frame_index.time_base)]

    with pytest.raises(ValueError, match="partition"):
        clip_export.build_clip_frame_map(frame_index, selected)
    sidecar = clip_export.build_clip_frame_map(
        frame_index, selected, require_full_source_partition=False
    )

    assert [item["pts"] for item in sidecar["clips"][0]["frames"]] == [5080, 5120]
    assert sidecar["full_source_partition"] is False


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


@pytest.mark.parametrize(
    "preset,crf", [("ultrafast", -1), ("unknown", 18), ("fast", 52)]
)
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
                "source_start_pts": 0,
                "source_end_pts_exclusive": 2000,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 0.0,
                "source_end_pts_exclusive_sec": 2.0,
            },
            {
                "clip_id": "clip-0002",
                "source_start_pts": 2000,
                "source_end_pts_exclusive": 4500,
                "source_time_base": {"numerator": 1, "denominator": 1000},
                "source_start_pts_sec": 2.0,
                "source_end_pts_exclusive_sec": 4.5,
            },
        ],
    )

    clips = load_export_clips(manifest)

    assert [(item.clip_id, item.duration_sec) for item in clips] == [
        ("clip-0001", 2.0),
        ("clip-0002", 2.5),
    ]


def test_load_export_clips_uses_exact_pts_below_sixty_not_rounded_seconds(
    tmp_path: Path,
) -> None:
    denominator = 10**18
    end_pts_exclusive = 60 * denominator - 1
    manifest = _write_manifest(
        tmp_path,
        [
            {
                "clip_id": "clip-0001",
                "source_start_pts": 0,
                "source_end_pts_exclusive": end_pts_exclusive,
                "source_time_base": {
                    "numerator": 1,
                    "denominator": denominator,
                },
            }
        ],
    )

    clips = load_export_clips(manifest)

    assert len(clips) == 1
    assert (
        Fraction(clips[0].source_end_pts_exclusive - clips[0].source_start_pts)
        * clips[0].source_time_base
        < 60
    )


def test_load_export_clips_allows_explicit_bounded_solve_duration(
    tmp_path: Path,
) -> None:
    manifest = _write_manifest(
        tmp_path,
        [_integer_pts_clip("target-solve", 0, 65_000)],
    )

    clips = load_export_clips(manifest, max_duration_seconds=66)

    assert [(item.clip_id, item.duration_sec) for item in clips] == [
        ("target-solve", 65.0)
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
        [_integer_pts_clip("../escape", 0, 2000)],
        [_integer_pts_clip("clip-0001", float("nan"), 2000)],
        [_integer_pts_clip("clip-0001", float("inf"), 2000)],
        [_integer_pts_clip("clip-0001", -float("inf"), 2000)],
        [_integer_pts_clip("clip-0001", 2000, 2000)],
        [
            _integer_pts_clip("clip-0001", 0, 2000),
            _integer_pts_clip("clip-0002", 1500, 3000),
        ],
        [
            _integer_pts_clip("clip-0001", 0, 2000),
            _integer_pts_clip("clip-0001", 2000, 3000),
        ],
        [_integer_pts_clip("clip-0001", 0, 60000)],
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
