from __future__ import annotations

import json
from pathlib import Path
import struct

import numpy as np

from cadscene.dji import metadata


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    result = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        result.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(result)


def _field(number: int, wire: int, value: bytes | int) -> bytes:
    key = _varint((number << 3) | wire)
    if wire == 0:
        return key + _varint(int(value))
    if wire == 2:
        raw = bytes(value)
        return key + _varint(len(raw)) + raw
    if wire == 5:
        return key + bytes(value)
    raise AssertionError(wire)


def _message(number: int, value: bytes) -> bytes:
    return _field(number, 2, value)


def _pose_packet(yaw_tenths: int, gimbal_pitch_tenths: int) -> bytes:
    attitude = b"".join(
        (_field(1, 0, 0), _field(2, 0, 0), _field(3, 0, yaw_tenths))
    )
    aircraft = _message(3, attitude)
    gimbal = b"".join((_field(1, 0, gimbal_pitch_tenths), _field(3, 0, 0)))
    quaternion = b"".join(
        _field(index, 5, struct.pack("<f", value))
        for index, value in enumerate((0.0, 0.0, 0.0, 1.0), start=1)
    )
    camera = _message(3, gimbal) + _message(4, quaternion)
    return _message(3, _message(3, aircraft) + _message(4, camera))


def test_load_dji_pose_priors_discovers_djmd_stream_and_decodes_packets(
    tmp_path: Path, monkeypatch
) -> None:
    packets = [_pose_packet(100, -900), _pose_packet(120, -850)]
    video = tmp_path / "flight.mp4"
    video.write_bytes(b"".join(packets))

    def fake_check_output(command, **_kwargs):
        if "-show_streams" in command:
            return json.dumps(
                {"streams": [{"index": 0, "codec_type": "video"}, {"index": 2, "codec_type": "data", "codec_tag_string": "djmd"}]}
            )
        return json.dumps(
            {
                "packets": [
                    {"pos": 0, "size": len(packets[0]), "pts_time": "0.0"},
                    {"pos": len(packets[0]), "size": len(packets[1]), "pts_time": "0.033"},
                ]
            }
        )

    monkeypatch.setattr(metadata.subprocess, "check_output", fake_check_output)
    result = metadata.load_dji_pose_priors(video, [0, 1])

    assert result.available is True
    assert result.packet_count == 2
    assert result.source_ordinals.tolist() == [0, 1]
    assert result.world_from_camera.shape == (2, 3, 3)
    assert result.convention == "Bentley XRightYDown"


def test_unsupported_metadata_layout_is_reported_not_invented(
    tmp_path: Path, monkeypatch
) -> None:
    video = tmp_path / "plain.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(
        metadata.subprocess,
        "check_output",
        lambda *_args, **_kwargs: json.dumps({"streams": [{"index": 0, "codec_type": "video"}]}),
    )

    result = metadata.load_dji_pose_priors(video, [0])

    assert result.available is False
    assert result.world_from_camera.shape == (0, 3, 3)
    assert "djmd" in result.reason.lower()
