from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from cadscene.cli.merge_bentley_pose_srt import main
from cadscene.srt.parser import analyze_srt_stream


def _write_srt(path: Path) -> Path:
    blocks = []
    for index in range(3):
        blocks.append(
            f"{index + 1}\n"
            f"00:00:0{index},000 --> 00:00:0{index},100\n"
            f"[latitude: 30.100000] [longitude: {118.8 + index * 0.0001:.6f}] "
            f"[rel_alt: {80.0 + index:.3f} abs_alt: {230.0 + index * 0.1:.3f}]"
        )
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8", newline="")
    return path


def _write_xml(path: Path) -> Path:
    photos = []
    for index, frame in enumerate((0, 2)):
        photos.append(
            f"""
            <Photo>
              <ImagePath>C:/frames/frame_{frame:06d}.jpg</ImagePath>
              <Pose>
                <Rotation><Yaw>{10 + index}</Yaw><Pitch>-45</Pitch><Roll>1</Roll></Rotation>
                <Metadata><Center><x>{118.8 + frame * 0.0001}</x><y>30.1</y><z>{230 + frame * 0.1}</z></Center></Metadata>
              </Pose>
            </Photo>
            """
        )
    path.write_text(
        "<BlocksExchange><Block><Photogroups><Photogroup>"
        + "".join(photos)
        + "</Photogroup></Photogroups></Block></BlocksExchange>",
        encoding="utf-8",
    )
    return path


def _arguments(
    source: Path,
    xml: Path,
    output: Path,
    report: Path,
) -> list[str]:
    return [
        "--srt",
        str(source),
        "--xml",
        str(xml),
        "--output",
        str(output),
        "--report",
        str(report),
        "--horizontal-fov-deg",
        "59.109",
    ]


def test_cli_writes_full_pose_srt_and_audited_report(tmp_path: Path) -> None:
    source = _write_srt(tmp_path / "flight.srt")
    xml = _write_xml(tmp_path / "block.xml")
    output = tmp_path / "flight-full.srt"
    report = tmp_path / "flight-full.merge-report.json"

    assert main(_arguments(source, xml, output, report)) == 0

    with output.open("rb") as stream:
        analysis = analyze_srt_stream(stream, output.name)
    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["full_pose_coverage"] == pytest.approx(1.0)
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["horizontal_fov_deg"] == 59.109
    assert payload["source_block_count"] == 3
    assert payload["xml_sample_count"] == 2
    assert payload["source_srt_sha256"] == sha256(source.read_bytes()).hexdigest()
    assert payload["source_xml_sha256"] == sha256(xml.read_bytes()).hexdigest()
    assert payload["output_srt_sha256"] == sha256(output.read_bytes()).hexdigest()


def test_cli_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    source = _write_srt(tmp_path / "flight.srt")
    xml = _write_xml(tmp_path / "block.xml")
    output = tmp_path / "existing.srt"
    report = tmp_path / "existing.merge-report.json"
    output.write_text("keep", encoding="utf-8")

    assert main(_arguments(source, xml, output, report)) == 1

    assert output.read_text(encoding="utf-8") == "keep"
    assert not report.exists()


def test_cli_does_not_publish_srt_when_report_destination_exists(
    tmp_path: Path,
) -> None:
    source = _write_srt(tmp_path / "flight.srt")
    xml = _write_xml(tmp_path / "block.xml")
    output = tmp_path / "flight-full.srt"
    report = tmp_path / "existing.merge-report.json"
    report.write_text("keep", encoding="utf-8")

    assert main(_arguments(source, xml, output, report)) == 1

    assert not output.exists()
    assert report.read_text(encoding="utf-8") == "keep"
