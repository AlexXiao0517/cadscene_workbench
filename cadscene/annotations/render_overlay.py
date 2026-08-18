from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
import math
import os
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, ImageDraw, ImageFont

from cadscene.annotations.callout_layout import layout_callout
from cadscene.cad.projection import project_point
from cadscene.core.camera import CameraState
from cadscene.core.coordinates import web_camera_to_cad_meters
from cadscene.rendering.overlay import load_camera_path_csv


@dataclass(frozen=True)
class AnnotationRenderEvent:
    annotation_id: str
    source_pts: int
    start_sec: float
    end_sec: float
    x: float
    y: float
    anchor_x: float
    anchor_y: float
    text: str
    content: Mapping[str, str]
    panel: Mapping[str, object]
    leader: Mapping[str, object]
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


def _cad_projection_world(
    bundle: Mapping[str, object], world: Sequence[object]
) -> Sequence[object]:
    transform = bundle.get("cad_coordinate_transform")
    if not isinstance(transform, Mapping):
        return world
    origin_xy = transform.get("origin_xy")
    if not isinstance(origin_xy, (list, tuple)) or len(origin_xy) != 2:
        raise ValueError("CAD coordinate transform requires origin_xy")
    return web_camera_to_cad_meters(
        {"x": float(world[0]), "y": float(world[1]), "z": float(world[2])},
        origin_xy,
        float(transform["cad_scale"]),
    )


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
            anchor_x: float | None = None
            anchor_y: float | None = None
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
                    or str(result.get("tracking_status", "lost"))
                    not in {"initialized", "tracked"}
                    or float(result.get("confidence", 0.0)) < threshold
                    or not isinstance(result.get("anchor_xy"), (list, tuple))
                ):
                    continue
                anchor_xy = result["anchor_xy"]
                anchor_x = float(anchor_xy[0])
                anchor_y = float(anchor_xy[1])
                if not (
                    math.isfinite(anchor_x)
                    and math.isfinite(anchor_y)
                    and 0.0 <= anchor_x < width
                    and 0.0 <= anchor_y < height
                ):
                    continue
                bbox = result.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    bbox_values = tuple(float(value) for value in bbox)
                    if not (
                        all(math.isfinite(value) for value in bbox_values)
                        and bbox_values[0] >= 0.0
                        and bbox_values[1] >= 0.0
                        and bbox_values[0] + bbox_values[2] <= width
                        and bbox_values[1] + bbox_values[3] <= height
                    ):
                        continue
                x = anchor_x + float(offset[0])
                y = anchor_y + float(offset[1])
            elif anchor_type == "cad_anchor":
                camera = _nearest_camera(frame_index, cameras)
                anchor = annotation.get("anchor")
                world = (
                    anchor.get("cad_world_xyz") if isinstance(anchor, Mapping) else None
                )
                if camera is None or not isinstance(world, (list, tuple)):
                    continue
                projected = project_point(
                    _cad_projection_world(bundle, world),
                    camera,
                    width=width,
                    height=height,
                )
                if projected is None or not (
                    0.0 <= projected.u < width and 0.0 <= projected.v < height
                ):
                    continue
                anchor_x = projected.u
                anchor_y = projected.v
                x = anchor_x + float(offset[0])
                y = anchor_y + float(offset[1])
            if x is None or y is None or anchor_x is None or anchor_y is None:
                continue
            raw_content = annotation.get("content")
            content = (
                {
                    "title": str(raw_content.get("title", "")),
                    "body": str(raw_content.get("body", annotation.get("text", ""))),
                }
                if isinstance(raw_content, Mapping)
                else {"title": "", "body": str(annotation.get("text", ""))}
            )
            events.append(
                AnnotationRenderEvent(
                    annotation_id=str(annotation["annotation_id"]),
                    source_pts=source_pts,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    x=x,
                    y=y,
                    anchor_x=anchor_x,
                    anchor_y=anchor_y,
                    text=content["body"],
                    content=content,
                    panel=(
                        annotation["panel"]
                        if isinstance(annotation.get("panel"), Mapping)
                        else {}
                    ),
                    leader=(
                        annotation["leader"]
                        if isinstance(annotation.get("leader"), Mapping)
                        else {}
                    ),
                    style=(
                        annotation["style"]
                        if isinstance(annotation.get("style"), Mapping)
                        else {}
                    ),
                )
            )
    return tuple(events)


def _rgba(
    value: object, default: str, *, opacity: float = 1.0
) -> tuple[int, int, int, int]:
    text = str(value or default).lstrip("#")
    if len(text) == 6:
        text += "FF"
    if len(text) != 8:
        text = default.lstrip("#")
        if len(text) == 6:
            text += "FF"
    alpha = round(int(text[6:8], 16) * max(0.0, min(1.0, float(opacity))))
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha


