from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
import os
from pathlib import Path
from typing import Mapping, Sequence

from cadscene.cad.projection import project_point
from cadscene.core.camera import CameraState
from cadscene.rendering.overlay import load_camera_path_csv


@dataclass(frozen=True)
class AnnotationRenderEvent:
    annotation_id: str
    source_pts: int
    start_sec: float
    end_sec: float
    x: float
    y: float
    text: str
    style: Mapping[str, object]


def _time_base(bundle: Mapping[str, object]) -> Fraction:
    value = bundle.get("source_time_base")
    if not isinstance(value, Mapping):
        raise ValueError("annotation render bundle requires exact source_time_base")
    return Fraction(int(value["numerator"]), int(value["denominator"]))


def _camera_states(
    camera_rows: Sequence[Mapping[str, object] | tuple[int, CameraState]],
) -> tuple[tuple[int, CameraState], ...]:
    states = []
    for row in camera_rows:
        if isinstance(row, tuple):
            states.append((int(row[0]), row[1]))
        else:
            states.append(
                (
                    int(row.get("frame_index", len(states))),
                    CameraState.from_row(dict(row)),
                )
            )
    return tuple(sorted(states, key=lambda item: item[0]))


def _nearest_camera(
    frame_index: int, cameras: tuple[tuple[int, CameraState], ...]
) -> CameraState | None:
    if not cameras:
        return None
    return min(cameras, key=lambda item: abs(item[0] - frame_index))[1]


def _tracking_by_pts(
    bundle: Mapping[str, object], revision: object
) -> dict[int, Mapping[str, object]]:
    revisions = bundle.get("tracking_revisions")
    payload = revisions.get(revision) if isinstance(revisions, Mapping) else None
    results = payload.get("results") if isinstance(payload, Mapping) else None
    if not isinstance(results, list):
        return {}
    return {
        int(item["source_pts"]): item
        for item in results
        if isinstance(item, Mapping) and isinstance(item.get("source_pts"), int)
    }


def build_annotation_events(
    bundle: Mapping[str, object],
    *,
    camera_rows: Sequence[Mapping[str, object] | tuple[int, CameraState]],
) -> tuple[AnnotationRenderEvent, ...]:
    frames = bundle.get("source_frames")
    annotations = bundle.get("annotations")
    if not isinstance(frames, list) or not frames:
        raise ValueError("annotation render bundle requires source frames")
    if not isinstance(annotations, list):
        raise ValueError("annotation render bundle requires annotations")
    time_base = _time_base(bundle)
    width = int(bundle["video_width"])
    height = int(bundle["video_height"])
    if width <= 0 or height <= 0:
        raise ValueError("annotation render dimensions must be positive")
    cameras = _camera_states(camera_rows)
    first_pts = int(frames[0]["source_pts"])
    events: list[AnnotationRenderEvent] = []
    tracking_cache: dict[str, dict[int, Mapping[str, object]]] = {}
    for frame_index, frame in enumerate(frames):
        source_pts = int(frame["source_pts"])
        if frame_index + 1 < len(frames):
            end_pts = int(frames[frame_index + 1]["source_pts"])
        else:
            duration = frame.get("duration_pts")
            if not isinstance(duration, int) or duration <= 0:
                duration = (
                    source_pts - int(frames[frame_index - 1]["source_pts"])
                    if frame_index > 0
                    else 1
                )
            end_pts = source_pts + duration
        start_sec = float((source_pts - first_pts) * time_base)
        end_sec = float((end_pts - first_pts) * time_base)
        for annotation in annotations:
            if not isinstance(annotation, Mapping) or not annotation.get(
                "user_visible", True
            ):
                continue
            source_range = annotation.get("source_pts_range")
            if not isinstance(source_range, Mapping) or not (
                int(source_range["start_pts"])
                <= source_pts
                < int(source_range["end_pts_exclusive"])
            ):
                continue
            offset = annotation.get("screen_offset", (0.0, 0.0))
            if not isinstance(offset, (list, tuple)) or len(offset) != 2:
                raise ValueError("annotation screen_offset must contain x and y")
            anchor_type = annotation.get("anchor_type")
            x: float | None = None
            y: float | None = None
            if anchor_type == "video_track":
                revision = str(annotation.get("active_tracking_revision") or "")
                if revision not in tracking_cache:
                    tracking_cache[revision] = _tracking_by_pts(bundle, revision)
                result = tracking_cache[revision].get(source_pts)
                policy = annotation.get("visibility_policy")
                threshold = float(
                    policy.get("min_tracking_confidence", 0.5)
                    if isinstance(policy, Mapping)
                    else 0.5
                )
                if (
                    result is None
                    or result.get("visibility") is not True
                    or float(result.get("confidence", 0.0)) < threshold
                    or not isinstance(result.get("anchor_xy"), (list, tuple))
                ):
                    continue
                anchor_xy = result["anchor_xy"]
                x = float(anchor_xy[0]) + float(offset[0])
                y = float(anchor_xy[1]) + float(offset[1])
            elif anchor_type == "cad_anchor":
                camera = _nearest_camera(frame_index, cameras)
                anchor = annotation.get("anchor")
                world = anchor.get("cad_world_xyz") if isinstance(anchor, Mapping) else None
                if camera is None or not isinstance(world, (list, tuple)):
                    continue
                projected = project_point(world, camera, width=width, height=height)
                if projected is None or not (
                    0.0 <= projected.u < width and 0.0 <= projected.v < height
                ):
                    continue
                x = projected.u + float(offset[0])
                y = projected.v + float(offset[1])
            if x is None or y is None:
                continue
            events.append(
                AnnotationRenderEvent(
                    annotation_id=str(annotation["annotation_id"]),
                    source_pts=source_pts,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    x=x,
                    y=y,
                    text=str(annotation.get("text", "")),
                    style=(
                        annotation["style"]
                        if isinstance(annotation.get("style"), Mapping)
                        else {}
                    ),
                )
            )
    return tuple(events)


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, int(round(float(seconds) * 100.0)))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole_seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"


