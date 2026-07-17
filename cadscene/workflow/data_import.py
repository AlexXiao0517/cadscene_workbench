from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Mapping

from cadscene.cad.dwg_converter import convert_dwg
from cadscene.cad.importer import import_dxf
from cadscene.cad.loader import detect_road_centerline
from cadscene.srt import analyze_srt_stream


VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv"}
RAW_CAD_EXTENSIONS = {".dxf", ".dwg"}
CAD_ARCHIVE_EXTENSIONS = {".zip"}
PROMOTED_CAD_ASSETS = {"design.json", "road_center.json", "road_edge.json", "road_ref.json", "cad_meta.json"}
COPY_CHUNK_SIZE = 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def slugify_dataset_name(value: str) -> str:
    original = str(value or "").strip()
    normalized = unicodedata.normalize("NFKD", original).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", normalized).strip("-").lower()
    if not slug:
        slug = f"dataset-{hashlib.sha1(original.encode('utf-8')).hexdigest()[:8]}"
    return slug[:80]


def _safe_dataset_dir(root: str | Path, dataset: str, *, create: bool = False) -> tuple[Path, str]:
    base = Path(root).resolve()
    data_root = (base / "data").resolve()
    slug = slugify_dataset_name(dataset)
    target = (data_root / slug).resolve()
    if data_root not in target.parents:
        raise ValueError("dataset directory escapes data root")
    if create:
        target.mkdir(parents=True, exist_ok=True)
    return target, slug


def _manifest_path(root: str | Path, dataset: str) -> Path:
    dataset_dir, _ = _safe_dataset_dir(root, dataset)
    return dataset_dir / "dataset_manifest.json"


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _default_srt() -> dict[str, Any]:
    return {
        "status": "missing",
        "path": None,
        "analysis_json": None,
        "analysis_report": None,
        "original_name": None,
        "coverage": {},
        "attitude_sources": {"gimbal": False, "drone": False},
        "warnings": [],
    }


def _default_workflow() -> dict[str, Any]:
    return {
        "trajectory_mode": "sfm_only",
        "debug_override": None,
        "implementation_status": "ready",
    }


def _with_defaults(defaults: Mapping[str, Any], value: Any) -> dict[str, Any]:
    merged = dict(defaults)
    if not isinstance(value, Mapping):
        return merged
    for key, item in value.items():
        default = defaults.get(key)
        if isinstance(default, Mapping) and isinstance(item, Mapping):
            merged[key] = _with_defaults(default, item)
        else:
            merged[key] = item
    return merged


def _ensure_srt_workflow(manifest: dict[str, Any]) -> bool:
    srt = _with_defaults(_default_srt(), manifest.get("srt"))
    legacy_srt_mode = {
        "partial": "srt_sfm_fused",
        "full": "srt_full_pose",
    }.get(srt.get("status"))
    workflow_source = manifest.get("workflow")
    if not isinstance(workflow_source, Mapping) and legacy_srt_mode is not None:
        workflow_source = {"trajectory_mode": legacy_srt_mode}
    workflow = _with_defaults(_default_workflow(), workflow_source)
    workflow["implementation_status"] = (
        "ready" if workflow.get("trajectory_mode") == "sfm_only" else "interface_only"
    )
    changed = manifest.get("srt") != srt or manifest.get("workflow") != workflow
    manifest["srt"] = srt
    manifest["workflow"] = workflow
    return changed
    return manifest


def _default_manifest(dataset: str, cad_scale: float, origin_xy: tuple[float, float]) -> dict[str, Any]:
    timestamp = _now_iso()
    return {
        "dataset": dataset,
        "created_at": timestamp,
        "updated_at": timestamp,
        "video": None,
        "cad": {
            "design_json": None,
            "url": None,
            "source_type": None,
            "original_name": None,
            "status": "missing",
            "has_road_centerline": False,
            "road_centerline_source": "none",
        },
        "defaults": {
            "cad_scale": float(cad_scale),
            "origin_xy": [float(origin_xy[0]), float(origin_xy[1])],
        },
        "srt": _default_srt(),
        "workflow": _default_workflow(),
        "status": "incomplete",
        "warnings": [],
    }


