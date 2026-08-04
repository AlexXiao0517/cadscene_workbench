from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
import json

import pytest

from cadscene.projects.concat import (
    ConcatClip,
    ConcatPreflightRequest,
    FallbackArtifact,
    RenderCandidate,
    SourceFallbackConfirmation,
    build_concat_plan,
    build_final_frame_map,
    clip_interval_fingerprint,
    preflight_concat,
)
from cadscene.projects.media import ProjectMediaSpec, parse_ffprobe
from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp


def _source_index() -> DecodedFrameIndex:
    return DecodedFrameIndex(
        Fraction(1, 1000),
        tuple(
            DecodedFrameTimestamp(ordinal, pts, 40, "pts")
            for ordinal, pts in enumerate((1000, 1040, 1080, 1120))
        ),
    )


def _media_spec(**changes: object) -> ProjectMediaSpec:
    values: dict[str, object] = {
        "width": 320,
        "height": 180,
        "display_orientation_baked": True,
        "sample_aspect_ratio": Fraction(1, 1),
        "pixel_format": "yuv420p",
        "codec_name": "h264",
        "profile": "High",
        "time_base": Fraction(1, 1000),
        "color_range": "tv",
        "color_space": "bt709",
        "color_transfer": "bt709",
        "color_primaries": "bt709",
        "nominal_frame_rate": Fraction(25, 1),
    }
    values.update(changes)
    return ProjectMediaSpec(**values)


def _frame_map(frames: tuple[DecodedFrameTimestamp, ...]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_time_base": {"numerator": 1, "denominator": 1000},
        "frames": [
            {
                "output_frame_ordinal": output,
                "source_decoded_frame_ordinal": frame.ordinal,
                "source_pts": frame.pts,
            }
            for output, frame in enumerate(frames)
        ],
    }


def _probe(*, width: int = 320, pts: tuple[int, ...] = (0, 40)):
    return parse_ffprobe(
        {
            "streams": [
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
                    "avg_frame_rate": "25/1",
                    "color_range": "tv",
                    "color_space": "bt709",
                    "color_transfer": "bt709",
                    "color_primaries": "bt709",
                }
            ],
            "frames": [
                {
                    "media_type": "video",
                    "stream_index": 0,
                    "pts": str(value),
                    "pkt_duration": "40",
                }
                for value in pts
            ],
            "format": {"duration": "0.08"},
        }
    )


def _clips() -> tuple[ConcatClip, ConcatClip]:
    index = _source_index()
    first = index.frames[:2]
    second = index.frames[2:]
    return (
        ConcatClip(
            clip_id="clip-1",
            render_order=0,
            analysis_revision="analysis-1",
            resolved_workflow="sfm_only",
            source_start_pts=1000,
            source_end_pts_exclusive=1080,
            source_time_base=index.time_base,
            authoritative_frame_map=_frame_map(first),
            current_render_input_fingerprint="1" * 64,
        ),
        ConcatClip(
            clip_id="clip-2",
            render_order=1,
            analysis_revision="analysis-1",
            resolved_workflow="pure_rotation",
            source_start_pts=1080,
            source_end_pts_exclusive=1160,
            source_time_base=index.time_base,
            authoritative_frame_map=_frame_map(second),
            current_render_input_fingerprint="2" * 64,
        ),
    )


def _candidate(clip: ConcatClip, *, probe=None) -> RenderCandidate:
    return RenderCandidate(
        project_id="project-1",
        clip_id=clip.clip_id,
        workflow=clip.resolved_workflow,
        exact_validated=True,
        input_fingerprint=clip.current_render_input_fingerprint,
        output_revision=f"render-{clip.clip_id}",
        output_fingerprint="a" * 64,
        proof_fingerprint="b" * 64,
        video_sha256="c" * 64,
        frame_map_sha256="d" * 64,
        publication_operation_id=f"operation-{clip.clip_id}",
        video_path=f"C:/project/{clip.clip_id}/rendered.mp4",
        frame_map_path=f"C:/project/{clip.clip_id}/render_frame_map.json",
        media=_probe() if probe is None else probe,
        render_frame_map=clip.authoritative_frame_map,
    )


