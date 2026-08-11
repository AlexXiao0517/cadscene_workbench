from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from cadscene.projects.concat import ConcatPlan, ConcatPlanEntry
from cadscene.projects.concat_adapters import (
    ConcatMediaAdapter,
    ConcatMediaInputs,
)
from cadscene.projects.adapters import AdapterResult
from cadscene.projects.concat_executor import (
    build_audio_mux_ffmpeg_command,
    build_concat_ffmpeg_command,
    build_normalize_ffmpeg_command,
    execute_concat_media,
    execution_plan_from_payload,
    validate_concat_outputs,
)
from cadscene.projects.media import ProjectMediaSpec, parse_ffprobe
from cadscene.projects.source_fallback import SourceIntervalRenderInputs, render_source_interval
from cadscene.video_analysis.pts import (
    DecodedFrameIndex,
    DecodedFrameTimestamp,
    probe_decoded_frame_index,
    resolve_ffmpeg_executable,
)



def _sha(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _spec() -> ProjectMediaSpec:
    return ProjectMediaSpec(
        width=320,
        height=180,
        display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1),
        pixel_format="yuv420p",
        codec_name="h264",
        profile="High",
        time_base=Fraction(1, 1000),
        color_range="tv",
        color_space="bt709",
        color_transfer="bt709",
        color_primaries="bt709",
        nominal_frame_rate=None,
    )


def _fixture(tmp_path: Path, *, normalize_second: bool = True) -> ConcatMediaInputs:
    source = (tmp_path / "source.mp4").resolve()
    source.write_bytes(b"authoritative-source")
    attempt = (tmp_path / "attempt").resolve()
    attempt.mkdir()
    frames = tuple(
        DecodedFrameTimestamp(index, pts, 40, "pts")
        for index, pts in enumerate((1000, 1040, 1080, 1120))
    )
    index = DecodedFrameIndex(Fraction(1, 1000), frames)
    entries = []
    for order, selected in enumerate((frames[:2], frames[2:])):
        video = (tmp_path / f"clip-{order}.mp4").resolve()
        frame_map = (tmp_path / f"clip-{order}.json").resolve()
        video.write_bytes(f"video-{order}".encode())
        frame_map.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_time_base": {"numerator": 1, "denominator": 1000},
                    "frames": [
                        {
                            "output_frame_ordinal": output,
                            "source_decoded_frame_ordinal": frame.ordinal,
                            "source_pts": frame.pts,
                        }
                        for output, frame in enumerate(selected)
                    ],
                }
            ),
            encoding="utf-8",
        )
        entries.append(
            ConcatPlanEntry(
                clip_id=f"clip-{order}", render_order=order, selection="rendered",
                status="ready", reason="validated", source_start_pts=selected[0].pts,
                source_end_pts_exclusive=selected[-1].pts + 40,
                source_time_base=index.time_base, source_frames=selected,
                input_video_path=str(video), input_frame_map_path=str(frame_map),
                input_output_revision=f"render-{order}",
                input_output_fingerprint=str(order + 1) * 64,
                input_proof_fingerprint=str(order + 3) * 64,
                input_video_sha256=_sha(video), input_frame_map_sha256=_sha(frame_map),
                input_publication_operation_id=f"operation-{order}", ready=True,
                dependency_required=False,
                needs_normalize=normalize_second and order == 1,
            )
        )
    plan = ConcatPlan(
        project_id="project-1", project_revision=7, clips_revision=9,
        project_media_spec_revision="media-spec-1",
        source_asset_fingerprint=_sha(source), entries=tuple(entries),
        audio_source=str(source),
    )
    return ConcatMediaInputs(
        plan=plan, source_frame_index=index, source_video_path=source,
        attempt_directory=attempt, project_media_spec=_spec(),
    )


def _probe_payload(
    *, frame_count: int = 4, audio: bool = True, width: int = 320
) -> dict[str, object]:
    streams: list[dict[str, object]] = [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "profile": "High",
            "width": width,
            "height": 180,
            "sample_aspect_ratio": "1:1",
            "pix_fmt": "yuv420p",
            "time_base": "1/1000",
            "avg_frame_rate": "0/0",
            "color_range": "tv",
            "color_space": "bt709",
            "color_transfer": "bt709",
            "color_primaries": "bt709",
        }
    ]
    if audio:
        streams.append(
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "profile": "LC",
                "sample_fmt": "fltp",
                "sample_rate": "48000",
                "channels": 2,
                "channel_layout": "stereo",
                "time_base": "1/48000",
                "duration": "0.160",
            }
        )
    return {
        "streams": streams,
        "frames": [
            {
                "media_type": "video",
                "stream_index": 0,
                "pts": str(index * 40),
                "pkt_duration": "40",
            }
            for index in range(frame_count)
        ],
        "format": {"duration": "0.160"},
    }


