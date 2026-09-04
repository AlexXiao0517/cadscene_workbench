from __future__ import annotations

from pathlib import Path

import pytest

from cadscene.srt.bentley_pose_merge import load_bentley_pose_samples


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