def _request(
    *,
    clips: tuple[ConcatClip, ...] | None = None,
    renders: dict[str, RenderCandidate] | None = None,
    confirmations: dict[str, SourceFallbackConfirmation] | None = None,
    fallbacks: dict[str, FallbackArtifact] | None = None,
) -> ConcatPreflightRequest:
    selected = _clips() if clips is None else clips
    candidates = (
        {clip.clip_id: _candidate(clip) for clip in selected}
        if renders is None
        else renders
    )
    return ConcatPreflightRequest(
        project_id="project-1",
        project_revision=7,
        clips_revision=9,
        source_frame_index=_source_index(),
        clips=selected,
        render_candidates=candidates,
        fallback_confirmations={} if confirmations is None else confirmations,
        fallback_artifacts={} if fallbacks is None else fallbacks,
        source_asset_fingerprint="f" * 64,
        project_media_spec_revision="media-spec-1",
        project_media_spec=_media_spec(),
        original_video_path="C:/project/original.mp4",
    )


def _confirmation(clip: ConcatClip, **changes: object) -> SourceFallbackConfirmation:
    values: dict[str, object] = {
        "project_id": "project-1",
        "clip_id": clip.clip_id,
        "project_revision": 7,
        "clips_revision": 9,
        "clip_analysis_revision": clip.analysis_revision,
        "interval_fingerprint": clip_interval_fingerprint(clip),
        "source_asset_fingerprint": "f" * 64,
        "project_media_spec_revision": "media-spec-1",
    }
    values.update(changes)
    return SourceFallbackConfirmation(**values)


def _fallback(clip: ConcatClip, **changes: object) -> FallbackArtifact:
    values: dict[str, object] = {
        "clip_id": clip.clip_id,
        "exact_validated": True,
        "interval_fingerprint": clip_interval_fingerprint(clip),
        "source_asset_fingerprint": "f" * 64,
        "project_media_spec_revision": "media-spec-1",
        "output_revision": f"fallback-{clip.clip_id}",
        "output_fingerprint": "c" * 64,
        "proof_fingerprint": "d" * 64,
        "video_sha256": "e" * 64,
        "frame_map_sha256": "f" * 64,
        "publication_operation_id": f"fallback-operation-{clip.clip_id}",
        "video_path": f"C:/project/fallback/{clip.clip_id}/rendered.mp4",
        "frame_map_path": (
            f"C:/project/fallback/{clip.clip_id}/render_frame_map.json"
        ),
        "media": _probe(width=640),
        "render_frame_map": clip.authoritative_frame_map,
    }
    values.update(changes)
    return FallbackArtifact(**values)


def test_exact_rendered_preflight_builds_source_order_plan_and_final_map() -> None:
    request = _request()

    report = preflight_concat(request)
    plan = build_concat_plan(request)
    final_map = build_final_frame_map(plan, request.source_frame_index)

    assert report.blockers == ()
    assert [entry.clip_id for entry in plan.entries] == ["clip-1", "clip-2"]
    assert [entry.selection for entry in plan.entries] == ["rendered", "rendered"]
    assert all(entry.ready and not entry.dependency_required for entry in plan.entries)
    assert plan.entries[0].input_output_revision == "render-clip-1"
    assert plan.entries[0].input_publication_operation_id == "operation-clip-1"
    assert plan.entries[0].input_video_sha256 == "c" * 64
    assert plan.entries[0].input_frame_map_sha256 == "d" * 64
    assert plan.audio_source == "C:/project/original.mp4"
    assert plan.source_asset_fingerprint == "f" * 64
    assert plan.to_dict()["source_asset_fingerprint"] == "f" * 64
    assert [item["source_pts"] for item in final_map["frames"]] == [
        1000, 1040, 1080, 1120
    ]
    json.dumps(plan.to_dict(), allow_nan=False)
    json.dumps(final_map, allow_nan=False)


