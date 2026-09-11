from __future__ import annotations

from pathlib import Path

from cadscene.cad.text_annotations import (
    clean_dxf_text,
    load_design_text_annotations,
    load_dxf_text_annotations,
)


def test_utf8_dxf_text_and_unicode_escapes_are_preserved(tmp_path: Path) -> None:
    path = tmp_path / "labels.dxf"
    path.write_text(
        "0\nTEXT\n10\n100\n20\n200\n40\n2.5\n1\n桥梁\\P\\U+4E2D\\U+5FC3\n0\nEOF\n",
        encoding="utf-8",
    )

    result = load_dxf_text_annotations(path, origin_xy=(90.0, 180.0), cad_scale=2.0)

    assert result.labels == ("桥梁 中心",)
    assert result.points_xy.tolist() == [[20.0, 40.0]]
    assert clean_dxf_text("里程%%d") == "里程°"


def test_design_text_annotations_keep_unicode_and_transform_cad_world(tmp_path: Path) -> None:
    path = tmp_path / "design.json"
    path.write_text(
        '{"meta":{"coordinate_mode":"cad_world"},"layers":[{"entities":['
        '{"entity_type":"MTEXT","text":"桥梁中心","world_position":[110,220,0],'
        '"cad_height":2.5,"cad_rotation":30}]}]}',
        encoding="utf-8",
    )

    result = load_design_text_annotations(
        path, origin_xy=(100.0, 200.0), cad_scale=2.0
    )

    assert result.labels == ("桥梁中心",)
    assert result.points_xy.tolist() == [[20.0, 40.0]]
    assert result.heights_m.tolist() == [5.0]
