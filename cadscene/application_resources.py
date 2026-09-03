from __future__ import annotations

from pathlib import Path


_REQUIRED_RESOURCES = (
    Path("apps/workflow_portal/index.html"),
    Path("apps/project_workspace/index.html"),
    Path("apps/project_library/index.html"),
    Path("apps/web_camera_viewer/index.html"),
    Path("configs/pipelines/sfm_overlay_existing_sfm.yaml"),
    Path("configs/pipelines/srt_fixed_track_visual_pose_overlay.yaml"),
    Path("configs/pure_rotation/adapter-calibration/cameras.txt"),
)


def validate_application_root(root: str | Path) -> Path:
    """返回同时包含正式网页和固定配置的应用根目录。"""

    resolved = Path(root).resolve()
    missing = [str(path) for path in _REQUIRED_RESOURCES if not (resolved / path).is_file()]
    if missing:
        raise FileNotFoundError(
            f"application resources are incomplete under {resolved}: "
            + ", ".join(missing)
        )
    return resolved


def application_root() -> Path:
    """定位源码 checkout 或 wheel 安装中的只读应用资源。"""

    return validate_application_root(Path(__file__).resolve().parents[1])


def apps_root() -> Path:
    return application_root() / "apps"


def configs_root() -> Path:
    return application_root() / "configs"
