from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


CURRENT_DOCUMENTS = (
    "README.md",
    "README_EN.md",
    "CHANGELOG.md",
    "docs/README.md",
    "docs/pipeline_usage.md",
    "docs/roadmap.md",
    "docs/sfm_cuda_backend.md",
    "docs/srt_capability_detection.md",
    "docs/web_viewer_usage.md",
    "docs/workflow_routing.md",
    "docs/design/coordinates-and-alignment.md",
    "docs/design/system-architecture.md",
    "docs/technical/api-and-artifacts.md",
    "docs/technical/developer-guide.md",
    "docs/technical/troubleshooting.md",
)


def test_official_upload_formats_match_the_current_portal() -> None:
    portal_html = _read("apps/workflow_portal/index.html")
    portal_script = _read("apps/workflow_portal/workflow_portal.js")
    readme = _read("README.md")
    readme_en = _read("README_EN.md")
    routing = _read("docs/workflow_routing.md")

    assert 'accept=".mp4,video/mp4"' in portal_html
    assert 'accept=".dxf,application/dxf"' in portal_html
    assert "视频仅支持 MP4 格式" in portal_script
    assert "CAD 图纸仅支持 DXF 格式" in portal_script

    assert "正式上传界面目前仅支持 MP4 视频和 DXF 图纸" in readme
    assert "The official upload UI currently supports only MP4 video and DXF drawings" in readme_en
    assert "正式项目上传界面" in routing
    assert "仅支持 `.mp4`" in routing
    assert "仅支持 `.dxf`" in routing

    stale_portal_claims = (
        "门户支持 MP4、MOV、AVI 和 MKV",
        "门户文件选择器支持 DXF 或 DWG",
        "the portal accepts MP4, MOV, AVI, and MKV",
        "The portal accepts DXF or DWG",
        "支持本地 MP4、MOV、AVI、MKV",
        "门户创建项目页面仅可选择 DXF 或 DWG",
    )
    current_user_docs = "\n".join((readme, readme_en, routing))
    for claim in stale_portal_claims:
        assert claim not in current_user_docs
    preparation = readme.split("## 适用场景与准备", 1)[1].split("## 快速开始", 1)[0]
    preparation_en = readme_en.split(
        "## Suitable scenarios and preparation", 1
    )[1].split("## Quick start", 1)[0]
    for unsupported in ("MOV", "AVI", "MKV", "M4V", "DWG", "design.json", "ZIP"):
        assert unsupported not in preparation
        assert unsupported not in preparation_en


def test_pure_rotation_is_documented_as_an_automatic_analysis_recommendation() -> None:
    portal_html = _read("apps/workflow_portal/index.html")
    portal_script = _read("apps/workflow_portal/workflow_portal.js")
    analyzer = _read("cadscene/video_analysis/analyzer.py")
    workspace_html = _read("apps/project_workspace/index.html")
    workspace_script = _read("apps/project_workspace/project_workspace.js")
    current_docs = "\n".join(_read(path) for path in CURRENT_DOCUMENTS)

    assert "portalHoveringDeclared" not in portal_html
    assert "hoveringDeclared" not in portal_script
    assert "source_rotation_verified" in analyzer
    assert "pure_rotation_verified=(" in analyzer
    assert 'class="workflow-select"' in workspace_html
    assert "clip.recommended_workflow" in workspace_script
    assert "clip.resolved_workflow" in workspace_script
    workflow_select = workspace_html.split('class="workflow-select"', 1)[1].split(
        "</select>", 1
    )[0]
    assert workflow_select.count("<option") == 2
    assert 'value="sfm_only"' in workflow_select
    assert 'value="pure_rotation"' in workflow_select
    assert (
        'return workflow === "pure_rotation" ? "pure_rotation" : "sfm_only";'
        in workspace_script
    )
    assert 'workflow.addEventListener("change"' in workspace_script

    assert "自动检测运动特征并保守推荐" in current_docs
    assert "automatic motion analysis can conservatively recommend" in current_docs.lower()
    routing = _read("docs/workflow_routing.md")
    assert "单一逻辑片段且不足 60 秒" in routing
    assert "项目页的“最终工作流”选择器用于纠正推荐" in routing
    assert "最终工作流下拉框只显示 SfM 和 OpenGV" in routing
    assert "不能仅凭下拉框显示的 SfM 认为覆盖已经保存" in routing
    assert "必须新建一个不上传 SRT 的项目" in routing

    stale_rotation_claims = (
        "用户显式声明“无人机悬停，仅转动视角”后使用",
        "不会由系统自动判断；只有用户在上传时明确选择实验选项",
        "用户在门户显式勾选“无人机悬停，仅转动视角（实验）”",
        "该 Experimental 路线要求用户声明悬停/纯旋转",
        "也不会自动识别纯旋转视频",
        "也不自动判断视频是否属于纯旋转",
        "Used only when the user explicitly declares hovering footage",
        "is never selected by automatic motion classification",
        "users select it explicitly rather than through automatic motion classification",
    )
    for claim in stale_rotation_claims:
        assert claim not in current_docs


