from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, ClassVar, Mapping

from cadscene.projects.identifiers import is_safe_stable_id
from cadscene.projects.models import (
    SCHEMA_VERSION,
    ManifestHeader,
    StateReference,
    _header_from_dict,
)


_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}(?:[0-9A-Fa-f]{2})?$")
_ANCHOR_TYPES = frozenset({"cad_anchor", "video_track"})


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    return result


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


@dataclass(frozen=True)
class SourcePtsRange:
    start_pts: int
    end_pts_exclusive: int
    time_base_numerator: int
    time_base_denominator: int

    def __post_init__(self) -> None:
        _integer(self.start_pts, "start_pts")
        _integer(self.end_pts_exclusive, "end_pts_exclusive")
        _integer(self.time_base_numerator, "time_base numerator")
        _integer(self.time_base_denominator, "time_base denominator")
        if self.end_pts_exclusive <= self.start_pts:
            raise ValueError("source PTS range must be non-empty and half-open")
        if self.time_base_numerator <= 0 or self.time_base_denominator <= 0:
            raise ValueError("source time_base must be positive")

    def contains(self, source_pts: int) -> bool:
        return (
            self.start_pts
            <= _integer(source_pts, "source_pts")
            < self.end_pts_exclusive
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "start_pts": self.start_pts,
            "end_pts_exclusive": self.end_pts_exclusive,
            "time_base": {
                "numerator": self.time_base_numerator,
                "denominator": self.time_base_denominator,
            },
            "semantics": "half_open",
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> SourcePtsRange:
        if value.get("semantics", "half_open") != "half_open":
            raise ValueError("annotation source PTS range must be half_open")
        time_base = value.get("time_base")
        if not isinstance(time_base, Mapping):
            raise TypeError("source PTS range requires an exact time_base")
        return cls(
            start_pts=_integer(value.get("start_pts"), "start_pts"),
            end_pts_exclusive=_integer(
                value.get("end_pts_exclusive"), "end_pts_exclusive"
            ),
            time_base_numerator=_integer(
                time_base.get("numerator"), "time_base numerator"
            ),
            time_base_denominator=_integer(
                time_base.get("denominator"), "time_base denominator"
            ),
        )


@dataclass(frozen=True)
class AnnotationStyle:
    font_size_px: int = 28
    text_color: str = "#FFFFFF"
    title_color: str = "#69D2FFFF"
    background_color: str = "#000000B3"
    background_opacity: float = 0.7
    border_color: str = "#FFFFFFCC"
    font_family: str = "sans-serif"
    font_weight: int = 600

    def __post_init__(self) -> None:
        if isinstance(self.font_size_px, bool) or not isinstance(
            self.font_size_px, int
        ):
            raise TypeError("font_size_px must be an integer")
        if not 8 <= self.font_size_px <= 128:
            raise ValueError("font_size_px must be between 8 and 128")
        for name in (
            "text_color",
            "title_color",
            "background_color",
            "border_color",
        ):
            if not _COLOR.fullmatch(getattr(self, name)):
                raise ValueError(f"{name} must be a #RRGGBB or #RRGGBBAA color")
        opacity = _finite(self.background_opacity, "background_opacity")
        if not 0.0 <= opacity <= 1.0:
            raise ValueError("background_opacity must be between 0 and 1")
        if not self.font_family.strip() or len(self.font_family) > 80:
            raise ValueError("font_family must be explicit and bounded")
        if self.font_weight not in {400, 500, 600, 700}:
            raise ValueError("font_weight is unsupported")

    def to_dict(self) -> dict[str, object]:
        return {
            "font_size_px": self.font_size_px,
            "text_color": self.text_color,
            "title_color": self.title_color,
            "background_color": self.background_color,
            "background_opacity": self.background_opacity,
            "border_color": self.border_color,
            "font_family": self.font_family,
            "font_weight": self.font_weight,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> AnnotationStyle:
        values = dict(value or {})
        return cls(
            font_size_px=int(values.get("font_size_px", 28)),
            text_color=str(values.get("text_color", "#FFFFFF")),
            title_color=str(values.get("title_color", "#69D2FFFF")),
            background_color=str(values.get("background_color", "#000000B3")),
            background_opacity=float(values.get("background_opacity", 0.7)),
            border_color=str(values.get("border_color", "#FFFFFFCC")),
            font_family=str(values.get("font_family", "sans-serif")),
            font_weight=int(values.get("font_weight", 600)),
        )


@dataclass(frozen=True)
class AnnotationContent:
    title: str = ""
    body: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or len(self.title) > 160:
            raise ValueError(
                "annotation title must be a string of at most 160 characters"
            )
        if not isinstance(self.body, str) or len(self.body) > 2000:
            raise ValueError(
                "annotation body must be a string of at most 2000 characters"
            )

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "body": self.body}

    @classmethod
    def from_dict(
        cls, value: Mapping[str, object] | None, *, legacy_text: str = ""
    ) -> AnnotationContent:
        if value is None:
            return cls(body=legacy_text)
        return cls(
            title=str(value.get("title", "")),
            body=str(value.get("body", legacy_text)),
        )


@dataclass(frozen=True)
class AnnotationPanel:
    width_px: int = 320
    padding_px: int = 16
    safe_margin_px: int = 20
    border_radius_px: int = 6
    shadow: bool = True

    def __post_init__(self) -> None:
        if not 160 <= _integer(self.width_px, "panel width_px") <= 720:
            raise ValueError("panel width_px must be between 160 and 720")
        if not 4 <= _integer(self.padding_px, "panel padding_px") <= 64:
            raise ValueError("panel padding_px must be between 4 and 64")
        if not 0 <= _integer(self.safe_margin_px, "panel safe_margin_px") <= 160:
            raise ValueError("panel safe_margin_px must be between 0 and 160")
        if not 0 <= _integer(self.border_radius_px, "panel border_radius_px") <= 48:
            raise ValueError("panel border_radius_px must be between 0 and 48")
        if not isinstance(self.shadow, bool):
            raise TypeError("panel shadow must be boolean")

    def to_dict(self) -> dict[str, object]:
        return {
            "width_px": self.width_px,
            "padding_px": self.padding_px,
            "safe_margin_px": self.safe_margin_px,
            "border_radius_px": self.border_radius_px,
            "shadow": self.shadow,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> AnnotationPanel:
        values = dict(value or {})
        return cls(
            width_px=int(values.get("width_px", 320)),
            padding_px=int(values.get("padding_px", 16)),
            safe_margin_px=int(values.get("safe_margin_px", 20)),
            border_radius_px=int(values.get("border_radius_px", 6)),
            shadow=bool(values.get("shadow", True)),
        )


@dataclass(frozen=True)
class AnnotationLeader:
    line_width_px: int = 2
    anchor_radius_px: int = 6
    elbow_length_px: int = 24
    anchor_shape: str = "circle"

    def __post_init__(self) -> None:
        if not 1 <= _integer(self.line_width_px, "leader line_width_px") <= 12:
            raise ValueError("leader line_width_px must be between 1 and 12")
        if not 2 <= _integer(self.anchor_radius_px, "leader anchor_radius_px") <= 32:
            raise ValueError("leader anchor_radius_px must be between 2 and 32")
        if not 0 <= _integer(self.elbow_length_px, "leader elbow_length_px") <= 160:
            raise ValueError("leader elbow_length_px must be between 0 and 160")
        if self.anchor_shape not in {"circle", "crosshair"}:
            raise ValueError("leader anchor_shape must be circle or crosshair")

    def to_dict(self) -> dict[str, object]:
        return {
            "line_width_px": self.line_width_px,
            "anchor_radius_px": self.anchor_radius_px,
            "elbow_length_px": self.elbow_length_px,
            "anchor_shape": self.anchor_shape,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> AnnotationLeader:
        values = dict(value or {})
        return cls(
            line_width_px=int(values.get("line_width_px", 2)),
            anchor_radius_px=int(values.get("anchor_radius_px", 6)),
            elbow_length_px=int(values.get("elbow_length_px", 24)),
            anchor_shape=str(values.get("anchor_shape", "circle")),
        )


@dataclass(frozen=True)
class VisibilityPolicy:
    hide_behind_camera: bool = True
    hide_outside_viewport: bool = True
    min_tracking_confidence: float = 0.5

    def __post_init__(self) -> None:
        if not isinstance(self.hide_behind_camera, bool):
            raise TypeError("hide_behind_camera must be boolean")
        if not isinstance(self.hide_outside_viewport, bool):
            raise TypeError("hide_outside_viewport must be boolean")
        confidence = _finite(self.min_tracking_confidence, "min_tracking_confidence")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("min_tracking_confidence must be between 0 and 1")

    def to_dict(self) -> dict[str, object]:
        return {
            "hide_behind_camera": self.hide_behind_camera,
            "hide_outside_viewport": self.hide_outside_viewport,
            "min_tracking_confidence": self.min_tracking_confidence,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object] | None) -> VisibilityPolicy:
        values = dict(value or {})
        return cls(
            hide_behind_camera=bool(values.get("hide_behind_camera", True)),
            hide_outside_viewport=bool(values.get("hide_outside_viewport", True)),
            min_tracking_confidence=float(values.get("min_tracking_confidence", 0.5)),
        )


def _validate_anchor(
    anchor_type: str,
    anchor: Mapping[str, Any],
    source_pts_range: SourcePtsRange,
) -> None:
    if anchor_type == "cad_anchor":
        world = anchor.get("cad_world_xyz")
        if not isinstance(world, (list, tuple)) or len(world) != 3:
            raise ValueError("cad_anchor requires cad_world_xyz")
        for index, coordinate in enumerate(world):
            _finite(coordinate, f"cad_world_xyz[{index}]")
        reference = anchor.get("cad_entity_reference")
        if reference is not None and not isinstance(reference, Mapping):
            raise TypeError("cad_entity_reference must be an object")
        return
    initialization = anchor.get("initialization")
    if not isinstance(initialization, Mapping):
        raise ValueError("video_track requires initialization")
    source_pts = _integer(initialization.get("source_pts"), "initialization source_pts")
    if not source_pts_range.contains(source_pts):
        raise ValueError(
            "video_track initialization must be inside the annotation PTS range"
        )
    bbox = initialization.get("bbox")
    anchor_xy = initialization.get("anchor_xy")
    if bbox is None and anchor_xy is None:
        raise ValueError("video_track initialization requires bbox or anchor_xy")
    if bbox is not None:
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError("video_track bbox must be [x, y, width, height]")
        values = tuple(_finite(item, "bbox value") for item in bbox)
        if values[2] <= 0 or values[3] <= 0:
            raise ValueError("video_track bbox must have positive size")
    if anchor_xy is not None:
        if not isinstance(anchor_xy, (list, tuple)) or len(anchor_xy) != 2:
            raise ValueError("video_track anchor_xy must be [x, y]")
        tuple(_finite(item, "anchor_xy value") for item in anchor_xy)


@dataclass(frozen=True)
class Annotation:
    annotation_id: str
    clip_id: str
    anchor_type: str
    text: str
    anchor: Mapping[str, Any]
    style: AnnotationStyle
    source_pts_range: SourcePtsRange
    screen_offset: tuple[float, float]
    visibility_policy: VisibilityPolicy
    user_visible: bool
    annotation_revision: int
    active_tracking_revision: str | None
    created_at: str
    updated_at: str
    created_operation_id: str
    updated_operation_id: str
    content: AnnotationContent = field(default_factory=AnnotationContent)
    panel: AnnotationPanel = field(default_factory=AnnotationPanel)
    leader: AnnotationLeader = field(default_factory=AnnotationLeader)

    def __post_init__(self) -> None:
        if not is_safe_stable_id(self.annotation_id):
            raise ValueError("annotation_id must be a safe stable ID")
        if not is_safe_stable_id(self.clip_id):
            raise ValueError("clip_id must be a safe stable ID")
        if self.anchor_type not in _ANCHOR_TYPES:
            raise ValueError("anchor_type must be cad_anchor or video_track")
        if not isinstance(self.text, str) or len(self.text) > 2000:
            raise ValueError(
                "annotation text must be a string of at most 2000 characters"
            )
        if not isinstance(self.content, AnnotationContent):
            raise TypeError("annotation content must be AnnotationContent")
        # content.body 是新结构的权威值；text 仅作为旧 Stage 9 兼容别名保留。
        if self.content.body != self.text:
            if self.content == AnnotationContent() and self.text:
                object.__setattr__(self, "content", AnnotationContent(body=self.text))
            else:
                object.__setattr__(self, "text", self.content.body)
        if not isinstance(self.anchor, Mapping):
            raise TypeError("annotation anchor must be an object")
        _validate_anchor(self.anchor_type, self.anchor, self.source_pts_range)
        if len(self.screen_offset) != 2:
            raise ValueError("screen_offset must contain x and y")
        for value in self.screen_offset:
            _finite(value, "screen_offset")
        if not isinstance(self.user_visible, bool):
            raise TypeError("user_visible must be boolean")
        if isinstance(self.annotation_revision, bool) or not isinstance(
            self.annotation_revision, int
        ):
            raise TypeError("annotation_revision must be an integer")
        if self.annotation_revision < 0:
            raise ValueError("annotation_revision must be non-negative")
        if (
            self.anchor_type == "cad_anchor"
            and self.active_tracking_revision is not None
        ):
            raise ValueError("cad_anchor cannot own a tracking revision")
        for name in (
            "created_at",
            "updated_at",
            "created_operation_id",
            "updated_operation_id",
        ):
            if not getattr(self, name):
                raise ValueError(f"{name} must not be empty")

    @classmethod
    def new(
        cls,
        *,
        annotation_id: str,
        clip_id: str,
        anchor_type: str,
        text: str,
        content: AnnotationContent | None = None,
        panel: AnnotationPanel | None = None,
        leader: AnnotationLeader | None = None,
        anchor: Mapping[str, Any],
        source_pts_range: SourcePtsRange,
        created_at: str,
        operation_id: str,
        screen_offset: tuple[float, float] = (0.0, 0.0),
        style: AnnotationStyle | None = None,
        visibility_policy: VisibilityPolicy | None = None,
        user_visible: bool = True,
    ) -> Annotation:
        return cls(
            annotation_id=annotation_id,
            clip_id=clip_id,
            anchor_type=anchor_type,
            text=(content.body if content is not None else text),
            anchor=dict(anchor),
            style=style or AnnotationStyle(),
            source_pts_range=source_pts_range,
            screen_offset=(float(screen_offset[0]), float(screen_offset[1])),
            visibility_policy=visibility_policy or VisibilityPolicy(),
            user_visible=user_visible,
            annotation_revision=0,
            active_tracking_revision=None,
            created_at=created_at,
            updated_at=created_at,
            created_operation_id=operation_id,
            updated_operation_id=operation_id,
            content=content or AnnotationContent(body=text),
            panel=panel or AnnotationPanel(),
            leader=leader or AnnotationLeader(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "annotation_id": self.annotation_id,
            "clip_id": self.clip_id,
            "anchor_type": self.anchor_type,
            "text": self.text,
            "content": self.content.to_dict(),
            "panel": self.panel.to_dict(),
            "leader": self.leader.to_dict(),
            "anchor": dict(self.anchor),
            "style": self.style.to_dict(),
            "source_pts_range": self.source_pts_range.to_dict(),
            "screen_offset": list(self.screen_offset),
            "visibility_policy": self.visibility_policy.to_dict(),
            "user_visible": self.user_visible,
            "annotation_revision": self.annotation_revision,
            "active_tracking_revision": self.active_tracking_revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "created_operation_id": self.created_operation_id,
            "updated_operation_id": self.updated_operation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Annotation:
        offset = value.get("screen_offset", (0.0, 0.0))
        if not isinstance(offset, (list, tuple)) or len(offset) != 2:
            raise ValueError("screen_offset must contain x and y")
        legacy_text = str(value.get("text", ""))
        content = AnnotationContent.from_dict(
            value.get("content") if isinstance(value.get("content"), Mapping) else None,
            legacy_text=legacy_text,
        )
        return cls(
            annotation_id=str(value["annotation_id"]),
            clip_id=str(value["clip_id"]),
            anchor_type=str(value["anchor_type"]),
            text=content.body,
            anchor=dict(value.get("anchor") or {}),
            style=AnnotationStyle.from_dict(value.get("style")),
            source_pts_range=SourcePtsRange.from_dict(value["source_pts_range"]),
            screen_offset=(float(offset[0]), float(offset[1])),
            visibility_policy=VisibilityPolicy.from_dict(
                value.get("visibility_policy")
            ),
            user_visible=bool(value.get("user_visible", True)),
            annotation_revision=int(value.get("annotation_revision", 0)),
            active_tracking_revision=(
                None
                if value.get("active_tracking_revision") is None
                else str(value["active_tracking_revision"])
            ),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            created_operation_id=str(value["created_operation_id"]),
            updated_operation_id=str(value["updated_operation_id"]),
            content=content,
            panel=AnnotationPanel.from_dict(
                value.get("panel") if isinstance(value.get("panel"), Mapping) else None
            ),
            leader=AnnotationLeader.from_dict(
                value.get("leader")
                if isinstance(value.get("leader"), Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class AnnotationsManifest(ManifestHeader):
    owner: ClassVar[str] = "annotations"

    annotations: tuple[Annotation, ...] = ()
    references: tuple[StateReference, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        super().__post_init__()
        identities = [annotation.annotation_id for annotation in self.annotations]
        if len(identities) != len(set(identities)):
            raise ValueError("annotation_id values must be unique")

    @classmethod
    def new(
        cls,
        project_id: str,
        *,
        updated_at: str,
        annotations: tuple[Annotation, ...] = (),
    ) -> AnnotationsManifest:
        return cls(
            schema_version=SCHEMA_VERSION,
            revision=0,
            updated_at=updated_at,
            project_id=project_id,
            annotations=annotations,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            **self._header_dict(),
            "annotations": [annotation.to_dict() for annotation in self.annotations],
            "references": [reference.to_dict() for reference in self.references],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AnnotationsManifest:
        return cls(
            **_header_from_dict(value, cls.owner),
            annotations=tuple(
                Annotation.from_dict(item) for item in value.get("annotations", ())
            ),
            references=tuple(
                StateReference.from_dict(item) for item in value.get("references", ())
            ),
        )
