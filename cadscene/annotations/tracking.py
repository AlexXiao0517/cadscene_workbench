from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
from typing import Callable, Mapping, Sequence

import cv2
import numpy as np

from cadscene.projects.identifiers import validate_project_id
from cadscene.projects.json_repositories import ProjectRepositories
from cadscene.video_analysis.pts import DecodedFrameIndex, probe_decoded_frame_index

from .service import AnnotationMutationResult, AnnotationService


def _finite_tuple(
    value: Sequence[float] | None, size: int, name: str
) -> tuple[float, ...] | None:
    if value is None:
        return None
    if len(value) != size:
        raise ValueError(f"{name} must contain {size} numbers")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        raise ValueError(f"{name} must contain finite numbers")
    return result


@dataclass(frozen=True)
class TrackingFrame:
    source_pts: int
    image_bgr: np.ndarray

    def __post_init__(self) -> None:
        if isinstance(self.source_pts, bool) or not isinstance(self.source_pts, int):
            raise TypeError("tracking frame source_pts must be an integer")
        if not isinstance(self.image_bgr, np.ndarray) or self.image_bgr.ndim not in {2, 3}:
            raise TypeError("tracking frame image must be a grayscale or BGR ndarray")


@dataclass(frozen=True)
class VideoTrackingInitialization:
    source_pts: int
    bbox: tuple[float, float, float, float] | None = None
    anchor_xy: tuple[float, float] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.source_pts, bool) or not isinstance(self.source_pts, int):
            raise TypeError("tracking initialization source_pts must be an integer")
        bbox = _finite_tuple(self.bbox, 4, "bbox")
        anchor = _finite_tuple(self.anchor_xy, 2, "anchor_xy")
        object.__setattr__(self, "bbox", bbox)
        object.__setattr__(self, "anchor_xy", anchor)
        if bbox is None and anchor is None:
            raise ValueError("tracking initialization requires bbox or anchor_xy")
        if bbox is not None and (bbox[2] <= 0.0 or bbox[3] <= 0.0):
            raise ValueError("tracking initialization bbox must have positive size")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_pts": self.source_pts,
            "bbox": None if self.bbox is None else list(self.bbox),
            "anchor_xy": None if self.anchor_xy is None else list(self.anchor_xy),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> VideoTrackingInitialization:
        return cls(
            source_pts=int(value["source_pts"]),
            bbox=value.get("bbox"),
            anchor_xy=value.get("anchor_xy"),
        )