def test_final_frame_map_builder_rejects_any_missing_or_duplicate_source_frame() -> None:
    request = _request()
    plan = build_concat_plan(request)
    corrupted_entry = replace(
        plan.entries[1], source_frames=plan.entries[1].source_frames[1:]
    )

    with pytest.raises(ValueError, match="complete authoritative source"):
        build_final_frame_map(
            replace(plan, entries=(plan.entries[0], corrupted_entry)),
            request.source_frame_index,
        )


def test_fallback_confirmation_json_roundtrip_is_strict_and_safe() -> None:
    confirmation = _confirmation(_clips()[1])

    assert SourceFallbackConfirmation.from_dict(
        confirmation.to_dict()
    ) == confirmation
    payload = confirmation.to_dict()
    payload["project_revision"] = True
    with pytest.raises(ValueError, match="project_revision"):
        SourceFallbackConfirmation.from_dict(payload)

    payload = confirmation.to_dict()
    payload["unexpected"] = "must-not-be-ignored"
    with pytest.raises(ValueError, match="unknown"):
        SourceFallbackConfirmation.from_dict(payload)


def test_canonical_snapshots_reject_non_text_revision_values() -> None:
    second = _clips()[1]

    with pytest.raises(ValueError, match="media spec revision"):
        _fallback(second, project_media_spec_revision=7)


@pytest.mark.parametrize("problem", ("duplicate", "missing", "source_order"))
def test_clip_render_order_must_be_complete_unique_and_match_source_order(
    problem: str,
) -> None:
    first, second = _clips()
    if problem == "duplicate":
        clips = (first, replace(second, render_order=0))
    elif problem == "missing":
        clips = (first, replace(second, render_order=2))
    else:
        clips = (replace(first, render_order=1), replace(second, render_order=0))

    with pytest.raises(ValueError, match="render_order|source PTS order"):
        preflight_concat(_request(clips=clips, renders={}))


def test_clip_authoritative_maps_must_exactly_partition_full_source_index() -> None:
    first, second = _clips()
    broken = dict(second.authoritative_frame_map)
    broken["frames"] = list(broken["frames"])[1:]
    clips = (first, replace(second, authoritative_frame_map=broken))

    with pytest.raises(ValueError, match="authoritative|complete source"):
        preflight_concat(_request(clips=clips, renders={}))


@pytest.mark.parametrize(
    "pollution",
    ("exact_validation", "current_input", "workflow", "project", "frame_map"),
)
def test_noncurrent_render_candidate_is_reported_as_unfinished_blocker(
    pollution: str,
) -> None:
    first, second = _clips()
    candidate = _candidate(second)
    if pollution == "exact_validation":
        candidate = replace(candidate, exact_validated=False)
    elif pollution == "current_input":
        candidate = replace(candidate, input_fingerprint="3" * 64)
    elif pollution == "workflow":
        candidate = replace(candidate, workflow="other_workflow")
    elif pollution == "project":
        candidate = replace(candidate, project_id="project-2")
    else:
        frame_map = dict(candidate.render_frame_map)
        frame_map["frames"] = [dict(item) for item in frame_map["frames"]]
        frame_map["frames"][0]["source_pts"] = 9999
        candidate = replace(candidate, render_frame_map=frame_map)

    report = preflight_concat(
        _request(renders={first.clip_id: _candidate(first), second.clip_id: candidate})
    )

    assert report.blockers == ("clip-2",)
    assert report.entries[1].status == "blocked"
    assert "render" in report.entries[1].reason
    with pytest.raises(ValueError, match="blocked"):
        build_concat_plan(
            _request(renders={first.clip_id: _candidate(first), second.clip_id: candidate})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_revision", 8),
        ("clips_revision", 10),
        ("clip_analysis_revision", "analysis-2"),
        ("interval_fingerprint", "0" * 64),
        ("source_asset_fingerprint", "e" * 64),
        ("project_media_spec_revision", "media-spec-2"),
    ),
)
def test_stale_source_fallback_confirmation_does_not_unblock_clip(
    field: str,
    value: object,
) -> None:
    first, second = _clips()
    confirmation = _confirmation(second, **{field: value})
    report = preflight_concat(
        _request(
            renders={first.clip_id: _candidate(first)},
            confirmations={second.clip_id: confirmation},
        )
    )

    assert report.blockers == ("clip-2",)
    assert "confirmation" in report.entries[1].reason


