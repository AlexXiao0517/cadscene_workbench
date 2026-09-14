from fractions import Fraction
import csv
import json

import numpy as np
import pytest

from cadscene.srt import full_pose_render_assets as subject
from cadscene.srt.full_pose import FullPoseBuildConfig
from cadscene.srt.georeference import CadGeoreference
from cadscene.terrain.tpkg import TerrainControlSet, TerrainSourceSummary


def _controls(source_id):
    source = TerrainSourceSummary(source_id, "test.tpkg", 2, 2, {"PointZ": 2}, {"terrain": 2}, (120, 30, 120, 30), (120, 122))
    return TerrainControlSet(
        points_xyz=np.array([[0., 0., 120.], [10., 0., 122.]]),
        point_source_ids=(source_id, source_id), point_labels=("一", "二"),
        point_colors_bgr=np.zeros((2, 3), dtype=np.uint8),
        segment_starts_xyz=np.empty((0, 3)), segment_ends_xyz=np.empty((0, 3)),
        segment_source_ids=(), segment_colors_bgr=np.empty((0, 3)), segment_widths=np.empty(0),
        sources=(source,), fingerprint=source_id,
    )


@pytest.mark.parametrize("with_terrain", [True, False])
@pytest.mark.parametrize("absolute_height", [194.174, None, float("nan")])
@pytest.mark.parametrize("offset", [0, 7])
@pytest.mark.parametrize("smoothed", [True, False])
def test_render_assets_choose_height_to_match_terrain(tmp_path, monkeypatch, with_terrain, absolute_height, offset, smoothed):
    georef = CadGeoreference.from_dict({
        "schema_version": 1, "horizontal_datum": "CGCS2000",
        "projection_family": "gauss_kruger", "zone_width_deg": 3,
        "projected_axis_order": "easting_northing",
        "cad_axis_mapping": "cad_x_easting_cad_y_northing",
        "zone_prefix": False, "linear_unit": "metre",
        "central_meridian_deg": 120, "epsg": 4549, "confirmed": True,
        "source": "user_confirmed", "confidence": 1,
    })
    config = FullPoseBuildConfig(
        clip_id="clip-1", source_start_pts=0, source_end_pts_exclusive=1001,
        source_time_base=Fraction(1, 1000), georeference=georef,
        cad_origin_xy=(500000, 3320113.3978450196), cad_scale=1,
        horizontal_fov_deg=84, cad_z_offset_m=offset,
    )
    trajectory = {
        "width": 1920, "height": 1080,
        "poses": [{"frame_index": 0, "registered": True, "center": [0., 0., 50. + offset], "source_pts": 0, "abs_alt": absolute_height}],
        "meta": {"warnings": []},
    }
    if smoothed and absolute_height is not None and np.isfinite(absolute_height):
        trajectory['poses'][0]['smoothed_abs_alt'] = absolute_height + .25
    (tmp_path / "georeference_diagnostics.json").write_text("{}")
    (tmp_path / "camera_path_full_pose.csv").write_text(f"frame_index,camera_z\n0,{50 + offset}\n")
    monkeypatch.setattr(subject, "load_tpkg", lambda path, **kwargs: _controls("a" * 64 if path.name == "a.tpkg" else "b" * 64))
    payload = {
        "terrain_source_paths": ["a.tpkg", "b.tpkg"] if with_terrain else [],
        "terrain_source_fingerprints": ["a" * 64, "b" * 64] if with_terrain else [],
        "cad_asset_fingerprint": "cad",
    }
    subject.publish_full_pose_render_assets(tmp_path, trajectory, config, payload)
    context = json.loads((tmp_path / "terrain_context.json").read_text(encoding="utf-8"))
    uses_absolute = with_terrain and absolute_height is not None and np.isfinite(absolute_height)
    expected_z = (absolute_height + (.25 if smoothed else 0) if uses_absolute else 50) + offset
    assert trajectory["poses"][0]["center"][2] == expected_z
    with (tmp_path / "camera_path_full_pose.csv").open(encoding="utf-8-sig") as stream:
        assert float(next(csv.DictReader(stream))["camera_z"]) == expected_z
    assert context["camera_height_datum_valid"] is False
    assert context["terrain_mode"] == ("terrain" if uses_absolute else "relative")
    assert len(context["terrain_source_fingerprints"]) == (2 if with_terrain else 0)
    if with_terrain:
        assert context["terrain_coverage"] == 1.0
        assert context["cad_fallback_ground_m"] == (121 if uses_absolute else 0)
        assert context["terrain_warnings"]
    camera = json.loads((tmp_path / "camera_calibration.json").read_text())
    assert camera["focal_px"] == pytest.approx(1920 / (2 * np.tan(np.deg2rad(42))))
    assert camera["distortion_source"] == "unknown_assumed_zero"


def test_rejects_unpaired_terrain_fingerprints(tmp_path):
    with pytest.raises(ValueError, match="matching lists"):
        subject.publish_full_pose_render_assets(tmp_path, {}, None, {"terrain_source_paths": ["x.tpkg"]})
