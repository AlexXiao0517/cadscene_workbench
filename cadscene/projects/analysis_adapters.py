from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import sys
from typing import Mapping

from cadscene.video_analysis.artifacts import CURRENT_REVISION_POINTER

from .adapters import AdapterResult
from .executor import JobExecutionPlan
from .queue import QueueJob


ADAPTER_NAME = "project_analysis"
ADAPTER_VERSION = "2"


def prepare_analysis_plan(
    job: QueueJob, source_assets: Mapping[str, object]
) -> JobExecutionPlan:
    attempt = Path(job.attempts[-1].directory)
    attempt.mkdir(parents=True, exist_ok=True)
    if job.job_type in {"cad_analysis", "cad_replacement"}:
        cad_path = asset_path(source_assets, "cad")
        if cad_path is None or not cad_path.is_file():
            raise FileNotFoundError("published project CAD is unavailable")
        cad_asset = source_assets.get("cad")
        if not isinstance(cad_asset, Mapping):
            raise ValueError("published CAD descriptor is missing")
        command = (
            sys.executable,
            "-m",
            "cadscene.projects.analysis_worker",
            "cad",
            "--project-id",
            job.project_id,
            "--input",
            str(cad_path),
            "--original-filename",
            str(cad_asset.get("original_filename") or cad_path.name),
            "--attempt-dir",
            str(attempt),
        )
        return JobExecutionPlan(
            commands=(command,), validate=lambda: validate_cad_outputs(job)
        )
    if job.job_type != "video_analysis":
        raise ValueError(f"unsupported analysis job type: {job.job_type}")
    video_path = asset_path(source_assets, "video")
    if video_path is None or not video_path.is_file():
        raise FileNotFoundError("published project video is unavailable")
    revision = f"analysis-{job.job_id}"
    command_items = [
        sys.executable,
        "-m",
        "cadscene.projects.analysis_worker",
        "video",
        "--project-id",
        job.project_id,
        "--input",
        str(video_path),
        "--analysis-revision",
        revision,
        "--attempt-dir",
        str(attempt),
    ]
    srt_path = asset_path(source_assets, "srt")
    if srt_path is not None and srt_path.is_file():
        command_items.extend(("--srt", str(srt_path)))
    return JobExecutionPlan(
        commands=(tuple(command_items),),
        validate=lambda: validate_video_outputs(job, revision),
    )


def validate_result_path(job: QueueJob, result: AdapterResult) -> Path:
    attempt_root = Path(job.attempts[-1].directory).resolve()
    output_key = (
        "cad_dataset"
        if job.job_type in {"cad_analysis", "cad_replacement"}
        else "analysis_output"
    )
    value = result.outputs.get(output_key)
    if value is None:
        raise ValueError(f"adapter output is missing {output_key}")
    output = Path(value).resolve()
    if not output.is_relative_to(attempt_root):
        raise ValueError(f"{output_key} escapes its immutable attempt directory")
    if not output.is_dir():
        raise FileNotFoundError(f"{output_key} directory is missing")
    if job.job_type in {"cad_analysis", "cad_replacement"}:
        payload = _read_json(output / "dataset_manifest.json", "CAD attempt")
        if str(payload.get("dataset")) != job.project_id:
            raise ValueError("CAD attempt dataset belongs to another project")
        return output
    payload = _read_json(output / "clip_manifest.json", "video analysis")
    if str(payload.get("analysis_revision")) != result.output_revision:
        raise ValueError("video analysis revision does not match adapter result")
    clips = payload.get("clips")
    if not isinstance(clips, list) or not clips:
        raise ValueError("video analysis produced no logical clips")
    return output