def _probe_for_path(path: Path, *, audio: bool = False):
    count = 2 if path.name.startswith("clip-") else 4
    return parse_ffprobe(_probe_payload(frame_count=count, audio=audio and count == 4))


def _plain(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def test_command_builders_preserve_frames_and_use_original_audio(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    execution = ConcatMediaAdapter().prepare(inputs)
    normalize = build_normalize_ffmpeg_command(
        execution,
        execution.segments[1],
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        output_path=execution.segments[1].concat_input,
    )
    concat_list = inputs.attempt_directory / "concat.txt"
    concat_list.write_text("file 'clip.mp4'\n", encoding="utf-8")
    concat = build_concat_ffmpeg_command(
        execution,
        project_media_spec=inputs.project_media_spec,
        concat_list_path=concat_list,
        ffmpeg_executable="ffmpeg",
        output_path=execution.video_only_output,
    )
    execution.video_only_output.write_bytes(b"video-only")
    mux = build_audio_mux_ffmpeg_command(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        input_video=execution.video_only_output,
        output_path=execution.final_output,
    )

    assert "-fps_mode" in normalize and normalize[normalize.index("-fps_mode") + 1] == "passthrough"
    assert "-r" not in normalize and "-bf" in normalize
    assert "setpts=PTS-STARTPTS" in normalize[normalize.index("-vf") + 1]
    assert "-an" in normalize
    assert "-f" in concat and concat[concat.index("-f") + 1] == "concat"
    assert "-c:v" in concat and concat[concat.index("-c:v") + 1] == "copy"
    assert "-an" in concat
    assert str(execution.audio_source) in mux
    assert "-map" in mux and "0:v:0" in mux
    assert "-shortest" not in mux
    assert "atrim=duration=0.16,asetpts=PTS-STARTPTS" in mux


def test_execution_rejects_media_spec_changed_after_snapshot(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    execution = ConcatMediaAdapter().prepare(inputs)

    with pytest.raises(ValueError, match="media spec.*fingerprint"):
        build_normalize_ffmpeg_command(
            execution,
            execution.segments[1],
            project_media_spec=replace(inputs.project_media_spec, width=640),
            ffmpeg_executable="ffmpeg",
            output_path=execution.segments[1].concat_input,
        )


def test_executor_rechecks_inputs_before_and_after_commands(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    calls = 0

    def runner(command: tuple[str, ...]) -> None:
        nonlocal calls
        calls += 1
        Path(command[-1]).write_bytes(b"output")
        if calls == 1:
            execution.segments[0].source_video.write_bytes(b"changed")

    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=runner,
        media_probe=lambda path: _probe_for_path(path, audio=True),
    )

    assert result.status == "failed"
    assert "fingerprint" in (result.error or "")
    assert not execution.final_output.exists()


def test_validation_rejects_wrong_final_frames_map_or_media(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    bad_map = _plain(execution.final_frame_map)
    bad_map["frames"] = list(bad_map["frames"])[1:]
    execution.final_frame_map_path.write_text(json.dumps(bad_map), encoding="utf-8")

    with pytest.raises(ValueError, match="frame map|frame count"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda path: _probe_for_path(path, audio=True),
        )


def test_standalone_validation_requires_original_source_audio(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final-without-audio")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="missing original-source audio"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(_probe_payload(audio=False)),
            source_media_probe=lambda _path: parse_ffprobe(_probe_payload(audio=True)),
        )


def test_standalone_validation_rechecks_snapshot_spec_and_input_bytes(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="media spec fingerprint"):
        validate_concat_outputs(
            execution,
            project_media_spec=replace(inputs.project_media_spec, width=640),
            media_probe=lambda _path: parse_ffprobe(_probe_payload(audio=False)),
        )

    execution.segments[0].source_video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="fingerprint"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(_probe_payload(audio=False)),
        )


def test_executor_rejects_segment_maps_that_do_not_flatten_to_final_map(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    first_map = json.loads(
        Path(inputs.plan.entries[0].input_frame_map_path).read_text(encoding="utf-8")
    )
    second_path = Path(inputs.plan.entries[1].input_frame_map_path)
    second_path.write_text(json.dumps(first_map), encoding="utf-8")
    second_entry = replace(
        inputs.plan.entries[1], input_frame_map_sha256=_sha(second_path)
    )
    inputs = replace(
        inputs,
        plan=replace(
            inputs.plan, entries=(inputs.plan.entries[0], second_entry)
        ),
    )
    execution = ConcatMediaAdapter().prepare(inputs)

    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=lambda command: Path(command[-1]).write_bytes(b"video"),
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert result.status == "failed"
    assert "segment frame maps" in (result.error or "")


def test_missing_packet_durations_use_video_stream_not_container_duration(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )
    payload = _probe_payload(audio=True)
    payload["streams"][0]["duration_ts"] = "160"
    payload["streams"][0]["duration"] = "0.160"
    payload["format"]["duration"] = "0.200"
    for frame in payload["frames"]:
        frame.pop("pkt_duration", None)

    proof = validate_concat_outputs(
        execution,
        project_media_spec=inputs.project_media_spec,
        media_probe=lambda _path: parse_ffprobe(payload),
    )

    assert proof["rendered_frame_count"] == 4

    payload["streams"][0]["duration_ts"] = "150"
    with pytest.raises(ValueError, match="video duration"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(payload),
        )


def test_float_stream_duration_is_not_authoritative_timing_evidence(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )
    payload = _probe_payload(audio=False)
    payload["streams"][0]["duration"] = "0.160"
    for frame in payload["frames"]:
        frame.pop("pkt_duration", None)

    with pytest.raises(ValueError, match="frame durations are incomplete"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(payload),
        )


def test_packet_duration_sum_cannot_hide_wrong_final_frame_end(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )
    payload = _probe_payload(audio=False)
    # The packet-duration sum is the expected 160 ms, but the final frame ends
    # at 140 ms (last PTS 120 + duration 20).  Summing packet durations would
    # therefore hide the incorrect final frame end.
    for frame, duration in zip(payload["frames"], (20, 60, 60, 20)):
        frame["pkt_duration"] = str(duration)

    with pytest.raises(ValueError, match="video duration"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(payload),
        )


def test_validation_fails_closed_when_final_timing_loses_vfr_gap(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    execution.final_output.parent.mkdir()
    execution.final_output.write_bytes(b"final")
    execution.final_frame_map_path.write_text(
        json.dumps(_plain(execution.final_frame_map)), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="timing"):
        validate_concat_outputs(
            execution,
            project_media_spec=inputs.project_media_spec,
            media_probe=lambda _path: parse_ffprobe(
                {
                    **_probe_payload(),
                    "frames": [
                        {
                            "media_type": "video", "stream_index": 0,
                            "pts": str(index * 30), "pkt_duration": "30",
                        }
                        for index in range(4)
                    ],
                }
            ),
        )


def test_video_only_source_is_explicitly_supported(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)

    def runner(command: tuple[str, ...]) -> None:
        Path(command[-1]).write_bytes(b"video")

    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=runner,
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert result.status == "success"
    assert result.validation_proof["audio_policy"] == "video_only"
    assert result.progress[-1].fraction is None
    assert Path(result.outputs["video"]).is_file()
    assert Path(result.outputs["frame_map"]).is_file()


def test_executor_uses_random_temporaries_and_does_not_publish_failed_output(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)

    def runner(command: tuple[str, ...]) -> None:
        raise RuntimeError("encoder failed")

    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=runner,
        media_probe=lambda _path: parse_ffprobe(_probe_payload(audio=False)),
    )

    assert result.status == "failed"
    assert not execution.final_output.exists()
    assert not execution.final_frame_map_path.exists()


def test_atomic_bundle_install_failure_publishes_neither_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cadscene.projects.concat_executor as executor_module

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    real_replace = executor_module.os.replace

    def replace_with_failure(source: Path, destination: Path) -> None:
        if Path(destination) == execution.final_output.parent:
            raise OSError("simulated bundle install failure")
        real_replace(source, destination)

    monkeypatch.setattr(executor_module.os, "replace", replace_with_failure)
    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=lambda command: Path(command[-1]).write_bytes(b"video"),
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert result.status == "failed"
    assert not execution.final_output.exists()
    assert not execution.final_frame_map_path.exists()


def test_no_fallible_hashing_occurs_after_atomic_outputs_are_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cadscene.projects.concat_executor as executor_module

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    real_sha = executor_module._sha256_file

    def fail_if_already_published(path: Path) -> str:
        if Path(path) in {execution.final_output, execution.final_frame_map_path}:
            raise OSError("post-publication hashing must not occur")
        return real_sha(path)

    monkeypatch.setattr(executor_module, "_sha256_file", fail_if_already_published)
    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=lambda command: Path(command[-1]).write_bytes(b"video"),
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert result.status == "success", result.error
    assert execution.final_output.is_file()
    assert execution.final_frame_map_path.is_file()


def test_post_rename_directory_fsync_failure_is_not_reported_as_unpublished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cadscene.projects.concat_executor as executor_module

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    calls = 0

    def fail_only_after_rename(_path: Path, *, suppress_errors: bool = False) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated parent directory fsync failure")
        return True

    monkeypatch.setattr(
        executor_module, "_fsync_directory_if_supported", fail_only_after_rename
    )
    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=lambda command: Path(command[-1]).write_bytes(b"video"),
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert calls == 2
    assert result.status == "success", result.error
    assert execution.final_output.is_file()
    assert execution.final_frame_map_path.is_file()


def test_validation_race_before_bundle_install_publishes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cadscene.projects.concat_executor as executor_module

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    monkeypatch.setattr(
        executor_module,
        "_publish_bundle",
        lambda _source, _destination, **_kwargs: (_ for _ in ()).throw(
            ValueError("simulated validation race")
        ),
    )
    result = execute_concat_media(
        execution,
        project_media_spec=inputs.project_media_spec,
        ffmpeg_executable="ffmpeg",
        command_runner=lambda command: Path(command[-1]).write_bytes(b"video"),
        media_probe=lambda path: _probe_for_path(path, audio=False),
    )

    assert result.status == "failed"
    assert not execution.final_output.exists()
    assert not execution.final_frame_map_path.exists()


def test_execution_snapshot_roundtrip_is_strict_and_does_not_restore_callable(
    tmp_path: Path,
) -> None:
    inputs = _fixture(tmp_path)
    execution = ConcatMediaAdapter().prepare(inputs)
    payload = execution.to_dict()

    restored = execution_plan_from_payload(payload)

    assert restored.to_dict() == payload
    assert restored.validate().status == "failed"
    payload["unexpected"] = "ignored-state-is-dangerous"
    with pytest.raises(ValueError, match="unknown"):
        execution_plan_from_payload(payload)


def test_execution_snapshot_rejects_unsafe_or_duplicate_clip_ids(
    tmp_path: Path,
) -> None:
    execution = ConcatMediaAdapter().prepare(_fixture(tmp_path))
    unsafe = json.loads(json.dumps(execution.to_dict()))
    unsafe["segments"][0]["clip_id"] = "../escape"
    with pytest.raises(ValueError, match="clip_id"):
        execution_plan_from_payload(unsafe)

    duplicate = json.loads(json.dumps(execution.to_dict()))
    duplicate["segments"][1]["clip_id"] = duplicate["segments"][0]["clip_id"]
    with pytest.raises(ValueError, match="unique"):
        execution_plan_from_payload(duplicate)


def test_concat_cli_consumes_execution_snapshot_and_emits_structured_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cadscene.cli import concat_media

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    snapshot = tmp_path / "execution.json"
    spec = tmp_path / "spec.json"
    snapshot.write_text(json.dumps(execution.to_dict()), encoding="utf-8")
    spec.write_text(json.dumps(inputs.project_media_spec.to_dict()), encoding="utf-8")
    monkeypatch.setattr(
        concat_media,
        "execute_concat_media",
        lambda *_args, **_kwargs: AdapterResult.success(
            output_revision="concat-1", output_fingerprint="a" * 64,
            outputs={"video": str(execution.final_output)},
        ),
    )

    code = concat_media.main(
        [
            "--execution-plan-json",
            str(snapshot),
            "--project-media-spec-json",
            str(spec),
            "--attempt-directory",
            str(inputs.attempt_directory),
        ]
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "success"


def test_concat_cli_rejects_snapshot_outside_authorized_attempt_directory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from cadscene.cli import concat_media

    inputs = _fixture(tmp_path, normalize_second=False)
    execution = ConcatMediaAdapter().prepare(inputs)
    snapshot = tmp_path / "execution.json"
    spec = tmp_path / "spec.json"
    other_attempt = tmp_path / "other-attempt"
    other_attempt.mkdir()
    snapshot.write_text(json.dumps(execution.to_dict()), encoding="utf-8")
    spec.write_text(json.dumps(inputs.project_media_spec.to_dict()), encoding="utf-8")

    code = concat_media.main(
        [
            "--execution-plan-json",
            str(snapshot),
            "--project-media-spec-json",
            str(spec),
            "--attempt-directory",
            str(other_attempt),
        ]
    )

    assert code == 1
    assert "not authorized" in json.loads(capsys.readouterr().out)["error"]


@pytest.mark.skipif(
    shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for real concat integration",
)
def test_real_vfr_adjacent_segments_normalize_concat_and_mux_original_audio(
    tmp_path: Path,
) -> None:
    ffmpeg = resolve_ffmpeg_executable()
    source = (tmp_path / "source-with-audio.mkv").resolve()
    video = (tmp_path / "source-video.mp4").resolve()
    subprocess.run(
        (
            str(ffmpeg), "-v", "error", "-f", "lavfi", "-i",
            "testsrc=size=64x48:rate=25:duration=0.2", "-vf",
            "settb=1/1000,select='eq(n,0)+eq(n,1)+eq(n,3)+eq(n,4)',setpts=PTS+5000",
            "-fps_mode", "passthrough", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-enc_time_base", "1/1000",
            "-video_track_timescale", "1000", "-color_range", "tv",
            "-colorspace", "bt709", "-color_trc", "bt709", "-color_primaries",
            "bt709", "-use_editlist", "0", str(video),
        ),
        check=True,
    )
    subprocess.run(
        (
            str(ffmpeg), "-v", "error", "-copyts", "-i", str(video),
            "-itsoffset", "5", "-f", "lavfi", "-i",
            "sine=frequency=440:sample_rate=48000:duration=0.2", "-map", "0:v:0",
            "-map", "1:a:0", "-c:v", "copy", "-c:a", "alac", "-use_editlist",
            "0", "-color_range", "tv", "-colorspace", "bt709", "-color_trc",
            "bt709", "-color_primaries", "bt709", str(source),
        ),
        check=True,
    )
    index = probe_decoded_frame_index(source, ffmpeg_executable=ffmpeg)
    assert index.source_start_pts != 0
    assert len(set(right.pts - left.pts for left, right in zip(index.frames, index.frames[1:]))) > 1
    spec = ProjectMediaSpec(
        width=64, height=48, display_orientation_baked=True,
        sample_aspect_ratio=Fraction(1, 1), pixel_format="yuv420p",
        codec_name="h264", profile="High", time_base=Fraction(1, 1000),
        color_range="tv", color_space="bt709", color_transfer="bt709",
        color_primaries="bt709", nominal_frame_rate=None,
    )
    boundary = index.frames[2].pts
    entries = []
    for order, (start, end) in enumerate(
        ((index.source_start_pts, boundary), (boundary, index.source_end_pts_exclusive))
    ):
        attempt = (tmp_path / f"segment-{order}").resolve()
        attempt.mkdir()
        segment_spec = replace(spec, width=32) if order == 1 else spec
        segment_video, segment_map = render_source_interval(
            SourceIntervalRenderInputs(
                project_id="project-real-concat", clip_id=f"clip-{order}",
                source_video_path=source, source_start_pts=start,
                source_end_pts_exclusive=end, source_time_base=index.time_base,
                attempt_directory=attempt, project_media_spec=segment_spec,
            ),
            ffmpeg_executable=ffmpeg,
        )
        selected = tuple(frame for frame in index.frames if start <= frame.pts < end)
        entries.append(
            ConcatPlanEntry(
                clip_id=f"clip-{order}", render_order=order,
                selection="source_fallback", status="ready", reason="validated",
                source_start_pts=start, source_end_pts_exclusive=end,
                source_time_base=index.time_base, source_frames=selected,
                input_video_path=str(segment_video), input_frame_map_path=str(segment_map),
                input_output_revision=f"fallback-{order}",
                input_output_fingerprint=str(order + 1) * 64,
                input_proof_fingerprint=str(order + 3) * 64,
                input_video_sha256=_sha(segment_video),
                input_frame_map_sha256=_sha(segment_map),
                input_publication_operation_id=f"operation-{order}", ready=True,
                dependency_required=False, needs_normalize=order == 1,
                media_differences=("width",) if order == 1 else (),
            )
        )
    plan = ConcatPlan(
        project_id="project-real-concat", project_revision=1, clips_revision=1,
        project_media_spec_revision="media-1", source_asset_fingerprint=_sha(source),
        entries=tuple(entries), audio_source=str(source),
    )
    merge_attempt = (tmp_path / "merge-attempt").resolve()
    merge_attempt.mkdir()
    execution = ConcatMediaAdapter().prepare(
        ConcatMediaInputs(
            plan=plan, source_frame_index=index, source_video_path=source,
            attempt_directory=merge_attempt, project_media_spec=spec,
        )
    )

    result = execute_concat_media(
        execution, project_media_spec=spec, ffmpeg_executable=ffmpeg,
    )

    assert result.status == "success", result.error
    final_map = json.loads(execution.final_frame_map_path.read_text(encoding="utf-8"))
    assert [item["source_pts"] for item in final_map["frames"]] == [frame.pts for frame in index.frames]
    assert result.validation_proof["audio_policy"] == "original_video"
