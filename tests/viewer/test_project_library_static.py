from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "apps" / "project_library"


def _read(name: str) -> str:
    return (LIBRARY / name).read_text(encoding="utf-8")


def _contrast_ratio(foreground: str, background: str) -> float:
    def luminance(value: str) -> float:
        channels = [int(value[index : index + 2], 16) / 255 for index in (1, 3, 5)]
        linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _light_theme_variable(css: str, name: str) -> str:
    block = css.split(':root[data-theme="light"] {', 1)[1].split("}", 1)[0]
    match = re.search(rf"{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}});", block)
    assert match, f"missing light theme variable {name}"
    return match.group(1)


def test_project_library_has_shared_sidebar_and_project_files_active() -> None:
    html = _read("index.html")

    assert html.count("data-nav=") == 4
    for label in ("项目概览", "项目文件", "片段管理", "设置", "收起侧栏"):
        assert label in html
    assert 'data-nav="files"' in html
    files_item = html.split('data-nav="files"', 1)[0].rsplit("<button", 1)[1]
    assert "active" in files_item
    assert 'aria-current="page"' in files_item
    assert 'd="M4 6.5h6l2 2h8v10.5H4z"' in html
    assert "M19.4 15a1.7 1.7" in html
    assert '<span aria-hidden="true">«</span>' in html
    assert "M3.5 7.5h6l2-2h3l2 2h4" not in html
    assert "M12 2.8v2.1" not in html
    assert 'd="m14 6-6 6 6 6"' not in html


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
    assert "[hidden] { display: none !important; }" in css


def test_project_library_matches_the_existing_workspace_visual_shell() -> None:
    html = _read("index.html")
    css = _read("style.css")

    assert '<span class="brand-mark" aria-hidden="true">' in html
    for declaration in (
        "--bg: #070b10",
        "--surface: #0e151d",
        "--blue: #3c7dff",
        "background: radial-gradient(circle at 80% -10%, #14243d 0, transparent 34%), var(--bg)",
        "grid-template-columns: 228px minmax(0, 1fr)",
        "background: rgba(9,15,21,.92)",
        ".topbar { min-height: 88px",
        "background: linear-gradient(135deg,#4381ff,#2364e9)",
        ".app-shell.sidebar-collapsed .sidebar-toggle span:first-child",
    ):
        assert declaration in css


def test_project_library_sidebar_theme_toggle_reuses_the_global_preference() -> None:
    html = _read("index.html")
    script = _read("project_library.js")

    assert 'id="sidebarThemeToggle"' in html
    assert 'class="sun-icon"' in html
    assert 'class="moon-icon"' in html
    assert html.index('id="sidebarThemeToggle"') < html.index('id="sidebarToggle"')
    for contract in (
        'const THEME_STORAGE_KEY = "mediaflow-theme"',
        'document.documentElement.setAttribute("data-theme", theme)',
        "window.localStorage.getItem(THEME_STORAGE_KEY)",
        "window.localStorage.setItem(THEME_STORAGE_KEY, theme)",
        'window.matchMedia?.("(prefers-color-scheme: light)")',
        'setAttribute("aria-pressed", String(isLight))',
    ):
        assert contract in script


def test_project_library_light_theme_covers_the_full_application_shell() -> None:
    css = _read("style.css")

    for contract in (
        ':root[data-theme="light"]',
        "--bg: #f5f8fc",
        "--surface: #ffffff",
        "--line: #cad7e5",
        "background: var(--page-background)",
        "background: var(--sidebar-background)",
        "background: var(--topbar-background)",
        "background: var(--panel-background)",
        ".sidebar-theme-toggle",
        ':root[data-theme="light"] .sidebar-theme-toggle .sun-icon',
        ".app-shell.sidebar-collapsed .sidebar-theme-toggle",
        ".primary-button { color: #fff;",
        ".asset-names { display: grid; gap: 3px; color: var(--text);",
        "background: var(--status-background); color: var(--status-color)",
    ):
        assert contract in css


def test_project_library_light_theme_controls_and_statuses_have_readable_contrast() -> None:
    css = _read("style.css")

    contrast_contracts = (
        ("--view-active-color", "--view-active-background", 3.0),
        ("--status-completed-color", "--status-completed-background", 4.5),
        ("--status-processing-color", "--status-processing-background", 4.5),
        ("--status-failed-color", "--status-failed-background", 4.5),
    )
    for foreground_name, background_name, minimum in contrast_contracts:
        foreground = _light_theme_variable(css, foreground_name)
        background = _light_theme_variable(css, background_name)
        assert _contrast_ratio(foreground, background) >= minimum

    assert "color: var(--view-active-color); background: var(--view-active-background)" in css
    assert "color: var(--status-completed-color); background: var(--status-completed-background)" in css
    assert "color: var(--status-failed-color); background: var(--status-failed-background)" in css


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


def test_project_library_supports_safe_multi_select_workspace_deletion() -> None:
    html = _read("index.html")
    css = _read("style.css")
    script = _read("project_library.js")

    for contract in (
        'id="deleteSelectedButton"',
        'id="deleteProjectDialog"',
        'id="confirmDeleteButton"',
        'id="selectAllProjects"',
        "永久删除项目工作空间",
        "外部源文件不会删除",
    ):
        assert contract in html
    for contract in (
        "selectedProjectIds: new Set()",
        "createSelectionControl",
        "toggleProjectSelection",
        'method: "DELETE"',
        "confirmation: project.project_id",
        "expected_revision: project.revision",
        "showModal()",
        "project_deletion_blocked",
        "running_job_count",
    ):
        assert contract in script
    assert ".project-selection" in css
    assert ".danger-button" in css
    assert ".confirm-dialog" in css
    assert ".project-card.selected" in css


def test_project_library_does_not_add_out_of_scope_controls() -> None:
    content = "\n".join((_read("index.html"), _read("project_library.js")))

    for label in ("归档项目", "导出项目", "搜索项目"):
        assert label not in content