@dataclass(frozen=True)
class TrackingResult:
    source_pts: int
    bbox: tuple[float, float, float, float] | None
    anchor_xy: tuple[float, float] | None
    confidence: float
    visible: bool
    tracking_status: str
    diagnostic: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "bbox", _finite_tuple(self.bbox, 4, "bbox"))
        object.__setattr__(
            self, "anchor_xy", _finite_tuple(self.anchor_xy, 2, "anchor_xy")
        )
        if not 0.0 <= float(self.confidence) <= 1.0:
            raise ValueError("tracking confidence must be between 0 and 1")
        if not self.visible and (self.bbox is not None or self.anchor_xy is not None):
            raise ValueError("hidden tracking results cannot retain a frozen position")
        if self.tracking_status not in {"initialized", "tracked", "lost"}:
            raise ValueError("unsupported tracking status")

    def to_dict(self) -> dict[str, object]:
        return {
            "source_pts": self.source_pts,
            "bbox": None if self.bbox is None else list(self.bbox),
            "anchor_xy": None if self.anchor_xy is None else list(self.anchor_xy),
            "confidence": self.confidence,
            "visibility": self.visible,
            "tracking_status": self.tracking_status,
            "diagnostic": self.diagnostic,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TrackingResult:
        return cls(
            source_pts=int(value["source_pts"]),
            bbox=value.get("bbox"),
            anchor_xy=value.get("anchor_xy"),
            confidence=float(value["confidence"]),
            visible=bool(value["visibility"]),
            tracking_status=str(value["tracking_status"]),
            diagnostic=str(value.get("diagnostic", "")),
        )


class VideoAnchorTracker(ABC):
    name: str
    version: str

    @abstractmethod
    def track(
        self,
        frames: Sequence[TrackingFrame],
        initialization: VideoTrackingInitialization,
    ) -> tuple[TrackingResult, ...]:
        raise NotImplementedError


def _gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return np.ascontiguousarray(image)
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def _bbox_for_initialization(
    initialization: VideoTrackingInitialization, width: int, height: int
) -> tuple[float, float, float, float]:
    if initialization.bbox is not None:
        bbox = initialization.bbox
    else:
        assert initialization.anchor_xy is not None
        bbox = (
            initialization.anchor_xy[0] - 16.0,
            initialization.anchor_xy[1] - 16.0,
            32.0,
            32.0,
        )
    x, y, w, h = bbox
    left = max(0.0, min(float(width - 1), x))
    top = max(0.0, min(float(height - 1), y))
    right = max(left + 1.0, min(float(width), x + w))
    bottom = max(top + 1.0, min(float(height), y + h))
    return left, top, right - left, bottom - top


def _center(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    return bbox[0] + bbox[2] * 0.5, bbox[1] + bbox[3] * 0.5


class OpenCvLkVideoAnchorTracker(VideoAnchorTracker):
    """Sparse pyramidal LK with FB checks and robust ROI propagation."""

    name = "opencv_sparse_lk"
    version = "1"

    def __init__(
        self,
        *,
        min_features: int = 6,
        max_features: int = 100,
        max_forward_backward_error: float = 1.5,
        min_confidence: float = 0.45,
    ) -> None:
        self.min_features = int(min_features)
        self.max_features = int(max_features)
        self.max_forward_backward_error = float(max_forward_backward_error)
        self.min_confidence = float(min_confidence)

    def _features(
        self, gray: np.ndarray, bbox: tuple[float, float, float, float]
    ) -> np.ndarray | None:
        mask = np.zeros(gray.shape[:2], dtype=np.uint8)
        x, y, w, h = bbox
        x0, y0 = max(0, int(math.floor(x))), max(0, int(math.floor(y)))
        x1 = min(gray.shape[1], int(math.ceil(x + w)))
        y1 = min(gray.shape[0], int(math.ceil(y + h)))
        mask[y0:y1, x0:x1] = 255
        return cv2.goodFeaturesToTrack(
            gray,
            mask=mask,
            maxCorners=self.max_features,
            qualityLevel=0.01,
            minDistance=3.0,
            blockSize=3,
        )

    def _direction(
        self,
        frames: Sequence[TrackingFrame],
        initial_bbox: tuple[float, float, float, float],
    ) -> list[TrackingResult]:
        previous_gray = _gray(frames[0].image_bgr)
        points = self._features(previous_gray, initial_bbox)
        results: list[TrackingResult] = []
        if points is None or len(points) < self.min_features:
            return [
                TrackingResult(
                    source_pts=frame.source_pts,
                    bbox=None,
                    anchor_xy=None,
                    confidence=0.0,
                    visible=False,
                    tracking_status="lost",
                    diagnostic="insufficient_initial_features",
                )
                for frame in frames[1:]
            ]
        bbox = initial_bbox
        lost = False
        for frame in frames[1:]:
            if lost:
                results.append(
                    TrackingResult(
                        frame.source_pts,
                        None,
                        None,
                        0.0,
                        False,
                        "lost",
                        "tracking_lost_in_prior_frame",
                    )
                )
                continue
            current_gray = _gray(frame.image_bgr)
            forward, status_forward, _ = cv2.calcOpticalFlowPyrLK(
                previous_gray,
                current_gray,
                points,
                None,
                winSize=(21, 21),
                maxLevel=3,
                criteria=(
                    cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                    30,
                    0.01,
                ),
            )
            if forward is None or status_forward is None:
                valid = np.zeros(len(points), dtype=bool)
                backward = None
            else:
                backward, status_backward, _ = cv2.calcOpticalFlowPyrLK(
                    current_gray,
                    previous_gray,
                    forward,
                    None,
                    winSize=(21, 21),
                    maxLevel=3,
                    criteria=(
                        cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                        30,
                        0.01,
                    ),
                )
                if backward is None or status_backward is None:
                    valid = np.zeros(len(points), dtype=bool)
                else:
                    fb_error = np.linalg.norm(
                        points.reshape(-1, 2) - backward.reshape(-1, 2), axis=1
                    )
                    valid = (
                        status_forward.reshape(-1).astype(bool)
                        & status_backward.reshape(-1).astype(bool)
                        & np.isfinite(fb_error)
                        & (fb_error <= self.max_forward_backward_error)
                    )
            count = int(np.count_nonzero(valid))
            diagnostic = "insufficient_tracked_features"
            if count >= self.min_features and forward is not None and backward is not None:
                old_valid = points.reshape(-1, 2)[valid]
                new_valid = forward.reshape(-1, 2)[valid]
                fb_valid = np.linalg.norm(
                    old_valid - backward.reshape(-1, 2)[valid], axis=1
                )
                matrix, _inliers = cv2.estimateAffinePartial2D(
                    old_valid,
                    new_valid,
                    method=cv2.RANSAC,
                    ransacReprojThreshold=2.0,
                )
                if matrix is None:
                    delta = np.median(new_valid - old_valid, axis=0)
                    matrix = np.asarray(
                        [[1.0, 0.0, delta[0]], [0.0, 1.0, delta[1]]],
                        dtype=np.float64,
                    )
                corners = np.asarray(
                    [
                        [bbox[0], bbox[1]],
                        [bbox[0] + bbox[2], bbox[1]],
                        [bbox[0], bbox[1] + bbox[3]],
                        [bbox[0] + bbox[2], bbox[1] + bbox[3]],
                    ],
                    dtype=np.float64,
                )
                transformed = corners @ matrix[:, :2].T + matrix[:, 2]
                x0, y0 = np.min(transformed, axis=0)
                x1, y1 = np.max(transformed, axis=0)
                candidate_bbox = (float(x0), float(y0), float(x1 - x0), float(y1 - y0))
                anchor = _center(candidate_bbox)
                retained = count / max(1, len(points))
                confidence = float(
                    np.clip(
                        retained
                        * math.exp(-float(np.median(fb_valid))),
                        0.0,
                        1.0,
                    )
                )
                height, width = current_gray.shape[:2]
                inside = 0.0 <= anchor[0] < width and 0.0 <= anchor[1] < height
                if not inside:
                    diagnostic = "target_left_frame_boundary"
                elif confidence < self.min_confidence:
                    diagnostic = "tracking_confidence_below_threshold"
                else:
                    bbox = candidate_bbox
                    points = new_valid.reshape(-1, 1, 2).astype(np.float32)
                    previous_gray = current_gray
                    results.append(
                        TrackingResult(
                            source_pts=frame.source_pts,
                            bbox=bbox,
                            anchor_xy=anchor,
                            confidence=confidence,
                            visible=True,
                            tracking_status="tracked",
                            diagnostic="tracked",
                        )
                    )
                    continue
            lost = True
            results.append(
                TrackingResult(
                    source_pts=frame.source_pts,
                    bbox=None,
                    anchor_xy=None,
                    confidence=0.0,
                    visible=False,
                    tracking_status="lost",
                    diagnostic=diagnostic,
                )
            )
        return results

    def track(
        self,
        frames: Sequence[TrackingFrame],
        initialization: VideoTrackingInitialization,
    ) -> tuple[TrackingResult, ...]:
        frames = tuple(frames)
        if not frames:
            raise ValueError("video tracking requires decoded frames")
        pts = tuple(frame.source_pts for frame in frames)
        if any(current >= following for current, following in zip(pts, pts[1:])):
            raise ValueError("tracking frames must have strictly increasing source_pts")
        try:
            initial_index = pts.index(initialization.source_pts)
        except ValueError as exc:
            raise ValueError("tracking initialization must match a decoded source PTS") from exc
        height, width = frames[initial_index].image_bgr.shape[:2]
        bbox = _bbox_for_initialization(initialization, width, height)
        initialized = TrackingResult(
            source_pts=initialization.source_pts,
            bbox=bbox,
            anchor_xy=_center(bbox),
            confidence=1.0,
            visible=True,
            tracking_status="initialized",
            diagnostic="initialized",
        )
        by_pts = {initialized.source_pts: initialized}
        forward_frames = frames[initial_index:]
        for result in self._direction(forward_frames, bbox):
            by_pts[result.source_pts] = result
        backward_frames = tuple(reversed(frames[: initial_index + 1]))
        for result in self._direction(backward_frames, bbox):
            by_pts[result.source_pts] = result
        return tuple(by_pts[source_pts] for source_pts in pts)


def decode_tracking_frames(
    video_path: Path,
    *,
    source_start_pts: int,
    source_end_pts_exclusive: int,
    expected_time_base: Fraction,
    frame_index: DecodedFrameIndex | None = None,
) -> tuple[TrackingFrame, ...]:
    """Decode pixels in presentation order and bind them to probed integer PTS."""

    source = Path(video_path)
    index = frame_index or probe_decoded_frame_index(source)
    if index.time_base != expected_time_base:
        raise ValueError("tracking source time_base differs from its clip contract")
    selected = tuple(
        frame
        for frame in index.frames
        if source_start_pts <= frame.pts < source_end_pts_exclusive
    )
    if not selected:
        raise ValueError("tracking clip contains no decoded source frames")
    selected_by_ordinal = {frame.ordinal: frame.pts for frame in selected}
    last_ordinal = selected[-1].ordinal
    capture = cv2.VideoCapture(str(source))
    if not capture.isOpened():
        raise RuntimeError(f"tracking video cannot be decoded: {source}")
    decoded: list[TrackingFrame] = []
    ordinal = 0
    try:
        while ordinal <= last_ordinal:
            ok, image = capture.read()
            if not ok or image is None:
                break
            source_pts = selected_by_ordinal.get(ordinal)
            if source_pts is not None:
                decoded.append(TrackingFrame(source_pts, image))
            ordinal += 1
    finally:
        capture.release()
    if len(decoded) != len(selected):
        raise RuntimeError(
            "decoded tracking pixels do not match the authoritative frame index"
        )
    return tuple(decoded)


@dataclass(frozen=True)
class TrackingRevision:
    tracking_revision: str
    annotation_id: str
    clip_id: str
    tracker_name: str
    tracker_version: str
    source_video_fingerprint: str
    clip_revision: int
    source_start_pts: int
    source_end_pts_exclusive: int
    time_base_numerator: int
    time_base_denominator: int
    initialization: VideoTrackingInitialization
    corrections: tuple[VideoTrackingInitialization, ...]
    results: tuple[TrackingResult, ...]
    created_at: str
    operation_id: str

    def __post_init__(self) -> None:
        for value in (
            self.tracking_revision,
            self.annotation_id,
            self.clip_id,
            self.tracker_name,
            self.tracker_version,
            self.source_video_fingerprint,
            self.created_at,
            self.operation_id,
        ):
            if not value:
                raise ValueError("tracking revision metadata must not be empty")
        if self.source_end_pts_exclusive <= self.source_start_pts:
            raise ValueError("tracking revision clip range must be non-empty")
        anchor_pts = (
            self.initialization.source_pts,
            *(item.source_pts for item in self.corrections),
        )
        if any(
            pts < self.source_start_pts or pts >= self.source_end_pts_exclusive
            for pts in anchor_pts
        ):
            raise ValueError("tracking anchor escaped clip source PTS range")
        if any(after <= before for before, after in zip(anchor_pts, anchor_pts[1:])):
            raise ValueError("tracking correction PTS must be strictly increasing")
        result_pts = tuple(item.source_pts for item in self.results)
        if len(result_pts) != len(set(result_pts)):
            raise ValueError("tracking results require unique source_pts")
        if any(
            pts < self.source_start_pts or pts >= self.source_end_pts_exclusive
            for pts in result_pts
        ):
            raise ValueError("tracking result escaped clip source PTS range")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "tracking_revision": self.tracking_revision,
            "annotation_id": self.annotation_id,
            "clip_id": self.clip_id,
            "tracker": {"name": self.tracker_name, "version": self.tracker_version},
            "source_video_fingerprint": self.source_video_fingerprint,
            "clip_revision": self.clip_revision,
            "source_pts_range": {
                "start_pts": self.source_start_pts,
                "end_pts_exclusive": self.source_end_pts_exclusive,
                "time_base": {
                    "numerator": self.time_base_numerator,
                    "denominator": self.time_base_denominator,
                },
                "semantics": "half_open",
            },
            "initialization": self.initialization.to_dict(),
            "corrections": [item.to_dict() for item in self.corrections],
            "results": [item.to_dict() for item in self.results],
            "created_at": self.created_at,
            "operation_id": self.operation_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TrackingRevision:
        tracker = value["tracker"]
        source_range = value["source_pts_range"]
        time_base = source_range["time_base"]
        return cls(
            tracking_revision=str(value["tracking_revision"]),
            annotation_id=str(value["annotation_id"]),
            clip_id=str(value["clip_id"]),
            tracker_name=str(tracker["name"]),
            tracker_version=str(tracker["version"]),
            source_video_fingerprint=str(value["source_video_fingerprint"]),
            clip_revision=int(value["clip_revision"]),
            source_start_pts=int(source_range["start_pts"]),
            source_end_pts_exclusive=int(source_range["end_pts_exclusive"]),
            time_base_numerator=int(time_base["numerator"]),
            time_base_denominator=int(time_base["denominator"]),
            initialization=VideoTrackingInitialization.from_dict(
                value["initialization"]
            ),
            corrections=tuple(
                VideoTrackingInitialization.from_dict(item)
                for item in value.get("corrections", ())
            ),
            results=tuple(
                TrackingResult.from_dict(item) for item in value.get("results", ())
            ),
            created_at=str(value["created_at"]),
            operation_id=str(value["operation_id"]),
        )


class TrackingRevisionRepository:
    def __init__(self, projects_root: Path) -> None:
        self.projects_root = Path(projects_root)

    def directory_for(
        self,
        project_id: str,
        clip_id: str,
        annotation_id: str,
        tracking_revision: str,
    ) -> Path:
        return (
            self.projects_root
            / validate_project_id(project_id)
            / "annotations"
            / validate_project_id(clip_id)
            / validate_project_id(annotation_id)
            / validate_project_id(tracking_revision)
        )

    def publish(self, project_id: str, revision: TrackingRevision) -> Path:
        target = self.directory_for(
            project_id,
            revision.clip_id,
            revision.annotation_id,
            revision.tracking_revision,
        )
        if target.exists():
            raise FileExistsError(f"immutable tracking revision already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = Path(
            tempfile.mkdtemp(prefix=f".{revision.tracking_revision}-", dir=target.parent)
        )
        try:
            payload = (
                json.dumps(
                    revision.to_dict(), ensure_ascii=False, indent=2, sort_keys=True
                )
                + "\n"
            ).encode("utf-8")
            result = temporary / "tracking_results.json"
            with result.open("wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
        return target / "tracking_results.json"

    def load(
        self,
        project_id: str,
        clip_id: str,
        annotation_id: str,
        tracking_revision: str,
    ) -> TrackingRevision:
        path = self.directory_for(
            project_id, clip_id, annotation_id, tracking_revision
        ) / "tracking_results.json"
        return TrackingRevision.from_dict(json.loads(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class VideoTrackingRevisionResult:
    revision: TrackingRevision
    artifact_path: Path
    annotation_result: AnnotationMutationResult


class VideoTrackingService:
    """Builds immutable tracker output, then activates it on the annotation."""

    def __init__(
        self,
        *,
        repositories: ProjectRepositories,
        annotations: AnnotationService,
        revisions: TrackingRevisionRepository,
        tracker: VideoAnchorTracker,
        now: Callable[[], str],
        identity: Callable[[], str],
    ) -> None:
        self.repositories = repositories
        self.annotations = annotations
        self.revisions = revisions
        self.tracker = tracker
        self.now = now
        self.identity = identity

    def create_revision(
        self,
        project_id: str,
        annotation_id: str,
        *,
        expected_revision: int,
        expected_annotation_revision: int,
        source_video_fingerprint: str,
        frames: Sequence[TrackingFrame],
        correction: VideoTrackingInitialization | None = None,
    ) -> VideoTrackingRevisionResult:
        annotations = self.repositories.annotations.load(project_id)
        annotation = next(
            (item for item in annotations.annotations if item.annotation_id == annotation_id),
            None,
        )
        if annotation is None:
            raise FileNotFoundError(f"annotation not found: {annotation_id}")
        if annotation.anchor_type != "video_track":
            raise ValueError("tracking revisions require a video_track annotation")
        if annotation.annotation_revision != expected_annotation_revision:
            from .service import AnnotationRevisionConflict

            raise AnnotationRevisionConflict(
                expected_annotation_revision, annotation.annotation_revision
            )
        clips = self.repositories.clips.load(project_id)
        clip = next(
            (item for item in clips.clips if item.clip_id == annotation.clip_id), None
        )
        if clip is None:
            raise ValueError(f"annotation clip is unavailable: {annotation.clip_id}")
        time_base = clip.analysis.get("source_time_base")
        if not isinstance(time_base, Mapping):
            raise ValueError("tracking clip is missing exact source time_base")
        start_pts = int(clip.analysis["source_start_pts"])
        end_pts = int(clip.analysis["source_end_pts_exclusive"])
        frames = tuple(frames)
        if any(
            frame.source_pts < start_pts or frame.source_pts >= end_pts
            for frame in frames
        ):
            raise ValueError("tracking frames cannot cross the current clip boundary")
        initialization_payload = annotation.anchor.get("initialization")
        if not isinstance(initialization_payload, Mapping):
            raise ValueError("video annotation initialization is unavailable")
        initialization = VideoTrackingInitialization.from_dict(initialization_payload)
        corrections: tuple[VideoTrackingInitialization, ...] = ()
        previous_results: tuple[TrackingResult, ...] = ()
        tracking_frames = frames
        tracker_initialization = initialization
        if correction is not None:
            if annotation.active_tracking_revision is None:
                raise ValueError("re-anchor requires an active tracking revision")
            previous = self.revisions.load(
                project_id,
                annotation.clip_id,
                annotation.annotation_id,
                annotation.active_tracking_revision,
            )
            if (
                previous.source_video_fingerprint != source_video_fingerprint
                or previous.clip_revision != clips.revision
            ):
                raise ValueError("active tracking revision dependencies are stale")
            last_anchor_pts = (
                previous.corrections[-1].source_pts
                if previous.corrections
                else initialization.source_pts
            )
            if correction.source_pts <= last_anchor_pts:
                raise ValueError(
                    "correction PTS must follow the latest anchor PTS"
                )
            corrections = (*previous.corrections, correction)
            previous_results = tuple(
                item for item in previous.results if item.source_pts < correction.source_pts
            )
            tracking_frames = tuple(
                frame for frame in frames if frame.source_pts >= correction.source_pts
            )
            tracker_initialization = correction
        tracked = self.tracker.track(tracking_frames, tracker_initialization)
        if self.repositories.clips.load(project_id).revision != clips.revision:
            raise ValueError("clip revision changed while video tracking was running")
        results = tuple(sorted((*previous_results, *tracked), key=lambda item: item.source_pts))
        tracking_revision = f"tracking-{self.identity()}"
        operation_id = f"tracking-operation-{self.identity()}"
        revision = TrackingRevision(
            tracking_revision=tracking_revision,
            annotation_id=annotation.annotation_id,
            clip_id=annotation.clip_id,
            tracker_name=self.tracker.name,
            tracker_version=self.tracker.version,
            source_video_fingerprint=source_video_fingerprint,
            clip_revision=clips.revision,
            source_start_pts=start_pts,
            source_end_pts_exclusive=end_pts,
            time_base_numerator=int(time_base["numerator"]),
            time_base_denominator=int(time_base["denominator"]),
            initialization=initialization,
            corrections=corrections,
            results=results,
            created_at=self.now(),
            operation_id=operation_id,
        )
        artifact_path = self.revisions.publish(project_id, revision)
        annotation_result = self.annotations.activate_tracking_revision(
            project_id,
            annotation_id,
            tracking_revision,
            expected_revision=expected_revision,
            expected_annotation_revision=expected_annotation_revision,
        )
        return VideoTrackingRevisionResult(
            revision=revision,
            artifact_path=artifact_path,
            annotation_result=annotation_result,
        )
