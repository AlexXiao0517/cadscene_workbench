from __future__ import annotations

import io
from pathlib import Path

from cadscene.workflow.data_import import (
    create_dataset,
    import_srt,
    load_dataset_manifest,
    set_no_srt_motion_mode,
)


SRT_FIXTURES = Path(__file__).parents[1] / "fixtures" / "srt"


def test_explicit_hovering_without_srt_routes_to_experimental_pure_rotation(tmp_path: Path) -> None:
    create_dataset(tmp_path, "hover")

    manifest = set_no_srt_motion_mode(tmp_path, "hover", hovering_declared=True)

    workflow = manifest["workflow"]
    assert workflow["trajectory_mode"] == "pure_rotation"
    assert workflow["motion_mode"] == "hovering_rotation"
    assert workflow["motion_mode_source"] == "user_selected"
    assert workflow["hovering_declared"] is True
    assert workflow["experimental"] is True
    assert workflow["translation_observable"] is False
    assert workflow["absolute_orientation_observable"] is False
    assert workflow["intrinsics_verified"] is False


def test_default_and_false_hovering_without_srt_stay_sfm_only(tmp_path: Path) -> None:
    default_manifest = create_dataset(tmp_path, "default")
    create_dataset(tmp_path, "explicit")
    explicit_manifest = set_no_srt_motion_mode(tmp_path, "explicit", hovering_declared=False)

    assert default_manifest["workflow"]["trajectory_mode"] == "sfm_only"
    assert default_manifest["workflow"]["hovering_declared"] is False
    assert explicit_manifest["workflow"]["trajectory_mode"] == "sfm_only"
    assert explicit_manifest["workflow"]["motion_mode"] == "general_motion"


def test_srt_routing_overrides_hovering_selection(tmp_path: Path) -> None:
    create_dataset(tmp_path, "srt")
    set_no_srt_motion_mode(tmp_path, "srt", hovering_declared=True)

    manifest = import_srt(
        tmp_path,
        "srt",
        "partial.srt",
        io.BytesIO((SRT_FIXTURES / "no_attitude_partial.srt").read_bytes()),
    )

    assert manifest["workflow"]["trajectory_mode"] == "srt_sfm_fused"
    assert manifest["workflow"]["hovering_declared"] is True
    assert load_dataset_manifest(tmp_path, "srt")["workflow"]["trajectory_mode"] == "srt_sfm_fused"


def test_legacy_manifest_without_motion_fields_is_normalized_to_sfm_only(tmp_path: Path) -> None:
    create_dataset(tmp_path, "legacy")
    path = tmp_path / "data" / "legacy" / "dataset_manifest.json"
    payload = path.read_text(encoding="utf-8")
    path.write_text(payload.replace(',\n    "debug_override": null', ''), encoding="utf-8")

    manifest = load_dataset_manifest(tmp_path, "legacy")

    assert manifest["workflow"]["trajectory_mode"] == "sfm_only"
    assert manifest["workflow"]["hovering_declared"] is False
    assert manifest["workflow"]["motion_mode"] == "general_motion"
