import csv
import json

from cadscene.core.artifacts import ArtifactManager
from cadscene.core.io import write_csv_utf8_sig


def test_artifact_manager_records_stage_manifest(tmp_path):
    manager = ArtifactManager(output_root=tmp_path, dataset="demo", run_id="run_a")
    stage = manager.stage_dir("alignment", "03_alignment")
    output = stage / "alignment.json"
    manager.write_json(output, {"ok": True})
    manager.record_stage(
        stage_name="alignment",
        command=["python", "-m", "cadscene.cli.align_to_cad"],
        inputs={"trajectory": "trajectory.json"},
        outputs={"alignment": output},
        metrics={"keyframes": 2},
        status="success",
    )

    manifest = json.loads((tmp_path / "demo" / "run_a" / "manifest.json").read_text(encoding="utf-8"))

    assert manifest["dataset"] == "demo"
    assert manifest["run_id"] == "run_a"
    assert manifest["stages"][0]["stage_name"] == "alignment"
    assert manifest["stages"][0]["outputs"]["alignment"].endswith("alignment.json")


def test_write_csv_utf8_sig_writes_bom(tmp_path):
    path = tmp_path / "report.csv"

    write_csv_utf8_sig(path, [{"frame": 1, "状态": "ok"}], fieldnames=["frame", "状态"])

    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        assert list(csv.DictReader(f)) == [{"frame": "1", "状态": "ok"}]