def _refresh_status(manifest: dict[str, Any]) -> dict[str, Any]:
    video_ready = isinstance(manifest.get("video"), dict) and bool(manifest["video"].get("path"))
    cad_ready = isinstance(manifest.get("cad"), dict) and manifest["cad"].get("status") == "ready"
    cad_failed = isinstance(manifest.get("cad"), dict) and manifest["cad"].get("status") == "failed"
    manifest["status"] = "failed" if cad_failed else ("ready" if video_ready and cad_ready else "incomplete")
    manifest["updated_at"] = _now_iso()
    return manifest


def create_dataset(
    root: str | Path,
    dataset: str,
    *,
    cad_scale: float = 0.06,
    origin_xy: tuple[float, float] = (567747.5756295, 3330464.2234675),
) -> dict[str, Any]:
    if float(cad_scale) <= 0:
        raise ValueError("cad_scale must be positive")
    if len(origin_xy) != 2:
        raise ValueError("origin_xy must contain two values")
    dataset_dir, slug = _safe_dataset_dir(root, dataset, create=True)
    path = dataset_dir / "dataset_manifest.json"
    if path.exists():
        manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        _ensure_srt_workflow(manifest)
        manifest["defaults"] = {
            "cad_scale": float(cad_scale),
            "origin_xy": [float(origin_xy[0]), float(origin_xy[1])],
        }
        _atomic_json(path, _refresh_status(manifest))
        return manifest
    manifest = _default_manifest(slug, float(cad_scale), (float(origin_xy[0]), float(origin_xy[1])))
    _atomic_json(path, manifest)
    return manifest


def load_dataset_manifest(root: str | Path, dataset: str) -> dict[str, Any]:
    path = _manifest_path(root, dataset)
    if not path.exists():
        raise FileNotFoundError(f"dataset manifest not found: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8-sig"))
    if _ensure_srt_workflow(manifest):
        _atomic_json(path, manifest)
    return manifest


def list_datasets(root: str | Path) -> list[dict[str, Any]]:
    data_root = Path(root).resolve() / "data"
    if not data_root.exists():
        return []
    manifests: list[dict[str, Any]] = []
    for path in sorted(data_root.glob("*/dataset_manifest.json")):
        try:
            manifest = json.loads(path.read_text(encoding="utf-8-sig"))
            _ensure_srt_workflow(manifest)
            manifests.append(manifest)
        except (OSError, json.JSONDecodeError):
            continue
    return manifests


def _safe_filename(filename: str) -> str:
    name = str(filename or "").strip()
    normalized = name.replace("\\", "/")
    if not name or "/" in normalized or normalized in {".", ".."}:
        raise ValueError("unsafe filename")
    return name


def _copy_stream(stream: BinaryIO, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".upload")
    size = 0
    with temporary.open("wb") as output:
        while True:
            chunk = stream.read(COPY_CHUNK_SIZE)
            if not chunk:
                break
            output.write(chunk)
            size += len(chunk)
    os.replace(temporary, destination)
    return size


def import_video(root: str | Path, dataset: str, filename: str, stream: BinaryIO) -> dict[str, Any]:
    name = _safe_filename(filename)
    if Path(name).suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("unsupported video extension")
    dataset_dir, slug = _safe_dataset_dir(root, dataset, create=True)
    manifest_path = dataset_dir / "dataset_manifest.json"
    if not manifest_path.exists():
        create_dataset(root, slug)
    destination = dataset_dir / name
    size = _copy_stream(stream, destination)
    manifest = load_dataset_manifest(root, slug)
    relative = destination.relative_to(Path(root).resolve()).as_posix()
    manifest["video"] = {
        "path": relative,
        "url": f"/{relative}",
        "original_name": name,
        "size_bytes": size,
    }
    _atomic_json(manifest_path, _refresh_status(manifest))
    return manifest


def _srt_status(mode: str) -> str:
    return "full" if mode == "srt_full_pose" else "partial"