def test_valid_fallback_confirmation_creates_dependency_without_fake_paths() -> None:
    first, second = _clips()
    request = _request(
        renders={first.clip_id: _candidate(first)},
        confirmations={second.clip_id: _confirmation(second)},
    )

    report = preflight_concat(request)
    plan = build_concat_plan(request)
    entry = plan.entries[1]

    assert report.blockers == ()
    assert entry.selection == "source_fallback"
    assert entry.status == "dependency_required"
    assert entry.dependency_required is True and entry.ready is False
    assert entry.input_video_path is None and entry.input_frame_map_path is None


def test_ready_fallback_and_incompatible_render_record_normalization_truthfully() -> None:
    first, second = _clips()
    fallback = _fallback(second)
    request = _request(
        renders={first.clip_id: _candidate(first)},
        confirmations={second.clip_id: _confirmation(second)},
        fallbacks={second.clip_id: fallback},
    )

    plan = build_concat_plan(request)

    assert plan.entries[1].ready is True
    assert plan.entries[1].needs_normalize is True
    assert plan.entries[1].media_differences == ("width",)
    assert plan.entries[1].input_video_path == fallback.video_path
    assert plan.audio_source == request.original_video_path


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("exact_validated", False),
        ("interval_fingerprint", "0" * 64),
        ("source_asset_fingerprint", "e" * 64),
        ("project_media_spec_revision", "media-spec-2"),
    ),
)
def test_stale_or_unvalidated_fallback_artifact_is_a_blocker(
    field: str,
    value: object,
) -> None:
    first, second = _clips()
    request = _request(
        renders={first.clip_id: _candidate(first)},
        confirmations={second.clip_id: _confirmation(second)},
        fallbacks={second.clip_id: _fallback(second, **{field: value})},
    )

    report = preflight_concat(request)

    assert report.blockers == ("clip-2",)
    assert "fallback artifact" in report.entries[1].reason


def test_segment_frame_timing_must_match_authoritative_source_deltas() -> None:
    first, second = _clips()
    invalid = _candidate(second, probe=_probe(pts=(0, 50)))

    report = preflight_concat(
        _request(renders={first.clip_id: _candidate(first), second.clip_id: invalid})
    )

    assert report.blockers == ("clip-2",)
    assert "timing" in report.entries[1].reason


@pytest.mark.parametrize("packet_durations", ((40, 4000), (None, None)))
def test_packet_duration_is_diagnostic_when_pts_and_interval_are_authoritative(
    packet_durations: tuple[int | None, int | None],
) -> None:
    first, second = _clips()
    invalid = _candidate(second, probe=_probe())
    invalid_media = replace(
        invalid.media,
        video=replace(invalid.media.video, frame_duration_pts=packet_durations),
    )
    invalid = replace(invalid, media=invalid_media)

    report = preflight_concat(
        _request(renders={first.clip_id: _candidate(first), second.clip_id: invalid})
    )

    assert report.blockers == ()
    assert report.entries[1].status == "ready"


def test_nonzero_or_nonmonotonic_segment_pts_is_a_blocker_not_normalization() -> None:
    first, second = _clips()
    invalid = _candidate(second, probe=_probe(pts=(40, 80)))

    report = preflight_concat(
        _request(renders={first.clip_id: _candidate(first), second.clip_id: invalid})
    )

    assert report.blockers == ("clip-2",)
    assert "PTS" in report.entries[1].reason
