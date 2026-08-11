from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.projects.adapters import AdapterResult
from cadscene.projects.concat import ConcatPlan, ConcatPlanEntry
from cadscene.projects.concat_adapters import ConcatMediaAdapter, ConcatMediaInputs
from cadscene.projects.media import ProjectMediaSpec
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp


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


def _fixture(tmp_path: Path, *, normalize_second: bool = True):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"authoritative-source")
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    frames = tuple(
        DecodedFrameTimestamp(index, pts, 40, "pts")
        for index, pts in enumerate((1000, 1040, 1080, 1120))
    )
    index = DecodedFrameIndex(Fraction(1, 1000), frames)
    entries = []
    for order, selected in enumerate((frames[:2], frames[2:])):
        video = tmp_path / f"clip-{order}.mp4"
        frame_map = tmp_path / f"clip-{order}.json"
        video.write_bytes(f"video-{order}".encode())
        frame_map.write_text(json.dumps({"clip": order}), encoding="utf-8")
        entries.append(
            ConcatPlanEntry(
                clip_id=f"clip-{order}",
                render_order=order,
                selection="rendered",
                status="ready",
                reason="validated",
                source_start_pts=selected[0].pts,
                source_end_pts_exclusive=selected[-1].pts + 40,
                source_time_base=index.time_base,
                source_frames=selected,
                input_video_path=str(video),
                input_frame_map_path=str(frame_map),
                input_output_revision=f"render-{order}",
                input_output_fingerprint=str(order + 1) * 64,
                input_proof_fingerprint=str(order + 3) * 64,
                input_video_sha256=_sha(video),
                input_frame_map_sha256=_sha(frame_map),
                input_publication_operation_id=f"operation-{order}",
                ready=True,
                dependency_required=False,
                needs_normalize=normalize_second and order == 1,
            )
        )
    plan = ConcatPlan(
        project_id="project-1",
        project_revision=7,
        clips_revision=9,
        project_media_spec_revision="media-spec-1",
        source_asset_fingerprint=_sha(source),
        entries=tuple(entries),
        audio_source=str(source),
    )
    inputs = ConcatMediaInputs(
        plan=plan,
        source_frame_index=index,
        source_video_path=source,
        attempt_directory=attempt,
        project_media_spec=_spec(),
    )
    return inputs


def test_adapter_prepares_source_order_normalization_and_original_audio(tmp_path: Path):
    inputs = _fixture(tmp_path)
    adapter = ConcatMediaAdapter(
        validator=lambda _inputs, _plan: AdapterResult.success(
            output_revision="merge-output-1",
            output_fingerprint="a" * 64,
            outputs={"video": str(tmp_path / "attempt" / "final.mp4")},
        )
    )

    execution = adapter.prepare(inputs)

    assert [item.clip_id for item in execution.segments] == ["clip-0", "clip-1"]
    assert [item.needs_normalize for item in execution.segments] == [False, True]
    assert execution.segments[0].concat_input == Path(inputs.plan.entries[0].input_video_path)
    assert execution.segments[1].concat_input.parent.name == "normalized"
    assert execution.audio_source == inputs.source_video_path
    assert execution.expected_video_duration == Fraction(4, 25)
    assert execution.audio_video_tolerance == Fraction(1, 20)
    assert [item["source_pts"] for item in execution.final_frame_map["frames"]] == [
        1000,
        1040,
        1080,
        1120,
    ]
    assert execution.validate().status == "success"
    json.dumps(execution.to_dict(), allow_nan=False)


@pytest.mark.parametrize("target", ("source", "video", "frame_map"))
def test_prepare_rejects_any_input_bytes_changed_after_plan(tmp_path: Path, target: str):
    inputs = _fixture(tmp_path)
    if target == "source":
        path = inputs.source_video_path
    elif target == "video":
        path = Path(inputs.plan.entries[0].input_video_path)
    else:
        path = Path(inputs.plan.entries[0].input_frame_map_path)
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(ValueError, match="fingerprint|SHA-256"):
        ConcatMediaAdapter().prepare(inputs)


