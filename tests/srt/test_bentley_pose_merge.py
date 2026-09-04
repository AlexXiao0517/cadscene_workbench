from __future__ import annotations

from io import BytesIO
from pathlib import Path
import re

import pytest

from cadscene.srt.bentley_pose_merge import (
    BentleyPoseSample,
    load_bentley_pose_samples,
    merge_bentley_orientations_into_srt,
)
from cadscene.srt.parser import analyze_srt_stream


def _write_bentley_xml(path: Path, *, frames: tuple[int, ...]) -> Path:
    photos: list[str] = []
    for index, frame in enumerate(frames):
        photos.append(
            f"""
            <Photo>
              <Id>{index}</Id>
              <ImagePath>C:/frames/frame_{frame:06d}.jpg</ImagePath>
              <Pose>
                <Rotation>
                  <Yaw>{-100.0 - index}</Yaw>
                  <Pitch>{-45.0 + index}</Pitch>
                  <Roll>{1.5 + index}</Roll>
                </Rotation>
                <Center><x>999</x><y>998</y><z>997</z></Center>
                <Metadata>
                  <Center>
                    <x>{118.8 + index * 0.0001}</x>
                    <y>30.1</y>
                    <z>{230.0 + index * 0.1}</z>
                  </Center>
                </Metadata>
              </Pose>
            </Photo>
            """
        )
    path.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
        <BlocksExchange version="3.2">
          <Block><Photogroups><Photogroup>
        """
        + "".join(photos)
        + "</Photogroup></Photogroups></Block></BlocksExchange>",
        encoding="utf-8",
    )
    return path


def _srt_text(block_count: int, *, newline: str = "\n") -> str:
    blocks = []
    for index in range(block_count):
        block = (
            f"{index + 1}\n"
            f"00:00:0{index},000 --> 00:00:0{index},100\n"
            f"<font>[latitude: 30.100000] "
            f"[longitude: {118.8 + index * 0.0001:.6f}] "
            f"[rel_alt: {80.0 + index:.3f} abs_alt: {230.0 + index * 0.1:.3f}]</font>"
        )
        blocks.append(block.replace("\n", newline))
    return (newline * 2).join(blocks) + newline


def _parse_text(text: str):
    analysis = analyze_srt_stream(BytesIO(text.encode("utf-8")), "merged.srt")
    return analysis, analysis["records"]


def _remove_camera_tags(text: str) -> str:
    return re.sub(
        r" \[camera_yaw: [-+0-9.]+\]"
        r" \[camera_pitch: [-+0-9.]+\]"
        r" \[camera_roll: [-+0-9.]+\]",
        "",
        text,
    )


def test_load_bentley_pose_samples_uses_image_frame_rotation_and_metadata(
    tmp_path: Path,
) -> None:
    xml = _write_bentley_xml(tmp_path / "block.xml", frames=(0, 120))

    samples = load_bentley_pose_samples(xml)

    assert [item.frame_index for item in samples] == [0, 120]
    assert samples[0].yaw_deg == pytest.approx(-100.0)
    assert samples[0].pitch_deg == pytest.approx(-45.0)
    assert samples[0].roll_deg == pytest.approx(1.5)
    assert samples[0].metadata_lon_lat_alt == pytest.approx((118.8, 30.1, 230.0))


def test_load_bentley_pose_samples_rejects_duplicate_frames(tmp_path: Path) -> None:
    xml = _write_bentley_xml(tmp_path / "duplicate.xml", frames=(120, 120))

    with pytest.raises(ValueError, match="unique.*frame"):
        load_bentley_pose_samples(xml)


def test_load_bentley_pose_samples_rejects_unparseable_image_frame(
    tmp_path: Path,
) -> None:
    xml = _write_bentley_xml(tmp_path / "invalid.xml", frames=(0,))
    xml.write_text(
        xml.read_text(encoding="utf-8").replace("frame_000000.jpg", "camera.jpg"),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="frame index"):
        load_bentley_pose_samples(xml)


def test_merge_preserves_original_text_and_adds_shortest_arc_slerped_pose() -> None:
    source = _srt_text(3, newline="\r\n")
    samples = (
        BentleyPoseSample(0, 170.0, -45.0, 0.0, (118.8, 30.1, 230.0)),
        BentleyPoseSample(2, -170.0, -45.0, 0.0, (118.8002, 30.1, 230.2)),
    )

    result = merge_bentley_orientations_into_srt(source, samples)

    assert _remove_camera_tags(result.text) == source
    analysis, records = _parse_text(result.text)
    assert len(records) == 3
    assert abs(abs(records[1]["gimbal_yaw"]) - 180.0) < 1e-6
    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["full_pose_coverage"] == pytest.approx(1.0)
    assert result.report["horizontal_fov_deg"] == 59.109


def test_merge_holds_last_pose_after_final_xml_sample() -> None:
    source = _srt_text(4)
    samples = (
        BentleyPoseSample(0, 10.0, -45.0, 1.0, (118.8, 30.1, 230.0)),
        BentleyPoseSample(2, 20.0, -44.0, 2.0, (118.8002, 30.1, 230.2)),
    )

    result = merge_bentley_orientations_into_srt(source, samples)

    _, records = _parse_text(result.text)
    for field in ("gimbal_yaw", "gimbal_pitch", "gimbal_roll"):
        assert records[3][field] == pytest.approx(records[2][field])
    assert result.report["held_tail_count"] == 1


def test_merge_rejects_xml_metadata_that_disagrees_with_srt_position() -> None:
    samples = (
        BentleyPoseSample(0, 0.0, -45.0, 0.0, (119.8, 30.1, 230.0)),
    )

    with pytest.raises(ValueError, match="metadata.*SRT"):
        merge_bentley_orientations_into_srt(_srt_text(3), samples)


def test_merge_rejects_existing_camera_attitude_tags() -> None:
    source = _srt_text(2).replace(
        "</font>",
        " [camera_yaw: 0] [camera_pitch: -45] [camera_roll: 0]</font>",
        1,
    )
    samples = (
        BentleyPoseSample(0, 0.0, -45.0, 0.0, (118.8, 30.1, 230.0)),
    )

    with pytest.raises(ValueError, match="already contains camera attitude"):
        merge_bentley_orientations_into_srt(source, samples)
