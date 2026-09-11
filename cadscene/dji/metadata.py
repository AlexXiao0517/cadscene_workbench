from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import struct
import subprocess
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class DjiPosePriorTrack:
    source_ordinals: np.ndarray
    world_from_camera: np.ndarray
    packet_count: int
    convention: str = "Bentley XRightYDown"
    available: bool = True
    reason: str = ""

    @classmethod
    def unavailable(cls, reason: str) -> "DjiPosePriorTrack":
        return cls(
            source_ordinals=np.empty(0, dtype=np.int64),
            world_from_camera=np.empty((0, 3, 3), dtype=np.float64),
            packet_count=0,
            available=False,
            reason=str(reason),
        )


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 70:
            break
    raise ValueError("invalid protobuf varint")


def _wire_fields(data: bytes) -> dict[int, list[tuple[int, object]]]:
    fields: dict[int, list[tuple[int, object]]] = {}
    offset = 0
    while offset < len(data):
        key, offset = _varint(data, offset)
        number, wire_type = key >> 3, key & 7
        if number <= 0:
            raise ValueError("invalid protobuf field number")
        if wire_type == 0:
            value, offset = _varint(data, offset)
        elif wire_type == 1:
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = _varint(data, offset)
            value = data[offset : offset + length]
            offset += length
        elif wire_type == 5:
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire_type}")
        if offset > len(data):
            raise ValueError("truncated protobuf field")
        fields.setdefault(number, []).append((wire_type, value))
    return fields


def _only_message(fields: dict[int, list[tuple[int, object]]], number: int) -> bytes:
    values = fields.get(number, [])
    if len(values) != 1 or values[0][0] != 2:
        raise ValueError(f"expected one message in protobuf field {number}")
    return bytes(values[0][1])


def _fixed32_float(fields: dict[int, list[tuple[int, object]]], number: int) -> float:
    values = fields.get(number, [])
    if len(values) != 1 or values[0][0] != 5:
        raise ValueError(f"expected one fixed32 in protobuf field {number}")
    return float(struct.unpack("<f", bytes(values[0][1]))[0])


def _signed64(value: object) -> int:
    integer = int(value)
    return integer - (1 << 64) if integer >= (1 << 63) else integer


def _optional_varint(fields: dict[int, list[tuple[int, object]]], number: int) -> int:
    values = fields.get(number, [])
    if not values:
        return 0
    if len(values) != 1 or values[0][0] != 0:
        raise ValueError(f"expected at most one varint in protobuf field {number}")
    return _signed64(values[0][1])


def decode_pose_fields(packet: bytes) -> tuple[np.ndarray, tuple[float, float, float], tuple[float, float]]:
    """Decode the guarded field layout observed in DJI Mavic djmd packets."""

    top = _wire_fields(packet)
    payload = _wire_fields(_only_message(top, 3))
    aircraft = _wire_fields(_only_message(payload, 3))
    aircraft_attitude = _wire_fields(_only_message(aircraft, 3))
    camera_pose = _wire_fields(_only_message(payload, 4))
    gimbal_attitude = _wire_fields(_only_message(camera_pose, 3))
    quaternion = _wire_fields(_only_message(camera_pose, 4))
    quaternion_value = np.asarray(
        [_fixed32_float(quaternion, number) for number in (1, 2, 3, 4)],
        dtype=np.float64,
    )
    aircraft_value = tuple(
        _optional_varint(aircraft_attitude, number) / 10.0 for number in (1, 2, 3)
    )
    gimbal_value = (
        _optional_varint(gimbal_attitude, 1) / 10.0,
        _optional_varint(gimbal_attitude, 3) / 10.0,
    )
    if not np.isfinite(quaternion_value).all():
        raise ValueError("DJI quaternion contains non-finite values")
    return quaternion_value, aircraft_value, gimbal_value


def bentley_world_from_camera(yaw_deg: float, pitch_deg: float, roll_deg: float = 0.0) -> np.ndarray:
    yaw, pitch, roll = np.radians([yaw_deg, pitch_deg, roll_deg])
    cy, sy = np.cos(yaw), np.sin(yaw)
    cp, sp = np.cos(pitch), np.sin(pitch)
    cr, sr = np.cos(roll), np.sin(roll)
    world_to_camera = np.asarray(
        [
            [cr * cy - sr * sp * sy, -cr * sy - cy * sr * sp, cp * sr],
            [cy * sr + cr * sp * sy, cr * cy * sp - sr * sy, -cr * cp],
            [cp * sy, cp * cy, sp],
        ],
        dtype=np.float64,
    )
    return world_to_camera.T


def _ffprobe_json(arguments: list[str]) -> dict:
    output = subprocess.check_output(
        ["ffprobe", "-v", "error", *arguments],
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return json.loads(output)


def load_dji_pose_priors(video: str | Path, source_ordinals: Sequence[int]) -> DjiPosePriorTrack:
    source = Path(video)
    ordinals = np.asarray(tuple(int(value) for value in source_ordinals), dtype=np.int64)
    try:
        streams = _ffprobe_json(["-show_streams", "-print_format", "json", str(source)]).get("streams", [])
        candidates = [
            int(stream["index"])
            for stream in streams
            if str(stream.get("codec_type", "")).lower() == "data"
            and "djmd" in " ".join(str(stream.get(key, "")) for key in ("codec_name", "codec_tag_string", "codec_long_name")).lower()
        ]
        if not candidates:
            return DjiPosePriorTrack.unavailable("DJI djmd data stream is unavailable")
        stream_index = candidates[0]
        packets = _ffprobe_json(
            ["-select_streams", str(stream_index), "-show_entries", "packet=pts_time,pos,size", "-print_format", "json", str(source)]
        ).get("packets", [])
        if len(packets) == 0 or np.any(ordinals < 0) or np.any(ordinals >= len(packets)):
            return DjiPosePriorTrack.unavailable("DJI metadata packet coverage does not match requested frames")
        rotations = []
        with source.open("rb") as stream:
            for ordinal in ordinals:
                packet = packets[int(ordinal)]
                stream.seek(int(packet["pos"]))
                raw = stream.read(int(packet["size"]))
                _quaternion, aircraft, gimbal = decode_pose_fields(raw)
                rotations.append(
                    bentley_world_from_camera(
                        yaw_deg=float(aircraft[2]),
                        pitch_deg=float(gimbal[0]),
                        roll_deg=0.0,
                    )
                )
        return DjiPosePriorTrack(
            source_ordinals=ordinals.copy(),
            world_from_camera=np.asarray(rotations, dtype=np.float64),
            packet_count=len(packets),
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        return DjiPosePriorTrack.unavailable(f"unsupported DJI metadata layout: {exc}")
