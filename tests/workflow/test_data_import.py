from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
import ezdxf

from cadscene.workflow.data_import import (
    create_dataset,
    import_cad,
    import_video,
    list_datasets,
    load_dataset_manifest,
    slugify_dataset_name,
)


def test_legacy_data_import_audit_is_documented() -> None:
    report = Path("docs/stage4c_legacy_data_import_audit.md")
    assert report.exists()
    text = report.read_text(encoding="utf-8")
    assert "paths.js" in text
    assert "design.json" in text
    assert "export_web_viewer_assets.py" in text
    assert "不迁移" in text


def _zip_bytes(files: dict[str, bytes]) -> io.BytesIO:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    stream.seek(0)
    return stream


def _dxf_bytes(tmp_path: Path, *, units: int = 6) -> io.BytesIO:
    path = tmp_path / "upload.dxf"
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    doc.layers.add("road_center", color=1)
    model = doc.modelspace()
    model.add_line((10, 20), (30, 20), dxfattribs={"layer": "road_center", "color": 1})
    model.add_lwpolyline([(10, 18), (30, 18)], dxfattribs={"layer": "road_edge", "color": 4})
    doc.saveas(path)
    return io.BytesIO(path.read_bytes())


def test_dataset_name_is_slugified_and_dataset_is_created(tmp_path: Path) -> None:
    assert slugify_dataset_name("  User Dataset 01  ") == "user-dataset-01"

    manifest = create_dataset(
        tmp_path,
        "User Dataset 01",
        cad_scale=0.06,
        origin_xy=(567747.5756295, 3330464.2234675),
    )

    assert manifest["dataset"] == "user-dataset-01"
    assert manifest["status"] == "incomplete"
    assert manifest["defaults"]["cad_scale"] == 0.06
    assert (tmp_path / "data/user-dataset-01/dataset_manifest.json").exists()


def test_video_import_streams_to_dataset_and_updates_manifest(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))

    manifest = import_video(tmp_path, "demo", "flight.mp4", io.BytesIO(b"video-bytes"))

    output = tmp_path / "data/demo/flight.mp4"
    assert output.read_bytes() == b"video-bytes"
    assert manifest["video"] == {
        "path": "data/demo/flight.mp4",
        "url": "/data/demo/flight.mp4",
        "original_name": "flight.mp4",
        "size_bytes": 11,
    }
    assert manifest["status"] == "incomplete"


def test_design_json_import_becomes_canonical_cad_asset(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))

    manifest = import_cad(tmp_path, "demo", "design.json", io.BytesIO(b'{"layers":[]}'))

    assert (tmp_path / "data/demo/design.json").exists()
    assert manifest["cad"]["source_type"] == "design_json"
    assert manifest["cad"]["status"] == "ready"
    assert manifest["cad"]["url"] == "/data/demo/design.json"


def test_assets_zip_extracts_safely_and_promotes_legacy_assets(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))
    archive = _zip_bytes(
        {
            "viewer/design.json": b'{"layers":[]}',
            "viewer/road_center.json": b'{"entities":[]}',
            "cad_meta.json": b"{}",
        }
    )

    manifest = import_cad(tmp_path, "demo", "cad_assets.zip", archive)

    assert (tmp_path / "data/demo/design.json").exists()
    assert (tmp_path / "data/demo/road_center.json").exists()
    assert manifest["cad"]["source_type"] == "assets_zip"
    assert manifest["cad"]["status"] == "ready"


def test_zip_slip_is_rejected_without_writing_outside_dataset(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))

    with pytest.raises(ValueError, match="unsafe zip member"):
        import_cad(tmp_path, "demo", "bad.zip", _zip_bytes({"../escaped.json": b"{}"}))

    assert not (tmp_path / "data/escaped.json").exists()


def test_dxf_is_parsed_and_updates_manifest_defaults(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))
    messages: list[str] = []

    manifest = import_cad(
        tmp_path,
        "demo",
        "drawing.dxf",
        _dxf_bytes(tmp_path),
        status_callback=messages.append,
    )

    assert (tmp_path / "data/demo/raw_cad/drawing.dxf").exists()
    assert (tmp_path / "data/demo/design.json").exists()
    assert (tmp_path / "data/demo/cad_import_report.md").exists()
    assert manifest["cad"]["source_type"] == "dxf_parsed"
    assert manifest["cad"]["status"] == "ready"
    assert manifest["cad"]["bbox"] == [10.0, 18.0, 30.0, 20.0]
    assert manifest["cad"]["entity_count"] == 2
    assert manifest["cad"]["layer_count"] == 2
    assert manifest["cad"]["aci_colors"] == [1, 4]
    assert manifest["cad"]["has_road_centerline"] is True
    assert manifest["cad"]["road_centerline_source"] == "layer"
    assert manifest["defaults"] == {"cad_scale": 1.0, "origin_xy": [10.0, 18.0]}
    assert messages == ["正在保存原始 CAD", "正在解析 DXF", "正在生成 design.json", "CAD 解析完成"]


def test_dwg_without_converter_is_saved_but_not_marked_ready(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CADSCENE_DWG_CONVERTER", raising=False)
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))

    manifest = import_cad(tmp_path, "demo", "drawing.dwg", io.BytesIO(b"raw-cad"))

    assert (tmp_path / "data/demo/raw_cad/drawing.dwg").exists()
    assert manifest["cad"]["source_type"] == "dwg_raw_saved"
    assert manifest["cad"]["status"] == "raw_saved"
    assert manifest["status"] == "incomplete"
    assert any("DWG" in warning and "转换工具" in warning for warning in manifest["warnings"])


def test_manifest_schema_and_dataset_listing(tmp_path: Path) -> None:
    create_dataset(tmp_path, "one", cad_scale=0.5, origin_xy=(3, 4))
    create_dataset(tmp_path, "two", cad_scale=1.0, origin_xy=(0, 0))

    manifest = load_dataset_manifest(tmp_path, "one")

    assert set(manifest) >= {"dataset", "created_at", "video", "cad", "defaults", "status", "warnings"}
    assert manifest["defaults"]["origin_xy"] == [3.0, 4.0]
    assert [item["dataset"] for item in list_datasets(tmp_path)] == ["one", "two"]


def test_import_rejects_path_traversal_and_unsupported_extensions(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))

    with pytest.raises(ValueError, match="unsafe filename"):
        import_video(tmp_path, "demo", "../escape.mp4", io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsupported video extension"):
        import_video(tmp_path, "demo", "video.exe", io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="unsupported CAD upload"):
        import_cad(tmp_path, "demo", "other.json", io.BytesIO(b"{}"))


def test_manifest_is_written_as_utf8_json(tmp_path: Path) -> None:
    create_dataset(tmp_path, "demo", cad_scale=0.06, origin_xy=(1, 2))
    import_cad(tmp_path, "demo", "drawing.dwg", io.BytesIO(b"raw"))

    text = (tmp_path / "data/demo/dataset_manifest.json").read_text(encoding="utf-8")
    payload = json.loads(text)
    assert "DWG" in " ".join(payload["warnings"])
