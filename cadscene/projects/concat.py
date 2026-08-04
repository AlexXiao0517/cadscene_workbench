from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from hashlib import sha256
import json
import math
from types import MappingProxyType
from typing import Any

from cadscene.video_analysis.pts import DecodedFrameIndex, DecodedFrameTimestamp

from .identifiers import is_safe_stable_id, validate_project_id
from .media import (
    ProbedMedia,
    ProjectMediaSpec,
    media_compatibility,
    validate_render_frame_map,
    validate_rendered_media,
)
from .source_fallback import select_source_interval_frames


@dataclass(frozen=True)
class ConcatClip:
    clip_id: str
    render_order: int
    analysis_revision: str
    resolved_workflow: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    authoritative_frame_map: Mapping[str, object]
    current_render_input_fingerprint: str

    def __post_init__(self) -> None:
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid concat clip_id")
        if type(self.render_order) is not int or self.render_order < 0:
            raise ValueError("render_order must be a non-negative integer")
        for name in ("analysis_revision", "resolved_workflow"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be explicit")
        if (
            type(self.source_start_pts) is not int
            or type(self.source_end_pts_exclusive) is not int
            or self.source_end_pts_exclusive <= self.source_start_pts
        ):
            raise ValueError("clip source interval must be increasing integer PTS")
        if not isinstance(self.source_time_base, Fraction) or self.source_time_base <= 0:
            raise ValueError("clip source time base must be a positive Fraction")
        _require_sha256(
            self.current_render_input_fingerprint,
            "current render input fingerprint",
        )
        if not isinstance(self.authoritative_frame_map, Mapping):
            raise TypeError("authoritative frame map must be a mapping")
        object.__setattr__(
            self,
            "authoritative_frame_map",
            _freeze_json_mapping(self.authoritative_frame_map),
        )


@dataclass(frozen=True)
class RenderCandidate:
    """Canonical snapshot built only after service job/owner exact validation."""

    project_id: str
    clip_id: str
    workflow: str
    exact_validated: bool
    input_fingerprint: str
    output_revision: str
    output_fingerprint: str
    proof_fingerprint: str
    video_sha256: str
    frame_map_sha256: str
    publication_operation_id: str
    video_path: str
    frame_map_path: str
    media: ProbedMedia
    render_frame_map: Mapping[str, object]

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid render candidate clip_id")
        if not isinstance(self.workflow, str) or not self.workflow.strip():
            raise ValueError("render candidate workflow must be explicit")
        if type(self.exact_validated) is not bool:
            raise ValueError("exact_validated must be boolean")
        _require_sha256(self.input_fingerprint, "render input fingerprint")
        _require_sha256(self.output_fingerprint, "render output fingerprint")
        _require_sha256(self.proof_fingerprint, "render proof fingerprint")
        _require_sha256(self.video_sha256, "render video fingerprint")
        _require_sha256(self.frame_map_sha256, "render frame map fingerprint")
        for name in ("output_revision", "publication_operation_id"):
            if not is_safe_stable_id(getattr(self, name)):
                raise ValueError(f"render candidate {name} is unsafe")
        for name in ("video_path", "frame_map_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"render candidate {name} must be explicit")
        if not isinstance(self.media, ProbedMedia):
            raise TypeError("render candidate media must be probed")
        if not isinstance(self.render_frame_map, Mapping):
            raise TypeError("render candidate frame map must be a mapping")
        object.__setattr__(
            self, "render_frame_map", _freeze_json_mapping(self.render_frame_map)
        )


@dataclass(frozen=True)
class FallbackArtifact:
    """Canonical source-fallback output snapshot; no manifest access occurs here."""

    clip_id: str
    exact_validated: bool
    interval_fingerprint: str
    source_asset_fingerprint: str
    project_media_spec_revision: str
    output_revision: str
    output_fingerprint: str
    proof_fingerprint: str
    video_sha256: str
    frame_map_sha256: str
    publication_operation_id: str
    video_path: str
    frame_map_path: str
    media: ProbedMedia
    render_frame_map: Mapping[str, object]

    def __post_init__(self) -> None:
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid fallback clip_id")
        if type(self.exact_validated) is not bool:
            raise ValueError("fallback exact_validated must be boolean")
        for name in ("video_path", "frame_map_path"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"fallback {name} must be explicit")
        if not isinstance(self.media, ProbedMedia):
            raise TypeError("fallback media must be probed")
        if not isinstance(self.render_frame_map, Mapping):
            raise TypeError("fallback frame map must be a mapping")
        for name in ("interval_fingerprint", "source_asset_fingerprint"):
            value = getattr(self, name)
            _require_sha256(value, name.replace("_", " "))
        if (
            not isinstance(self.project_media_spec_revision, str)
            or not self.project_media_spec_revision.strip()
            or self.project_media_spec_revision
            != self.project_media_spec_revision.strip()
        ):
            raise ValueError("fallback media spec revision must be explicit")
        for name in ("output_revision", "publication_operation_id"):
            if not is_safe_stable_id(getattr(self, name)):
                raise ValueError(f"fallback {name} is unsafe")
        _require_sha256(self.output_fingerprint, "fallback output fingerprint")
        _require_sha256(self.proof_fingerprint, "fallback proof fingerprint")
        _require_sha256(self.video_sha256, "fallback video fingerprint")
        _require_sha256(self.frame_map_sha256, "fallback frame map fingerprint")
        object.__setattr__(
            self, "render_frame_map", _freeze_json_mapping(self.render_frame_map)
        )


@dataclass(frozen=True)
class SourceFallbackConfirmation:
    project_id: str
    clip_id: str
    project_revision: int
    clips_revision: int
    clip_analysis_revision: str
    interval_fingerprint: str
    source_asset_fingerprint: str
    project_media_spec_revision: str

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid fallback confirmation clip_id")
        for name in ("project_revision", "clips_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("clip_analysis_revision", "project_media_spec_revision"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{name} must be explicit")
        _require_sha256(self.interval_fingerprint, "interval fingerprint")
        _require_sha256(self.source_asset_fingerprint, "source asset fingerprint")

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "clip_id": self.clip_id,
            "project_revision": self.project_revision,
            "clips_revision": self.clips_revision,
            "clip_analysis_revision": self.clip_analysis_revision,
            "interval_fingerprint": self.interval_fingerprint,
            "source_asset_fingerprint": self.source_asset_fingerprint,
            "project_media_spec_revision": self.project_media_spec_revision,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourceFallbackConfirmation:
        expected_keys = {
            "project_id",
            "clip_id",
            "project_revision",
            "clips_revision",
            "clip_analysis_revision",
            "interval_fingerprint",
            "source_asset_fingerprint",
            "project_media_spec_revision",
        }
        unknown_keys = set(value) - expected_keys
        if unknown_keys:
            raise ValueError(
                "fallback confirmation contains unknown fields: "
                + ", ".join(sorted(unknown_keys))
            )
        return cls(
            project_id=_strict_text(value.get("project_id"), "project_id"),
            clip_id=_strict_text(value.get("clip_id"), "clip_id"),
            project_revision=_strict_integer(
                value.get("project_revision"), "project_revision"
            ),
            clips_revision=_strict_integer(
                value.get("clips_revision"), "clips_revision"
            ),
            clip_analysis_revision=_strict_text(
                value.get("clip_analysis_revision"), "clip_analysis_revision"
            ),
            interval_fingerprint=_strict_text(
                value.get("interval_fingerprint"), "interval_fingerprint"
            ),
            source_asset_fingerprint=_strict_text(
                value.get("source_asset_fingerprint"), "source_asset_fingerprint"
            ),
            project_media_spec_revision=_strict_text(
                value.get("project_media_spec_revision"),
                "project_media_spec_revision",
            ),
        )


@dataclass(frozen=True)
class ConcatPreflightRequest:
    project_id: str
    project_revision: int
    clips_revision: int
    source_frame_index: DecodedFrameIndex
    clips: tuple[ConcatClip, ...]
    render_candidates: Mapping[str, RenderCandidate]
    fallback_confirmations: Mapping[str, SourceFallbackConfirmation]
    fallback_artifacts: Mapping[str, FallbackArtifact]
    source_asset_fingerprint: str
    project_media_spec_revision: str
    project_media_spec: ProjectMediaSpec
    original_video_path: str

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        for name in ("project_revision", "clips_revision"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if not isinstance(self.source_frame_index, DecodedFrameIndex):
            raise TypeError("source_frame_index must be a DecodedFrameIndex")
        if (
            not isinstance(self.clips, Sequence)
            or isinstance(self.clips, (str, bytes, bytearray))
            or not self.clips
            or any(not isinstance(clip, ConcatClip) for clip in self.clips)
        ):
            raise ValueError("concat clips must be a non-empty sequence")
        object.__setattr__(self, "clips", tuple(self.clips))
        for name, item_type in (
            ("render_candidates", RenderCandidate),
            ("fallback_confirmations", SourceFallbackConfirmation),
            ("fallback_artifacts", FallbackArtifact),
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or any(
                not isinstance(key, str) or not isinstance(item, item_type)
                for key, item in value.items()
            ):
                raise TypeError(f"{name} must map clip IDs to {item_type.__name__}")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        _require_sha256(self.source_asset_fingerprint, "source asset fingerprint")
        if (
            not isinstance(self.project_media_spec_revision, str)
            or not self.project_media_spec_revision.strip()
            or self.project_media_spec_revision
            != self.project_media_spec_revision.strip()
        ):
            raise ValueError("project media spec revision must be explicit")
        if not isinstance(self.project_media_spec, ProjectMediaSpec):
            raise TypeError("project_media_spec must be a ProjectMediaSpec")
        if not isinstance(self.original_video_path, str) or not self.original_video_path.strip():
            raise ValueError("original video path must be explicit")


@dataclass(frozen=True)
class ConcatPlanEntry:
    clip_id: str
    render_order: int
    selection: str | None
    status: str
    reason: str
    source_start_pts: int
    source_end_pts_exclusive: int
    source_time_base: Fraction
    source_frames: tuple[DecodedFrameTimestamp, ...]
    input_video_path: str | None
    input_frame_map_path: str | None
    input_output_revision: str | None
    input_output_fingerprint: str | None
    input_proof_fingerprint: str | None
    input_video_sha256: str | None
    input_frame_map_sha256: str | None
    input_publication_operation_id: str | None
    ready: bool
    dependency_required: bool
    needs_normalize: bool | None
    media_differences: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("invalid concat plan clip_id")
        if type(self.render_order) is not int or self.render_order < 0:
            raise ValueError("concat plan render_order must be non-negative")
        if (
            type(self.source_start_pts) is not int
            or type(self.source_end_pts_exclusive) is not int
            or self.source_end_pts_exclusive <= self.source_start_pts
        ):
            raise ValueError("concat plan source interval must be increasing")
        if not isinstance(self.source_time_base, Fraction) or self.source_time_base <= 0:
            raise ValueError("concat plan source time base must be positive")
        if (
            not isinstance(self.source_frames, Sequence)
            or isinstance(self.source_frames, (str, bytes, bytearray))
            or not self.source_frames
            or any(
                not isinstance(frame, DecodedFrameTimestamp)
                for frame in self.source_frames
            )
        ):
            raise ValueError("concat plan source frames must be explicit")
        object.__setattr__(self, "source_frames", tuple(self.source_frames))
        if type(self.ready) is not bool or type(self.dependency_required) is not bool:
            raise ValueError("concat plan readiness must be boolean")
        if self.needs_normalize is not None and type(self.needs_normalize) is not bool:
            raise ValueError("concat plan normalization state must be boolean or null")
        if (
            not isinstance(self.media_differences, Sequence)
            or isinstance(self.media_differences, (str, bytes, bytearray))
            or any(not isinstance(item, str) for item in self.media_differences)
        ):
            raise ValueError("concat media differences must be strings")
        object.__setattr__(self, "media_differences", tuple(self.media_differences))

    def to_dict(self) -> dict[str, object]:
        return {
            "clip_id": self.clip_id,
            "render_order": self.render_order,
            "selection": self.selection,
            "status": self.status,
            "reason": self.reason,
            "source_start_pts": self.source_start_pts,
            "source_end_pts_exclusive": self.source_end_pts_exclusive,
            "source_time_base": {
                "numerator": self.source_time_base.numerator,
                "denominator": self.source_time_base.denominator,
            },
            "source_frames": [
                {"ordinal": frame.ordinal, "pts": frame.pts}
                for frame in self.source_frames
            ],
            "input_video_path": self.input_video_path,
            "input_frame_map_path": self.input_frame_map_path,
            "input_output_revision": self.input_output_revision,
            "input_output_fingerprint": self.input_output_fingerprint,
            "input_proof_fingerprint": self.input_proof_fingerprint,
            "input_video_sha256": self.input_video_sha256,
            "input_frame_map_sha256": self.input_frame_map_sha256,
            "input_publication_operation_id": self.input_publication_operation_id,
            "ready": self.ready,
            "dependency_required": self.dependency_required,
            "needs_normalize": self.needs_normalize,
            "media_differences": list(self.media_differences),
        }


@dataclass(frozen=True)
class ConcatPreflight:
    entries: tuple[ConcatPlanEntry, ...]
    blockers: tuple[str, ...]

    @property
    def can_build_plan(self) -> bool:
        return not self.blockers

    def to_dict(self) -> dict[str, object]:
        return {
            "entries": [entry.to_dict() for entry in self.entries],
            "blockers": list(self.blockers),
            "can_build_plan": self.can_build_plan,
        }


@dataclass(frozen=True)
class ConcatPlan:
    project_id: str
    project_revision: int
    clips_revision: int
    project_media_spec_revision: str
    source_asset_fingerprint: str
    entries: tuple[ConcatPlanEntry, ...]
    audio_source: str

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        for name in ("project_revision", "clips_revision"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            not isinstance(self.project_media_spec_revision, str)
            or not self.project_media_spec_revision.strip()
            or self.project_media_spec_revision != self.project_media_spec_revision.strip()
        ):
            raise ValueError("project media spec revision must be explicit")
        _require_sha256(self.source_asset_fingerprint, "source asset fingerprint")
        if (
            not isinstance(self.entries, Sequence)
            or isinstance(self.entries, (str, bytes, bytearray))
            or not self.entries
            or any(not isinstance(entry, ConcatPlanEntry) for entry in self.entries)
        ):
            raise ValueError("concat plan entries must be explicit")
        object.__setattr__(self, "entries", tuple(self.entries))
        if (
            not isinstance(self.audio_source, str)
            or not self.audio_source.strip()
            or self.audio_source != self.audio_source.strip()
        ):
            raise ValueError("concat audio source must be explicit")

    def to_dict(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "project_revision": self.project_revision,
            "clips_revision": self.clips_revision,
            "project_media_spec_revision": self.project_media_spec_revision,
            "source_asset_fingerprint": self.source_asset_fingerprint,
            "entries": [entry.to_dict() for entry in self.entries],
            "audio_source": self.audio_source,
            "audio_policy": "original_video",
        }


def clip_interval_fingerprint(clip: ConcatClip) -> str:
    frames = _clip_authoritative_entries(clip)
    payload = {
        "clip_id": clip.clip_id,
        "analysis_revision": clip.analysis_revision,
        "source_start_pts": clip.source_start_pts,
        "source_end_pts_exclusive": clip.source_end_pts_exclusive,
        "source_time_base": {
            "numerator": clip.source_time_base.numerator,
            "denominator": clip.source_time_base.denominator,
        },
        "frames": [
            {"ordinal": frame.ordinal, "pts": frame.pts} for frame in frames
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def preflight_concat(request: ConcatPreflightRequest) -> ConcatPreflight:
    ordered = _validate_source_partition(request)
    known_ids = {clip.clip_id for clip, _frames in ordered}
    for name, values in (
        ("render candidates", request.render_candidates),
        ("fallback confirmations", request.fallback_confirmations),
        ("fallback artifacts", request.fallback_artifacts),
    ):
        extras = set(values) - known_ids
        if extras:
            raise ValueError(f"{name} contain unknown clip IDs: {sorted(extras)}")

    entries: list[ConcatPlanEntry] = []
    blockers: list[str] = []
    for clip, source_frames in ordered:
        candidate = request.render_candidates.get(clip.clip_id)
        render_error: str | None = None
        if candidate is not None:
            try:
                entries.append(
                    _ready_entry(
                        request,
                        clip,
                        source_frames,
                        selection="rendered",
                        media=candidate.media,
                        frame_map=candidate.render_frame_map,
                        video_path=candidate.video_path,
                        frame_map_path=candidate.frame_map_path,
                        output_revision=candidate.output_revision,
                        output_fingerprint=candidate.output_fingerprint,
                        proof_fingerprint=candidate.proof_fingerprint,
                        video_sha256=candidate.video_sha256,
                        frame_map_sha256=candidate.frame_map_sha256,
                        publication_operation_id=candidate.publication_operation_id,
                        candidate=candidate,
                    )
                )
                continue
            except (ValueError, TypeError) as exc:
                render_error = f"render candidate invalid: {exc}"

        confirmation = request.fallback_confirmations.get(clip.clip_id)
        confirmation_error = _fallback_confirmation_error(
            request, clip, confirmation
        )
        if confirmation_error is None:
            fallback = request.fallback_artifacts.get(clip.clip_id)
            if fallback is None:
                entries.append(
                    _base_entry(
                        clip,
                        source_frames,
                        selection="source_fallback",
                        status="dependency_required",
                        reason="source fallback render dependency is required",
                        ready=False,
                        dependency_required=True,
                    )
                )
                continue
            try:
                _validate_fallback_binding(request, clip, fallback)
                entries.append(
                    _ready_entry(
                        request,
                        clip,
                        source_frames,
                        selection="source_fallback",
                        media=fallback.media,
                        frame_map=fallback.render_frame_map,
                        video_path=fallback.video_path,
                        frame_map_path=fallback.frame_map_path,
                        output_revision=fallback.output_revision,
                        output_fingerprint=fallback.output_fingerprint,
                        proof_fingerprint=fallback.proof_fingerprint,
                        video_sha256=fallback.video_sha256,
                        frame_map_sha256=fallback.frame_map_sha256,
                        publication_operation_id=fallback.publication_operation_id,
                    )
                )
                continue
            except (ValueError, TypeError) as exc:
                render_error = f"source fallback artifact invalid: {exc}"
        failures = tuple(
            item for item in (render_error, confirmation_error) if item is not None
        )
        reason = "; ".join(failures) if failures else "render is unfinished"
        entries.append(
            _base_entry(
                clip,
                source_frames,
                selection=None,
                status="blocked",
                reason=reason,
                ready=False,
                dependency_required=False,
            )
        )
        blockers.append(clip.clip_id)
    return ConcatPreflight(entries=tuple(entries), blockers=tuple(blockers))


def build_concat_plan(request: ConcatPreflightRequest) -> ConcatPlan:
    report = preflight_concat(request)
    if report.blockers:
        raise ValueError(
            "concat plan is blocked by clips: " + ", ".join(report.blockers)
        )
    return ConcatPlan(
        project_id=request.project_id,
        project_revision=request.project_revision,
        clips_revision=request.clips_revision,
        project_media_spec_revision=request.project_media_spec_revision,
        source_asset_fingerprint=request.source_asset_fingerprint,
        entries=report.entries,
        audio_source=request.original_video_path,
    )


def build_final_frame_map(
    plan: ConcatPlan, source_frame_index: DecodedFrameIndex
) -> dict[str, object]:
    flattened = tuple(frame for entry in plan.entries for frame in entry.source_frames)
    expected = tuple(
        (frame.ordinal, frame.pts) for frame in source_frame_index.frames
    )
    actual = tuple((frame.ordinal, frame.pts) for frame in flattened)
    if actual != expected:
        raise ValueError(
            "final frame map differs from the complete authoritative source sequence"
        )
    return {
        "schema_version": 1,
        "source_time_base": {
            "numerator": source_frame_index.time_base.numerator,
            "denominator": source_frame_index.time_base.denominator,
        },
        "frames": [
            {
                "output_frame_ordinal": output,
                "source_decoded_frame_ordinal": frame.ordinal,
                "source_pts": frame.pts,
            }
            for output, frame in enumerate(flattened)
        ],
    }


def _validate_source_partition(
    request: ConcatPreflightRequest,
) -> tuple[tuple[ConcatClip, tuple[DecodedFrameTimestamp, ...]], ...]:
    orders = [clip.render_order for clip in request.clips]
    if sorted(orders) != list(range(len(request.clips))):
        raise ValueError("render_order must be unique and complete from zero")
    ordered_clips = tuple(sorted(request.clips, key=lambda clip: clip.render_order))
    if list(ordered_clips) != sorted(
        ordered_clips, key=lambda clip: clip.source_start_pts
    ):
        raise ValueError("render_order must match source PTS order")
    if ordered_clips[0].source_start_pts != request.source_frame_index.source_start_pts:
        raise ValueError("clip intervals omit the authoritative source start")
    if (
        ordered_clips[-1].source_end_pts_exclusive
        != request.source_frame_index.source_end_pts_exclusive
    ):
        raise ValueError("clip intervals omit the authoritative exclusive source end")
    for previous, current in zip(ordered_clips, ordered_clips[1:]):
        if previous.source_end_pts_exclusive != current.source_start_pts:
            raise ValueError("clip source intervals must be contiguous and half-open")

    ordered: list[tuple[ConcatClip, tuple[DecodedFrameTimestamp, ...]]] = []
    flattened: list[tuple[int, int]] = []
    for clip in ordered_clips:
        if clip.source_time_base != request.source_frame_index.time_base:
            raise ValueError("clip source time base differs from source index")
        source_frames = select_source_interval_frames(
            request.source_frame_index,
            source_start_pts=clip.source_start_pts,
            source_end_pts_exclusive=clip.source_end_pts_exclusive,
            source_time_base=clip.source_time_base,
        )
        try:
            validate_render_frame_map(
                _thaw_json(clip.authoritative_frame_map),
                rendered_frame_count=len(source_frames),
                expected_source_frames=source_frames,
                expected_source_time_base=clip.source_time_base,
            )
        except ValueError as exc:
            raise ValueError(
                f"clip authoritative frame map is invalid: {exc}"
            ) from exc
        flattened.extend((frame.ordinal, frame.pts) for frame in source_frames)
        ordered.append((clip, source_frames))
    expected = [
        (frame.ordinal, frame.pts) for frame in request.source_frame_index.frames
    ]
    if flattened != expected:
        raise ValueError(
            "clip authoritative maps do not cover the complete source frame index"
        )
    return tuple(ordered)


def _ready_entry(
    request: ConcatPreflightRequest,
    clip: ConcatClip,
    source_frames: tuple[DecodedFrameTimestamp, ...],
    *,
    selection: str,
    media: ProbedMedia,
    frame_map: Mapping[str, object],
    video_path: str,
    frame_map_path: str,
    output_revision: str,
    output_fingerprint: str,
    proof_fingerprint: str,
    video_sha256: str,
    frame_map_sha256: str,
    publication_operation_id: str,
    candidate: RenderCandidate | None = None,
) -> ConcatPlanEntry:
    if candidate is not None:
        _validate_render_candidate(request, clip, candidate)
    if not video_path or not frame_map_path:
        raise ValueError("render input paths are incomplete")
    validate_rendered_media(
        media,
        _thaw_json(frame_map),
        expected_source_frames=source_frames,
        expected_source_time_base=clip.source_time_base,
    )
    _validate_frame_timing(
        media,
        source_frames,
        clip.source_time_base,
    )
    compatibility = media_compatibility(media.video, request.project_media_spec)
    return _base_entry(
        clip,
        source_frames,
        selection=selection,
        status="ready",
        reason="validated current segment",
        input_video_path=video_path,
        input_frame_map_path=frame_map_path,
        input_output_revision=output_revision,
        input_output_fingerprint=output_fingerprint,
        input_proof_fingerprint=proof_fingerprint,
        input_video_sha256=video_sha256,
        input_frame_map_sha256=frame_map_sha256,
        input_publication_operation_id=publication_operation_id,
        ready=True,
        dependency_required=False,
        needs_normalize=not compatibility.compatible,
        media_differences=compatibility.differences,
    )


def _validate_render_candidate(
    request: ConcatPreflightRequest,
    clip: ConcatClip,
    candidate: RenderCandidate,
) -> None:
    if (
        not candidate.exact_validated
        or candidate.project_id != request.project_id
        or candidate.clip_id != clip.clip_id
        or candidate.workflow != clip.resolved_workflow
        or candidate.input_fingerprint != clip.current_render_input_fingerprint
    ):
        raise ValueError("render candidate is not an exact current validated success")


def _fallback_confirmation_error(
    request: ConcatPreflightRequest,
    clip: ConcatClip,
    confirmation: SourceFallbackConfirmation | None,
) -> str | None:
    if confirmation is None:
        return "source fallback confirmation is required"
    expected = {
        "project_id": request.project_id,
        "clip_id": clip.clip_id,
        "project_revision": request.project_revision,
        "clips_revision": request.clips_revision,
        "clip_analysis_revision": clip.analysis_revision,
        "interval_fingerprint": clip_interval_fingerprint(clip),
        "source_asset_fingerprint": request.source_asset_fingerprint,
        "project_media_spec_revision": request.project_media_spec_revision,
    }
    actual = confirmation.to_dict()
    if actual != expected:
        changed = [key for key, value in expected.items() if actual.get(key) != value]
        return "source fallback confirmation is stale: " + ", ".join(changed)
    return None


def _validate_fallback_binding(
    request: ConcatPreflightRequest,
    clip: ConcatClip,
    artifact: FallbackArtifact,
) -> None:
    if not artifact.exact_validated:
        raise ValueError("fallback artifact is not an exact validated output")
    if artifact.clip_id != clip.clip_id:
        raise ValueError("fallback artifact clip identity differs")
    for actual, expected, label in (
        (
            artifact.interval_fingerprint,
            clip_interval_fingerprint(clip),
            "interval fingerprint",
        ),
        (
            artifact.source_asset_fingerprint,
            request.source_asset_fingerprint,
            "source asset fingerprint",
        ),
        (
            artifact.project_media_spec_revision,
            request.project_media_spec_revision,
            "media spec revision",
        ),
    ):
        if actual != expected:
            raise ValueError(f"fallback artifact {label} is stale")


def _validate_frame_timing(
    media: ProbedMedia,
    source_frames: tuple[DecodedFrameTimestamp, ...],
    source_time_base: Fraction,
) -> None:
    output_deltas = tuple(
        Fraction(current - previous) * media.video.time_base
        for previous, current in zip(
            media.video.frame_pts, media.video.frame_pts[1:]
        )
    )
    source_deltas = tuple(
        Fraction(current.pts - previous.pts) * source_time_base
        for previous, current in zip(source_frames, source_frames[1:])
    )
    if output_deltas != source_deltas:
        raise ValueError("rendered frame timing differs from authoritative source")


def _base_entry(
    clip: ConcatClip,
    source_frames: tuple[DecodedFrameTimestamp, ...],
    *,
    selection: str | None,
    status: str,
    reason: str,
    ready: bool,
    dependency_required: bool,
    input_video_path: str | None = None,
    input_frame_map_path: str | None = None,
    input_output_revision: str | None = None,
    input_output_fingerprint: str | None = None,
    input_proof_fingerprint: str | None = None,
    input_video_sha256: str | None = None,
    input_frame_map_sha256: str | None = None,
    input_publication_operation_id: str | None = None,
    needs_normalize: bool | None = None,
    media_differences: tuple[str, ...] = (),
) -> ConcatPlanEntry:
    return ConcatPlanEntry(
        clip_id=clip.clip_id,
        render_order=clip.render_order,
        selection=selection,
        status=status,
        reason=reason,
        source_start_pts=clip.source_start_pts,
        source_end_pts_exclusive=clip.source_end_pts_exclusive,
        source_time_base=clip.source_time_base,
        source_frames=source_frames,
        input_video_path=input_video_path,
        input_frame_map_path=input_frame_map_path,
        input_output_revision=input_output_revision,
        input_output_fingerprint=input_output_fingerprint,
        input_proof_fingerprint=input_proof_fingerprint,
        input_video_sha256=input_video_sha256,
        input_frame_map_sha256=input_frame_map_sha256,
        input_publication_operation_id=input_publication_operation_id,
        ready=ready,
        dependency_required=dependency_required,
        needs_normalize=needs_normalize,
        media_differences=media_differences,
    )


def _clip_authoritative_entries(
    clip: ConcatClip,
) -> tuple[DecodedFrameTimestamp, ...]:
    frames = clip.authoritative_frame_map.get("frames")
    if not isinstance(frames, Sequence) or isinstance(frames, (str, bytes, bytearray)):
        raise ValueError("clip authoritative frame map has no frames")
    result: list[DecodedFrameTimestamp] = []
    for entry in frames:
        if not isinstance(entry, Mapping):
            raise ValueError("clip authoritative frame map entry is invalid")
        ordinal = entry.get("source_decoded_frame_ordinal")
        pts = entry.get("source_pts")
        if type(ordinal) is not int or type(pts) is not int:
            raise ValueError("clip authoritative frame identity must be integer")
        result.append(DecodedFrameTimestamp(ordinal, pts, None, "pts"))
    return tuple(result)


def _require_sha256(value: object, field_name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{field_name} must be lowercase SHA-256")
    return value


def _strict_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} must be explicit")
    return value


def _strict_integer(value: object, field_name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")
    return value


def _freeze_json_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TypeError("mapping keys must be strings")
        frozen[key] = _freeze_json_value(item)
    return MappingProxyType(frozen)


def _freeze_json_value(value: object) -> object:
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("mapping numbers must be finite")
        return value
    if isinstance(value, Mapping):
        return _freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    raise TypeError("mapping values must be JSON-compatible")


def _thaw_json(value: object) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value
