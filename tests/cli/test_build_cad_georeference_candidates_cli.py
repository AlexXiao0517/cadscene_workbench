from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.cli.build_cad_georeference_candidates import main


def _write_srt(path: Path) -> Path:
    path.write_text(
        "\n\n".join(
            (
                "1\n00:00:00,000 --> 00:00:01,000\n"
                "[latitude: 30.0] [longitude: 119.999] [rel_alt: 10.0]",
                "2\n00:00:01,000 --> 00:00:02,000\n"
                "[latitude: 30.001] [longitude: 120.001] [rel_alt: 11.0]",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_candidate_cli_publishes_fingerprinted_candidates_and_progress(
    tmp_path: Path,
) -> None:
    srt = _write_srt(tmp_path / "flight.srt")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm_version": "1",
                "input_fingerprint": "candidate-input-fingerprint",
                "srt_path": str(srt),
                "cad_bbox_raw": [
                    499_800.0,
                    3_319_900.0,
                    500_200.0,
                    3_320_400.0,
                ],
                "central_meridian_deg": 120.0,
                "limit": 6,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "attempt" / "cad_georeference_candidates.json"
    progress = tmp_path / "attempt" / "adapter_progress.json"

    result = main(
        [
            "--request",
            str(request),
            "--output",
            str(output),
            "--progress-file",
            str(progress),
        ]
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    progress_payload = json.loads(progress.read_text(encoding="utf-8"))
    assert result == 0
    assert payload["schema_version"] == 1
    assert payload["algorithm_version"] == "1"
    assert payload["input_fingerprint"] == "candidate-input-fingerprint"
    assert payload["central_meridian_deg"] == 120.0
    assert payload["candidates"]
    assert {item["central_meridian_deg"] for item in payload["candidates"]} == {
        120.0
    }
    assert progress_payload["stage"] == "complete"
    assert progress_payload["fraction"] == 1.0


def test_candidate_cli_normalizes_degree_minute_custom_projection(
    tmp_path: Path,
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm_version": "1",
                "input_fingerprint": "custom-candidate-input",
                "srt_path": str(_write_srt(tmp_path / "flight.srt")),
                "cad_bbox_raw": [0.0, 0.0, 1.0, 1.0],
                "central_meridian_deg": "118°50′",
                "limit": 6,
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "attempt" / "cad_georeference_candidates.json"

    result = main(
        [
            "--request",
            str(request),
            "--output",
            str(output),
            "--progress-file",
            str(tmp_path / "attempt" / "progress.json"),
        ]
    )

    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["central_meridian_deg"] == pytest.approx(118.0 + 50.0 / 60.0)
    assert payload["candidates"][0]["crs_source"] == "custom"
    assert payload["candidates"][0]["epsg"] is None


def test_candidate_cli_rejects_request_without_matching_fingerprint(
    tmp_path: Path,
) -> None:
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "algorithm_version": "1",
                "input_fingerprint": "",
                "srt_path": str(_write_srt(tmp_path / "flight.srt")),
                "cad_bbox_raw": [0.0, 0.0, 1.0, 1.0],
                "central_meridian_deg": 120.0,
                "limit": 6,
            }
        ),
        encoding="utf-8",
    )

    result = main(
        [
            "--request",
            str(request),
            "--output",
            str(tmp_path / "output.json"),
            "--progress-file",
            str(tmp_path / "progress.json"),
        ]
    )

    assert result == 1
    assert not (tmp_path / "output.json").exists()