def test_current_docs_separate_project_storage_from_compatibility_storage() -> None:
    viewer_doc = _read("docs/web_viewer_usage.md")
    doc_index = _read("docs/README.md")

    assert "<storage-root>/projects/<project_id>/" in viewer_doc
    assert "`data/` 与 `runs/` 只属于兼容 dataset/run 工作流" in viewer_doc
    assert "门户创建的数据集可直接进入 Viewer" not in viewer_doc
    assert "门户进入 Viewer 后" not in viewer_doc
    assert "正式项目从项目片段管理进入 Viewer" in viewer_doc
    normalized_viewer_doc = " ".join(viewer_doc.split())
    assert "兼容 Workflow API 创建的 dataset" in normalized_viewer_doc
    assert "正式项目流程" in doc_index
    assert "兼容 dataset/run 工作流" in doc_index


def test_current_docs_do_not_pin_themselves_to_an_obsolete_commit() -> None:
    for path in CURRENT_DOCUMENTS:
        assert "main@" not in _read(path), path
    for path in (
        "docs/design/system-architecture.md",
        "docs/technical/api-and-artifacts.md",
    ):
        introduction = _read(path).split("##", 1)[0]
        assert re.search(r"\b[0-9a-f]{7,40}\b", introduction) is None, path


def test_installation_and_supported_pure_rotation_status_are_current() -> None:
    readme = _read("README.md")
    readme_en = _read("README_EN.md")
    developer_guide = _read("docs/technical/developer-guide.md")

    assert "python -m pip install ." in readme
    assert "cadscene-workbench doctor" in readme
    assert "cadscene-workbench serve" in readme
    assert "python -m pip install ." in readme_en
    assert "cadscene-workbench doctor" in readme_en
    assert "cadscene-workbench serve" in readme_en
    assert "`pure_rotation` | Supported" in readme
    assert "`pure_rotation` | Supported" in readme_en
    assert "`segmentation`" not in developer_guide
    assert "临时显式安装 `PyYAML`" not in developer_guide


def test_project_library_is_the_normal_reopen_entry() -> None:
    readme = _read("README.md")
    readme_en = _read("README_EN.md")

    assert "http://127.0.0.1:8300/apps/project_library/" in readme
    assert "项目库" in readme
    assert "http://127.0.0.1:8300/apps/project_library/" in readme_en
    assert "project library" in readme_en.lower()


def test_current_document_links_and_documented_cli_modules_exist() -> None:
    markdown_link = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    cli_module = re.compile(r"python -m cadscene\.cli\.([A-Za-z0-9_]+)")

    for path in CURRENT_DOCUMENTS:
        document = ROOT / path
        text = document.read_text(encoding="utf-8")
        for raw_target in markdown_link.findall(text):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            assert (document.parent / target).resolve().exists(), (path, raw_target)
        for module in cli_module.findall(text):
            assert (ROOT / "cadscene" / "cli" / f"{module}.py").is_file(), (
                path,
                module,
            )