def _srt_report(analysis: Mapping[str, Any]) -> str:
    warnings = analysis.get("warnings", [])
    lines = [
        "# SRT analysis",
        "",
        f"- Source: {analysis.get('source_file', '')}",
        f"- Trajectory mode: {analysis.get('detected_mode', 'sfm_only')}",
        f"- Full-pose coverage: {analysis.get('full_pose_coverage', 0.0)}",
        "",
        "## Warnings",
        "",
    ]
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None")
    return "\n".join(lines) + "\n"


def _failed_srt_manifest(
    root: str | Path,
    destination: Path,
    name: str,
    analysis_json: Path,
    analysis_report: Path,
    error: Exception,
) -> dict[str, Any]:
    warning = f"SRT analysis failed: {error}"
    failure = {
        "source_file": name,
        "detected_mode": "sfm_only",
        "fields": {},
        "coverage": {},
        "attitude_sources": {"gimbal": False, "drone": False},
        "warnings": [warning],
        "error": str(error),
    }
    _atomic_json(analysis_json, failure)
    _atomic_text(analysis_report, _srt_report(failure))
    return {
        "status": "failed",
        "path": _relative(root, destination),
        "analysis_json": _relative(root, analysis_json),
        "analysis_report": _relative(root, analysis_report),
        "original_name": name,
        "coverage": {},
        "attitude_sources": {"gimbal": False, "drone": False},
        "warnings": [warning],
    }


def import_srt(
    root: str | Path,
    dataset: str,
    filename: str,
    stream: BinaryIO,
    video_duration_sec: float | None = None,
) -> dict[str, Any]:
    """Persist and conservatively analyse an optional SRT upload."""

    name = _safe_filename(filename)
    if Path(name).suffix.lower() != ".srt":
        raise ValueError("unsupported SRT extension")
    dataset_dir, slug = _safe_dataset_dir(root, dataset, create=True)
    manifest_path = dataset_dir / "dataset_manifest.json"
    if not manifest_path.exists():
        create_dataset(root, slug)
    manifest = load_dataset_manifest(root, slug)
    destination = dataset_dir / "telemetry" / name
    analysis_json = dataset_dir / "srt_analysis.json"
    analysis_report = dataset_dir / "srt_analysis_report.md"
    _copy_stream(stream, destination)
    try:
        with destination.open("rb") as saved_stream:
            analysis = analyze_srt_stream(saved_stream, name, video_duration_sec=video_duration_sec)
        _atomic_json(analysis_json, analysis)
        _atomic_text(analysis_report, _srt_report(analysis))
    except Exception as exc:
        manifest["srt"] = _failed_srt_manifest(
            root, destination, name, analysis_json, analysis_report, exc
        )
        manifest["workflow"] = _default_workflow()
        _atomic_json(manifest_path, _refresh_status(manifest))
        return manifest

    mode = str(analysis["detected_mode"])
    manifest["srt"] = {
        "status": _srt_status(mode),
        "path": _relative(root, destination),
        "analysis_json": _relative(root, analysis_json),
        "analysis_report": _relative(root, analysis_report),
        "original_name": name,
        "coverage": analysis["coverage"],
        "attitude_sources": analysis["attitude_sources"],
        "warnings": analysis["warnings"],
    }
    manifest["workflow"] = {
        "trajectory_mode": mode,
        "debug_override": None,
        "implementation_status": "ready" if mode == "sfm_only" else "interface_only",
    }
    _atomic_json(manifest_path, _refresh_status(manifest))
    return manifest


