from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "apps" / "project_library"


def _read(name: str) -> str:
    return (LIBRARY / name).read_text(encoding="utf-8")


def test_project_library_has_shared_sidebar_and_project_files_active() -> None:
    html = _read("index.html")

    assert html.count("data-nav=") == 4
    for label in ("项目概览", "项目文件", "片段管理", "设置", "收起侧栏"):
        assert label in html
    assert 'data-nav="files"' in html
    files_item = html.split('data-nav="files"', 1)[0].rsplit("<button", 1)[1]
    assert "active" in files_item
    assert 'aria-current="page"' in files_item


def test_project_library_supports_fixed_card_and_detailed_list_views() -> None:
    html = _read("index.html")
    css = _read("style.css")
    script = _read("project_library.js")

    assert 'id="cardViewButton"' in html
    assert 'id="listViewButton"' in html
    assert 'id="newProjectButton"' in html
    assert "/apps/workflow_portal/" in html
    assert 'id="projectGrid"' in html
    assert 'id="projectTable"' in html
    for heading in ("项目名称", "视频 / CAD 文件", "片段完成度", "当前状态", "最近更新时间"):
        assert heading in html
    assert "grid-template-columns: repeat(auto-fill, 148px)" in css
    assert "justify-content: start" in css
    assert "mediaflow-project-library-view" in script
    assert "localStorage.getItem" in script
    assert "localStorage.setItem" in script
    assert "renderCards" in script
    assert "renderList" in script


def test_project_library_has_safe_empty_error_and_retry_states() -> None:
    html = _read("index.html")
    script = _read("project_library.js")

    assert 'id="emptyState"' in html
    assert 'id="errorState"' in html
    assert 'id="retryButton"' in html
    assert 'id="refreshButton"' in html
    assert 'fetch("/api/projects"' in script
    assert "textContent" in script
    assert "innerHTML" not in script
    assert "const hasError = !errorState.hidden" in script


def test_project_rename_is_hover_only_and_uses_existing_revision_api() -> None:
    css = _read("style.css")
    script = _read("project_library.js")

    assert ".project-rename-button" in css
    assert "opacity: 0" in css
    assert ".project-name-row:hover .project-rename-button" in css
    assert ".project-name-row:focus-within .project-rename-button" in css
    assert 'method: "PATCH"' in script
    assert "display_name" in script
    assert "expected_revision" in script
    assert "revision_conflict" in script
    assert 'event.key === "Enter"' in script
    assert 'event.key === "Escape"' in script


def test_project_library_does_not_add_out_of_scope_controls() -> None:
    content = "\n".join((_read("index.html"), _read("project_library.js")))

    for label in ("删除项目", "归档项目", "导出项目", "搜索项目"):
        assert label not in content