def _ass_color(value: object, default: str) -> str:
    text = str(value or default).lstrip("#")
    if len(text) == 6:
        text += "FF"
    if len(text) != 8:
        text = default.lstrip("#")
        if len(text) == 6:
            text += "FF"
    red, green, blue, alpha = text[0:2], text[2:4], text[4:6], text[6:8]
    ass_alpha = f"{255 - int(alpha, 16):02X}"
    return f"&H{ass_alpha}{blue}{green}{red}"


def _escape_ass_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\\N")
        .replace("\n", "\\N")
    )


def build_ass_document(
    events: Sequence[AnnotationRenderEvent], *, width: int, height: int
) -> str:
    styles: dict[str, Mapping[str, object]] = {}
    for event in events:
        styles.setdefault(event.annotation_id, event.style)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
    ]
    for index, (annotation_id, style) in enumerate(styles.items()):
        name = f"Label{index}"
        styles[annotation_id] = {**style, "_ass_name": name}
        lines.append(
            "Style: "
            + ",".join(
                (
                    name,
                    str(style.get("font_family") or "sans-serif"),
                    str(int(style.get("font_size_px") or 28)),
                    _ass_color(style.get("text_color"), "#FFFFFFFF"),
                    _ass_color(style.get("text_color"), "#FFFFFFFF"),
                    _ass_color(style.get("border_color"), "#FFFFFFCC"),
                    _ass_color(style.get("background_color"), "#000000B3"),
                    "-1" if int(style.get("font_weight") or 600) >= 600 else "0",
                    "0", "0", "0", "100", "100", "0", "0", "3", "1", "0", "5", "0", "0", "0", "1",
                )
            )
        )
    lines.extend(
        (
            "",
            "[Events]",
            "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
        )
    )
    for event in events:
        style_name = str(styles[event.annotation_id]["_ass_name"])
        text = f"{{\\pos({event.x:.2f},{event.y:.2f})}}{_escape_ass_text(event.text)}"
        lines.append(
            f"Dialogue: 0,{_ass_time(event.start_sec)},{_ass_time(event.end_sec)},{style_name},,0,0,0,,{text}"
        )
    return "\n".join(lines) + "\n"