@pytest.mark.parametrize("change", ("dependency", "not_ready"))
def test_execution_rejects_dependency_or_unready_plan_entries(
    tmp_path: Path, change: str
):
    inputs = _fixture(tmp_path)
    entry = inputs.plan.entries[1]
    if change == "dependency":
        changed = replace(entry, dependency_required=True)
    else:
        changed = replace(entry, ready=False)
    inputs = replace(inputs, plan=replace(inputs.plan, entries=(inputs.plan.entries[0], changed)))

    with pytest.raises(ValueError, match="ready|dependency"):
        ConcatMediaAdapter().prepare(inputs)


def test_validator_must_return_structured_adapter_result(tmp_path: Path):
    execution = ConcatMediaAdapter(validator=lambda _inputs, _plan: object()).prepare(
        _fixture(tmp_path)
    )

    with pytest.raises(TypeError, match="AdapterResult"):
        execution.validate()


def test_validator_rechecks_inputs_changed_after_prepare(tmp_path: Path):
    inputs = _fixture(tmp_path)
    execution = ConcatMediaAdapter(
        validator=lambda _inputs, _plan: AdapterResult.failed("not reached")
    ).prepare(inputs)
    Path(inputs.plan.entries[1].input_video_path).write_bytes(b"changed-after-prepare")

    with pytest.raises(ValueError, match="fingerprint"):
        execution.validate()


def test_validator_rejects_source_replaced_by_same_bytes_symlink(tmp_path: Path):
    inputs = _fixture(tmp_path)
    execution = ConcatMediaAdapter(
        validator=lambda _inputs, _plan: AdapterResult.failed("not reached")
    ).prepare(inputs)
    replacement = tmp_path / "same-source-bytes.mp4"
    replacement.write_bytes(inputs.source_video_path.read_bytes())
    inputs.source_video_path.unlink()
    try:
        inputs.source_video_path.symlink_to(replacement)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="regular file"):
        execution.validate()


def test_mutable_plan_input_cannot_redirect_validation_after_prepare(tmp_path: Path):
    inputs = _fixture(tmp_path)
    mutable_entries = list(inputs.plan.entries)
    inputs = replace(inputs, plan=replace(inputs.plan, entries=mutable_entries))
    execution = ConcatMediaAdapter(
        validator=lambda _inputs, _plan: AdapterResult.failed("not reached")
    ).prepare(inputs)
    original_video = execution.segments[0].source_video
    replacement_video = tmp_path / "replacement.mp4"
    replacement_map = tmp_path / "replacement.json"
    replacement_video.write_bytes(b"clean-replacement")
    replacement_map.write_bytes(b"clean-replacement-map")
    mutable_entries[0] = replace(
        mutable_entries[0],
        input_video_path=str(replacement_video),
        input_frame_map_path=str(replacement_map),
        input_video_sha256=_sha(replacement_video),
        input_frame_map_sha256=_sha(replacement_map),
    )
    original_video.write_bytes(b"changed-original-execution-input")

    with pytest.raises(ValueError, match="fingerprint"):
        execution.validate()


@pytest.mark.parametrize("change", ("unknown_normalize", "bad_render_order"))
def test_prepare_rejects_ambiguous_execution_state(tmp_path: Path, change: str):
    inputs = _fixture(tmp_path)
    entry = inputs.plan.entries[1]
    changed = (
        replace(entry, needs_normalize=None)
        if change == "unknown_normalize"
        else replace(entry, render_order=7)
    )
    inputs = replace(inputs, plan=replace(inputs.plan, entries=(inputs.plan.entries[0], changed)))

    with pytest.raises(ValueError, match="normalize|render_order"):
        ConcatMediaAdapter().prepare(inputs)


def test_execution_snapshot_rejects_output_path_escape(tmp_path: Path):
    execution = ConcatMediaAdapter().prepare(_fixture(tmp_path))

    with pytest.raises(ValueError, match="attempt directory"):
        replace(execution, final_frame_map_path=tmp_path / "escaped-map.json")