def load_srt_analysis(root: str | Path, dataset: str) -> dict[str, Any]:
    """Load the persisted SRT analysis for a dataset."""

    dataset_dir, _ = _safe_dataset_dir(root, dataset)
    path = dataset_dir / "srt_analysis.json"
    if not path.exists():
        raise FileNotFoundError(f"SRT analysis not found: {path}")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _zip_member_path(dataset_dir: Path, member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or ".." in pure.parts or any(":" in part for part in pure.parts):
        raise ValueError(f"unsafe zip member: {member_name}")
    destination = (dataset_dir / Path(*pure.parts)).resolve()
    if dataset_dir not in (destination, *destination.parents):
        raise ValueError(f"unsafe zip member: {member_name}")
    return destination


def _extract_assets_zip(dataset_dir: Path, archive_path: Path) -> list[str]:
    extracted: list[str] = []
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for info in archive.infolist():
                destination = _zip_member_path(dataset_dir, info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError(f"unsafe zip member: {info.filename}")
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, COPY_CHUNK_SIZE)
                extracted.append(destination.relative_to(dataset_dir).as_posix())
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid CAD assets zip") from exc
    for asset_name in PROMOTED_CAD_ASSETS:
        candidates = sorted(dataset_dir.rglob(asset_name))
        if candidates and candidates[0] != dataset_dir / asset_name:
            shutil.copyfile(candidates[0], dataset_dir / asset_name)
    return extracted


def _remove_raw_warning(manifest: dict[str, Any]) -> None:
    manifest["warnings"] = [
        item
        for item in manifest.get("warnings", [])
        if not any(token in str(item) for token in ("原始 CAD 已保存", "DWG", "DXF 未声明"))
    ]


def _relative(root: str | Path, path: Path) -> str:
    return path.resolve().relative_to(Path(root).resolve()).as_posix()


def _notify(callback: Callable[[str], None] | None, message: str) -> None:
    if callback is not None:
        callback(message)


def _parsed_cad_manifest(
    root: str | Path,
    slug: str,
    original_name: str,
    raw_path: Path,
    result,
    *,
    source_type: str,
    converted_dxf: Path | None = None,
) -> dict[str, Any]:
    stats = result.stats
    payload: dict[str, Any] = {
        "status": "ready",
        "source_type": source_type,
        "original_name": original_name,
        "raw_path": _relative(root, raw_path),
        "converted_dxf": _relative(root, converted_dxf) if converted_dxf else None,
        "design_json": _relative(root, result.design_json),
        "url": f"/data/{slug}/design.json",
        "bbox": stats["bbox"],
        "entity_count": stats["entity_count"],
        "point_count": stats["point_count"],
        "segment_count": stats["segment_count"],
        "layer_count": stats["layer_count"],
        "aci_colors": stats["aci_colors"],
        "unit": stats["unit"],
        "unsupported_entities": stats["unsupported_entities"],
        "import_report": _relative(root, result.report),
        "import_stats": _relative(root, result.stats_json),
        "has_road_centerline": bool(stats.get("has_road_centerline", False)),
        "road_centerline_source": str(stats.get("road_centerline_source", "none")),
    }
    return payload


def import_cad(
    root: str | Path,
    dataset: str,
    filename: str,
    stream: BinaryIO,
    *,
    status_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    name = _safe_filename(filename)
    extension = Path(name).suffix.lower()
    if name.lower() != "design.json" and extension not in CAD_ARCHIVE_EXTENSIONS | RAW_CAD_EXTENSIONS:
        raise ValueError("unsupported CAD upload")
    dataset_dir, slug = _safe_dataset_dir(root, dataset, create=True)
    manifest_path = dataset_dir / "dataset_manifest.json"
    if not manifest_path.exists():
        create_dataset(root, slug)
    manifest = load_dataset_manifest(root, slug)
    if name.lower() == "design.json":
        destination = dataset_dir / "design.json"
        _copy_stream(stream, destination)
        try:
            json.loads(destination.read_text(encoding="utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            destination.unlink(missing_ok=True)
            raise ValueError("invalid design.json") from exc
        centerline = detect_road_centerline(dataset_dir)
        manifest["cad"] = {
            "design_json": destination.relative_to(Path(root).resolve()).as_posix(),
            "url": f"/data/{slug}/design.json",
            "source_type": "design_json",
            "original_name": name,
            "status": "ready",
            "has_road_centerline": centerline.has_road_centerline,
            "road_centerline_source": centerline.source,
        }
        _remove_raw_warning(manifest)
    elif extension == ".zip":
        archive_path = dataset_dir / ".cad_assets.upload.zip"
        _copy_stream(stream, archive_path)
        try:
            _extract_assets_zip(dataset_dir, archive_path)
        finally:
            archive_path.unlink(missing_ok=True)
        design = dataset_dir / "design.json"
        ready = design.exists()
        centerline = detect_road_centerline(dataset_dir) if ready else None
        manifest["cad"] = {
            "design_json": design.relative_to(Path(root).resolve()).as_posix() if ready else None,
            "url": f"/data/{slug}/design.json" if ready else None,
            "source_type": "assets_zip",
            "original_name": name,
            "status": "ready" if ready else "missing",
            "has_road_centerline": bool(centerline and centerline.has_road_centerline),
            "road_centerline_source": centerline.source if centerline else "none",
        }
        _remove_raw_warning(manifest)
        if not ready:
            manifest.setdefault("warnings", []).append("CAD assets zip 中没有 design.json。")
    elif extension == ".dxf":
        destination = dataset_dir / "raw_cad" / name
        _notify(status_callback, "正在保存原始 CAD")
        _copy_stream(stream, destination)
        try:
            _notify(status_callback, "正在解析 DXF")
            result = import_dxf(destination, dataset_dir)
            _notify(status_callback, "正在生成 design.json")
        except Exception as exc:
            manifest["cad"] = {
                "status": "failed",
                "source_type": "dxf_failed",
                "original_name": name,
                "raw_path": _relative(root, destination),
                "design_json": None,
                "url": None,
                "error": str(exc),
            }
            manifest.setdefault("warnings", []).append(f"DXF 解析失败：{exc}")
            _atomic_json(manifest_path, _refresh_status(manifest))
            _notify(status_callback, "CAD 解析失败")
            raise ValueError(f"DXF parsing failed: {exc}") from exc
        manifest["cad"] = _parsed_cad_manifest(
            root,
            slug,
            name,
            destination,
            result,
            source_type="dxf_parsed",
        )
        manifest["defaults"] = {
            "cad_scale": float(result.stats["cad_scale"]),
            "origin_xy": list(result.stats["origin_xy"]),
        }
        _remove_raw_warning(manifest)
        manifest.setdefault("warnings", []).extend(
            warning for warning in result.warnings if warning not in manifest["warnings"]
        )
        _notify(status_callback, "CAD 解析完成")
    else:
        destination = dataset_dir / "raw_cad" / name
        _notify(status_callback, "正在保存原始 CAD")
        _copy_stream(stream, destination)
        converted = dataset_dir / "raw_cad" / f"{destination.stem}_converted.dxf"
        _notify(status_callback, "正在转换 DWG")
        try:
            converted_path = convert_dwg(destination, converted)
        except Exception as exc:
            manifest["cad"] = {
                "status": "failed",
                "source_type": "dwg_conversion_failed",
                "original_name": name,
                "raw_path": _relative(root, destination),
                "converted_dxf": None,
                "design_json": None,
                "url": None,
                "error": str(exc),
            }
            manifest.setdefault("warnings", []).append(f"DWG 转换失败：{exc}")
            _atomic_json(manifest_path, _refresh_status(manifest))
            _notify(status_callback, "CAD 解析失败")
            raise ValueError(f"DWG conversion failed: {exc}") from exc
        if converted_path is None:
            warning = "DWG 已保存，但当前环境缺少 DWG→DXF 转换工具，请安装转换器或上传 DXF。"
            manifest["cad"] = {
                "status": "raw_saved",
                "source_type": "dwg_raw_saved",
                "original_name": name,
                "raw_path": _relative(root, destination),
                "converted_dxf": None,
                "design_json": None,
                "url": None,
            }
            if warning not in manifest.setdefault("warnings", []):
                manifest["warnings"].append(warning)
        else:
            _notify(status_callback, "正在解析 DXF")
            result = import_dxf(converted_path, dataset_dir)
            _notify(status_callback, "正在生成 design.json")
            manifest["cad"] = _parsed_cad_manifest(
                root,
                slug,
                name,
                destination,
                result,
                source_type="dwg_converted",
                converted_dxf=converted_path,
            )
            manifest["defaults"] = {
                "cad_scale": float(result.stats["cad_scale"]),
                "origin_xy": list(result.stats["origin_xy"]),
            }
            _remove_raw_warning(manifest)
            manifest.setdefault("warnings", []).extend(
                warning for warning in result.warnings if warning not in manifest["warnings"]
            )
            _notify(status_callback, "CAD 解析完成")
    _atomic_json(manifest_path, _refresh_status(manifest))
    return manifest
