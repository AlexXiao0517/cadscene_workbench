from __future__ import annotations

import json
from pathlib import Path

import pytest

from cadscene.video_analysis.artifacts import REQUIRED_ARTIFACTS, publish_analysis_revision


def _valid_payloads(revision: str) -> dict[str, str]:
    return {
        "video_analysis_manifest.json": json.dumps(
            {"schema_version": 1, "analysis_revision": revision, "status": "complete"}
        ),
        "video_metadata.json": json.dumps(
            {"source_start_pts_sec": 0.0, "source_end_pts_sec": 7.25}
        ),
        "analysis_windows.csv": "start_pts_sec,end_pts_sec,motion_mode,confidence\n0.0,7.25,unknown,0.4\n",
        "detected_boundaries.json": json.dumps({"boundaries": []}),
        "clip_manifest.json": json.dumps(
            {
                "analysis_revision": revision,
                "clips": [
                    {
                        "clip_id": "clip-0001",
                        "source_start_pts_sec": 0.0,
                        "source_end_pts_sec": 7.25,
                    }
                ],
            }
        ),
        "video_analysis_report.md": f"# Analysis {revision}\n",
    }


def test_publish_writes_required_root_artifacts_and_immutable_revision(tmp_path: Path) -> None:
    output = tmp_path / "02_video_analysis"

    published = publish_analysis_revision(output, "analysis-0001", _valid_payloads("analysis-0001"))

    assert published == output / "analysis_revisions" / "analysis-0001"
    assert set(REQUIRED_ARTIFACTS) == {path.name for path in published.iterdir()}
    assert all((output / name).is_file() for name in REQUIRED_ARTIFACTS)


def test_reanalysis_keeps_old_revision_and_manual_overrides(tmp_path: Path) -> None:
    output = tmp_path / "02_video_analysis"
    first = publish_analysis_revision(output, "analysis-0001", _valid_payloads("analysis-0001"))
    manual = output / "manual_overrides" / "workflow_overrides.json"
    manual.parent.mkdir()
    manual.write_text('{"clip-0001": "pure_rotation"}', encoding="utf-8")

    second = publish_analysis_revision(output, "analysis-0002", _valid_payloads("analysis-0002"))

    assert first.is_dir() and second.is_dir()
    assert json.loads((first / "video_analysis_manifest.json").read_text(encoding="utf-8"))[
        "analysis_revision"
    ] == "analysis-0001"
    assert manual.read_text(encoding="utf-8") == '{"clip-0001": "pure_rotation"}'
    assert json.loads((output / "video_analysis_manifest.json").read_text(encoding="utf-8"))[
        "analysis_revision"
    ] == "analysis-0002"


def test_invalid_staged_outputs_are_not_published(tmp_path: Path) -> None:
    output = tmp_path / "02_video_analysis"
    payloads = _valid_payloads("analysis-0001")
    del payloads["clip_manifest.json"]

    with pytest.raises(ValueError, match="missing required artifacts"):
        publish_analysis_revision(output, "analysis-0001", payloads)

    assert not output.exists()


def test_publication_rejects_clip_over_hard_sixty_second_limit(tmp_path: Path) -> None:
    output = tmp_path / "02_video_analysis"
    payloads = _valid_payloads("analysis-0001")
    clip_manifest = json.loads(payloads["clip_manifest.json"])
    clip_manifest["clips"][0]["source_end_pts_sec"] = 60.001
    payloads["clip_manifest.json"] = json.dumps(clip_manifest)

    with pytest.raises(ValueError, match="exceeds 60"):
        publish_analysis_revision(output, "analysis-0001", payloads)

    assert not output.exists()