@lru_cache(maxsize=128)
def _font(font_family: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = resolve_font_file(font_family)
    if path is not None:
        try:
            return ImageFont.truetype(str(path), size=size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    width: float,
) -> list[str]:
    lines: list[str] = []
    for logical in value.replace("\r\n", "\n").split("\n"):
        if not logical:
            lines.append("")
            continue
        current = ""
        for character in logical:
            candidate = current + character
            if current and draw.textlength(candidate, font=font) > width:
                lines.append(current)
                current = character
            else:
                current = candidate
        lines.append(current)
    return lines or [""]


def _setting(values: Mapping[str, object], key: str, default: object) -> object:
    value = values.get(key)
    return default if value is None else value


def render_callout_overlay(
    events: Sequence[AnnotationRenderEvent], *, width: int, height: int
) -> Image.Image:
    """按与浏览器一致的布局语义绘制透明 RGBA 工程标牌图层。"""

    image = Image.new("RGBA", (int(width), int(height)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image, "RGBA")
    for event in events:
        style = event.style
        panel = event.panel
        leader = event.leader
        body_size = int(style.get("font_size_px") or 28)
        font_family = str(style.get("font_family") or "sans-serif")
        title_font = _font(font_family, min(128, body_size + 2))
        body_font = _font(font_family, body_size)
        panel_width = float(_setting(panel, "width_px", 320))
        padding = float(_setting(panel, "padding_px", 16))
        text_width = max(1.0, panel_width - 2.0 * padding)
        title = str(event.content.get("title", ""))
        body = str(event.content.get("body", event.text))
        title_lines = _wrap_text(draw, title, title_font, text_width) if title else []
        body_lines = _wrap_text(draw, body, body_font, text_width)
        title_height = body_size + 6
        body_height = body_size + 5
        gap = 8 if title_lines and body_lines else 0
        panel_height = (
            2.0 * padding
            + len(title_lines) * title_height
            + gap
            + len(body_lines) * body_height
        )
        layout = layout_callout(
            anchor_xy=(event.anchor_x, event.anchor_y),
            screen_offset=(event.x - event.anchor_x, event.y - event.anchor_y),
            panel_size=(panel_width, panel_height),
            viewport_size=(float(width), float(height)),
            safe_margin=float(_setting(panel, "safe_margin_px", 20)),
            elbow_length=float(_setting(leader, "elbow_length_px", 24)),
        )
        border = _rgba(style.get("border_color"), "#FFFFFFCC")
        line_width = int(_setting(leader, "line_width_px", 2))
        draw.line(layout.leader_points, fill=border, width=line_width, joint="curve")
        radius = float(_setting(leader, "anchor_radius_px", 6))
        anchor_box = (
            event.anchor_x - radius,
            event.anchor_y - radius,
            event.anchor_x + radius,
            event.anchor_y + radius,
        )
        if leader.get("anchor_shape") == "crosshair":
            draw.line(
                (
                    (event.anchor_x - radius, event.anchor_y),
                    (event.anchor_x + radius, event.anchor_y),
                ),
                fill=border,
                width=line_width,
            )
            draw.line(
                (
                    (event.anchor_x, event.anchor_y - radius),
                    (event.anchor_x, event.anchor_y + radius),
                ),
                fill=border,
                width=line_width,
            )
        else:
            draw.ellipse(anchor_box, outline=border, width=line_width)

        left, top, panel_width, panel_height = layout.panel_rect
        rectangle = (left, top, left + panel_width, top + panel_height)
        corner = int(_setting(panel, "border_radius_px", 6))
        if panel.get("shadow", True):
            shadow = (left + 5, top + 6, left + panel_width + 5, top + panel_height + 6)
            draw.rounded_rectangle(shadow, radius=corner, fill=(0, 0, 0, 90))
        background = _rgba(
            style.get("background_color"),
            "#000000B3",
            opacity=float(style.get("background_opacity", 0.7)),
        )
        draw.rounded_rectangle(
            rectangle,
            radius=corner,
            fill=background,
            outline=border,
            width=max(1, line_width),
        )
        cursor_y = top + padding
        title_color = _rgba(style.get("title_color"), "#69D2FFFF")
        text_color = _rgba(style.get("text_color"), "#FFFFFFFF")
        for line in title_lines:
            draw.text(
                (left + padding, cursor_y), line, font=title_font, fill=title_color
            )
            cursor_y += title_height
        if title_lines and body_lines:
            cursor_y += gap
        for line in body_lines:
            draw.text((left + padding, cursor_y), line, font=body_font, fill=text_color)
            cursor_y += body_height
    return image


def build_overlay_concat_document(
    paths: Sequence[Path],
    *,
    source_frames: Sequence[Mapping[str, object]],
    time_base_numerator: int,
    time_base_denominator: int,
) -> str:
    if len(paths) != len(source_frames):
        raise ValueError("overlay images must match authoritative source frames")
    time_base = Fraction(int(time_base_numerator), int(time_base_denominator))
    frame_rate = Fraction(time_base.denominator, time_base.numerator)
    frame_rate_text = (
        str(frame_rate.numerator)
        if frame_rate.denominator == 1
        else f"{frame_rate.numerator}/{frame_rate.denominator}"
    )
    lines = ["ffconcat version 1.0"]
    for index, (path, frame) in enumerate(zip(paths, source_frames)):
        escaped = str(path.resolve()).replace("\\", "/").replace("'", "'\\''")
        duration_pts = int(frame.get("duration_pts") or 0)
        if duration_pts <= 0:
            current_pts = int(frame["source_pts"])
            if index + 1 < len(source_frames):
                duration_pts = int(source_frames[index + 1]["source_pts"]) - current_pts
            elif index > 0:
                duration_pts = current_pts - int(source_frames[index - 1]["source_pts"])
            else:
                duration_pts = 1
        if duration_pts <= 0:
            raise ValueError("annotation overlay frame PTS must be strictly increasing")
        lines.append(f"file '{escaped}'")
        lines.append(f"option framerate {frame_rate_text}")
        lines.append(f"duration {float(duration_pts * time_base):.12f}")
    if paths:
        # concat demuxer 只有看到下一项时才会兑现最后一帧的 duration。
        escaped = str(paths[-1].resolve()).replace("\\", "/").replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
        lines.append(f"option framerate {frame_rate_text}")
    return "\n".join(lines) + "\n"


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
                    "0",
                    "0",
                    "0",
                    "100",
                    "100",
                    "0",
                    "0",
                    "3",
                    "1",
                    "0",
                    "5",
                    "0",
                    "0",
                    "0",
                    "1",
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
            if next_event is None or abs(next_event.start_sec - event.end_sec) > 1e-12:
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