def validate_dependency_output(dependency: QueueJob, video_job: QueueJob) -> Path:
    if (
        dependency.project_id != video_job.project_id
        or dependency.job_type != "cad_analysis"
        or dependency.status != "success"
        or not dependency.output_validated
        or dependency.validated_input_fingerprint != dependency.input_fingerprint
    ):
        raise ValueError("CAD analysis dependency is not validated")
    value = dependency.published_outputs.get("cad_dataset")
    if value is None:
        raise ValueError("CAD dependency has no dataset output")
    dataset = Path(value).resolve()
    attempt_root = Path(dependency.attempts[-1].directory).resolve()
    if not dataset.is_relative_to(attempt_root):
        raise ValueError("CAD dependency output escapes its attempt directory")
    if not (dataset / "dataset_manifest.json").is_file():
        raise FileNotFoundError("CAD dependency dataset is incomplete")
    return dataset


def validate_cad_outputs(job: QueueJob) -> AdapterResult:
    dataset = Path(job.attempts[-1].directory) / "scratch" / "data" / job.project_id
    manifest = dataset / "dataset_manifest.json"
    try:
        payload = _read_json(manifest, "CAD analysis")
    except (OSError, ValueError) as exc:
        return AdapterResult.failed(str(exc))
    if str(payload.get("dataset")) != job.project_id:
        return AdapterResult.failed("CAD analysis dataset belongs to another project")
    digest = tree_fingerprint(dataset)
    return AdapterResult.success(
        output_revision=f"cad:{digest[:16]}",
        output_fingerprint=digest,
        outputs={"cad_dataset": str(dataset)},
    )


def validate_video_outputs(job: QueueJob, revision: str) -> AdapterResult:
    output = _video_analysis_output_dir(job, revision)
    try:
        payload = _read_json(output / "clip_manifest.json", "video analysis")
    except (OSError, ValueError) as exc:
        return AdapterResult.failed(str(exc))
    if str(payload.get("analysis_revision")) != revision:
        return AdapterResult.failed("video analysis revision mismatch")
    if not payload.get("clips"):
        return AdapterResult.failed("video analysis produced no logical clips")
    digest = tree_fingerprint(output)
    return AdapterResult.success(
        output_revision=revision,
        output_fingerprint=digest,
        outputs={"analysis_output": str(output), "analysis_revision": revision},
    )


def _video_analysis_output_dir(job: QueueJob, revision: str) -> Path:
    """Resolve both legacy flat and current revision-indexed analyzer layouts."""
    root = Path(job.attempts[-1].directory) / "02_video_analysis"
    flat = root / revision
    indexed = root / "analysis_revisions" / revision
    if (flat / "clip_manifest.json").is_file():
        return flat
    if (indexed / "clip_manifest.json").is_file():
        return indexed
    pointer = root / CURRENT_REVISION_POINTER
    if pointer.is_file():
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8-sig"))
            if str(payload.get("analysis_revision")) == revision:
                relative = Path(str(payload.get("revision_directory", "")))
                candidate = (root / relative).resolve()
                resolved_root = root.resolve()
                if (
                    candidate.is_relative_to(resolved_root)
                    and (candidate / "clip_manifest.json").is_file()
                ):
                    return candidate
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return indexed


def asset_path(assets: Mapping[str, object], name: str) -> Path | None:
    value = assets.get(f"{name}_path") or assets.get(name)
    if isinstance(value, Mapping):
        value = value.get("path")
    return None if value in (None, "") else Path(str(value))


def tree_fingerprint(root: Path) -> str:
    digest = sha256()
    digest.update(b"cadscene-tree-fingerprint-v2\x00")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            entry_type = b"L"
            content = path.readlink().as_posix().encode("utf-8")
        elif path.is_dir():
            entry_type = b"D"
            content = b""
        elif path.is_file():
            entry_type = b"F"
            content = path.read_bytes()
        else:
            raise ValueError(f"unsupported tree entry: {path}")
        digest.update(entry_type)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _read_json(path: Path, label: str) -> Mapping[str, object]:
    if not path.is_file():
        raise FileNotFoundError(f"{label} manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid {label} manifest: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(f"invalid {label} manifest root")
    return value
