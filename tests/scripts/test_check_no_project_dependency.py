from pathlib import Path

from scripts.check_no_project_dependency import scan_for_forbidden_dependencies


def test_checker_flags_runtime_project_and_cadvideo_dependencies(tmp_path):
    root = tmp_path / "cadscene_workbench"
    (root / "cadscene").mkdir(parents=True)
    bad = root / "cadscene" / "bad.py"
    bad.write_text("from cad" + "video.sfm_align import align\n", encoding="utf-8")

    findings = scan_for_forbidden_dependencies(root)

    assert findings
    assert findings[0].path == bad
    assert "cad" + "video.sfm_align" in findings[0].matched_text


def test_checker_allows_docs_legacy_and_refactor_plan_mentions(tmp_path):
    root = tmp_path / "cadscene_workbench"
    for folder in ("docs", "legacy", "cadscene_workbench_refactor_plan"):
        path = root / folder
        path.mkdir(parents=True)
        (path / "note.md").write_text("旧路径 " + "proj" + "ect/cad" + "video/sfm_align/align.py 仅作说明。\n", encoding="utf-8")

    assert scan_for_forbidden_dependencies(root) == []


def test_checker_allows_projection_module_name(tmp_path):
    root = tmp_path / "cadscene_workbench"
    (root / "cadscene").mkdir(parents=True)
    good = root / "cadscene" / "good.py"
    good.write_text("from cadscene.cad.projection import project_point\n", encoding="utf-8")

    assert scan_for_forbidden_dependencies(root) == []