def build_sendcmd_document(
    events: Sequence[AnnotationRenderEvent],
) -> tuple[str, dict[str, str]]:
    """Build exact-time drawtext updates without ASS centisecond quantization."""

    annotation_ids = tuple(sorted({event.annotation_id for event in events}))
    targets = {
        annotation_id: f"Label{index}"
        for index, annotation_id in enumerate(annotation_ids)
    }
    grouped: dict[str, list[AnnotationRenderEvent]] = {
        annotation_id: [] for annotation_id in annotation_ids
    }
    for event in sorted(
        events, key=lambda item: (item.annotation_id, item.start_sec, item.source_pts)
    ):
        grouped[event.annotation_id].append(event)

    commands: list[tuple[float, int, str]] = []
    sequence = 0
    for annotation_id in annotation_ids:
        target = targets[annotation_id]
        annotation_events = grouped[annotation_id]
        for index, event in enumerate(annotation_events):
            prefix = f"drawtext@{target}"
            commands.extend(
                (
                    (
                        event.start_sec,
                        sequence,
                        f"{prefix} x {event.x:.6f}",
                    ),
                    (
                        event.start_sec,
                        sequence + 1,
                        f"{prefix} y {event.y:.6f}-text_h/2",
                    ),
                    (
                        event.start_sec,
                        sequence + 2,
                        f"{prefix} alpha 1",
                    ),
                )
            )
            sequence += 3
            next_event = (
                annotation_events[index + 1]
                if index + 1 < len(annotation_events)
                else None
            )
            if next_event is None or abs(
                next_event.start_sec - event.end_sec
            ) > 1e-12:
                commands.append(
                    (
                        event.end_sec,
                        sequence,
                        f"{prefix} alpha 0",
                    )
                )
                sequence += 1
    document = "\n".join(
        f"{timestamp:.9f} [enter] {command};"
        for timestamp, _, command in sorted(commands)
    )
    if document:
        document += "\n"
    return document, targets


def _filter_path(path: Path) -> str:
    value = str(path.resolve()).replace("\\", "/")
    return value.replace(":", "\\:").replace("'", "\\'")


def _filter_value(value: object) -> str:
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
    )


def build_drawtext_filter(
    events: Sequence[AnnotationRenderEvent],
    *,
    command_path: Path,
    text_paths: Mapping[str, Path],
    targets: Mapping[str, str],
    font_paths: Mapping[str, Path] | None = None,
) -> str:
    """Build one runtime-updated drawtext filter per annotation."""

    first_events: dict[str, AnnotationRenderEvent] = {}
    for event in events:
        first_events.setdefault(event.annotation_id, event)
    # Preserve the input PTS and final-frame duration. Resetting PTS here causes
    # the MP4 muxer to mark the final filtered frame as discard on FFmpeg 7.x.
    filters = [f"[0:v]sendcmd=filename='{_filter_path(command_path)}'"]
    for annotation_id in sorted(first_events):
        event = first_events[annotation_id]
        style = event.style
        text_path = text_paths.get(annotation_id)
        target = targets.get(annotation_id)
        if text_path is None or target is None:
            raise ValueError(f"drawtext resources are missing for {annotation_id}")
        font_path = (font_paths or {}).get(annotation_id)
        font_option = (
            f"fontfile='{_filter_path(font_path)}'"
            if font_path is not None
            else f"font='{_filter_value(style.get('font_family') or 'sans-serif')}'"
        )
        filters.append(
            f"drawtext@{target}="
            f"{font_option}"
            f":textfile='{_filter_path(text_path)}'"
            f":fontsize={int(style.get('font_size_px') or 28)}"
            f":fontcolor={_filter_value(style.get('text_color') or '#FFFFFF')}"
            f":box=1:boxcolor={_filter_value(style.get('background_color') or '#000000B3')}"
            f":boxborderw=5"
            f":borderw=1:bordercolor={_filter_value(style.get('border_color') or '#FFFFFFCC')}"
            f":x=0:y=0:alpha=0:fix_bounds=1"
        )
    return ",".join(filters) + "[annotated]\n"


def resolve_font_file(font_family: object) -> Path | None:
    """Resolve a deterministic local font, preferring a CJK-capable fallback."""

    family = str(font_family or "").casefold().replace(" ", "")
    windows_root = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    preferred = []
    if "yahei" in family or "msyh" in family or "微软雅黑" in family:
        preferred.extend((windows_root / "msyh.ttc", windows_root / "msyhbd.ttc"))
    preferred.extend(
        (
            windows_root / "msyh.ttc",
            windows_root / "arial.ttf",
            Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        )
    )
    return next((path for path in preferred if path.is_file()), None)


def load_camera_rows(path: Path | None) -> tuple[tuple[int, CameraState], ...]:
    if path is None:
        return ()
    return tuple(load_camera_path_csv(path))
