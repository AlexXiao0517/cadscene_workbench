from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import threading
from typing import Callable, Mapping
from uuid import uuid4
import zipfile


ASSET_TYPES = frozenset({"video", "cad", "srt"})
from .identifiers import validate_project_id
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".m4v"})
_CAD_EXTENSIONS = frozenset({".dwg", ".dxf", ".json", ".zip"})


class UploadValidationError(ValueError):
    """An upload failed integrity or content validation and was not published."""


Validator = Callable[[Path, str], Mapping[str, object]]
PublishedCallback = Callable[[str, str, "PublishedUpload"], None]


@dataclass(frozen=True)
class PublishedUpload:
    project_id: str
    asset_type: str
    original_filename: str
    path: Path
    size_bytes: int
    sha256: str
    validation: Mapping[str, object]
    validation_report_path: Path


class PendingUpload:
    def __init__(
        self,
        store: "ValidatedUploadStore",
        *,
        project_id: str,
        asset_type: str,
        original_filename: str,
        temporary_path: Path,
        destination: Path,
        expected_size: int,
        expected_sha256: str | None,
    ) -> None:
        self._store = store
        self.project_id = project_id
        self.asset_type = asset_type
        self.original_filename = original_filename
        self.temporary_path = temporary_path
        self.destination = destination
        self.expected_size = expected_size
        self.expected_sha256 = expected_sha256
        self._stream = temporary_path.open("xb")
        self._digest = sha256()
        self._size = 0
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("upload is already closed")
        if not isinstance(data, bytes):
            raise TypeError("upload chunks must be bytes")
        written = self._stream.write(data)
        self._digest.update(data[:written])
        self._size += written
        return written

    def abort(self) -> None:
        if not self._closed:
            self._stream.close()
            self._closed = True
        self.temporary_path.unlink(missing_ok=True)

    def complete(self) -> PublishedUpload:
        if self._closed:
            raise ValueError("upload is already closed")
        self._stream.flush()
        os.fsync(self._stream.fileno())
        self._stream.close()
        self._closed = True
        digest = self._digest.hexdigest()
        try:
            if self._size != self.expected_size:
                raise UploadValidationError(
                    f"upload size mismatch: expected {self.expected_size}, got {self._size}"
                )
            if self.expected_sha256 is not None and digest != self.expected_sha256:
                raise UploadValidationError("upload SHA-256 fingerprint mismatch")
            validation = dict(
                self._store.validator_for(self.asset_type)(
                    self.temporary_path, self.asset_type
                )
            )
            self.destination = self.destination.with_name(
                f"{self.asset_type}-{digest[:16]}{self.destination.suffix}"
            )
            self._store._write_attempt_report(
                self.project_id,
                self.asset_type,
                {
                    "schema_version": "1.0",
                    "status": "validated",
                    "asset_type": self.asset_type,
                    "original_filename": self.original_filename,
                    "size_bytes": self._size,
                    "sha256": digest,
                    "validation": validation,
                },
            )
            os.replace(self.temporary_path, self.destination)
            self._store._fsync_directory(self.destination.parent)
            report = {
                "schema_version": "1.0",
                "status": "published",
                "asset_type": self.asset_type,
                "original_filename": self.original_filename,
                "published_path": self.destination.name,
                "size_bytes": self._size,
                "sha256": digest,
                "validation": validation,
            }
            report_path = self._store._write_attempt_report(
                self.project_id, self.asset_type, report
            )
            self._store._write_report(
                self.project_id, self.asset_type, report
            )
        except Exception as exc:
            self.temporary_path.unlink(missing_ok=True)
            if not isinstance(exc, UploadValidationError):
                error = UploadValidationError(str(exc))
            else:
                error = exc
            failure = {
                "schema_version": "1.0",
                "status": "failed",
                "asset_type": self.asset_type,
                "original_filename": self.original_filename,
                "size_bytes": self._size,
                "sha256": digest,
                "error": str(error),
            }
            if self.published_path_exists():
                self._store._write_attempt_report(
                    self.project_id, self.asset_type, failure
                )
            else:
                self._store._write_report(
                    self.project_id, self.asset_type, failure
                )
            raise error from exc if error is not exc else None
        published = PublishedUpload(
            project_id=self.project_id,
            asset_type=self.asset_type,
            original_filename=self.original_filename,
            path=self.destination,
            size_bytes=self._size,
            sha256=digest,
            validation=validation,
            validation_report_path=report_path,
        )
        self._store._notify_published(published)
        return published

    def published_path_exists(self) -> bool:
        return self._store.published_path(
            self.project_id, self.asset_type
        ).is_file()


class ValidatedUploadStore:
    """Publishes validated assets atomically inside their project directory."""

    def __init__(
        self,
        projects_root: Path,
        *,
        validators: Mapping[str, Validator] | None = None,
        on_published: PublishedCallback | None = None,
    ) -> None:
        self.projects_root = Path(projects_root)
        self._validators = dict(validators or {})
        self._on_published = on_published
        self._lock = threading.RLock()

    def begin(
        self,
        project_id: str,
        asset_type: str,
        filename: str,
        *,
        expected_size: int,
        expected_sha256: str | None = None,
    ) -> PendingUpload:
        project_id = self._validate_project_id(project_id)
        if asset_type not in ASSET_TYPES:
            raise ValueError(f"unsupported asset type: {asset_type}")
        if isinstance(expected_size, bool) or expected_size < 0:
            raise ValueError("expected_size must be a non-negative integer")
        original_filename = Path(filename).name
        if not original_filename or original_filename != filename:
            raise ValueError("unsafe upload filename")
        extension = Path(original_filename).suffix.lower()
        self._validate_extension(asset_type, extension, original_filename)
        if expected_sha256 is not None:
            expected_sha256 = expected_sha256.lower()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
                raise ValueError("expected_sha256 must be a lowercase SHA-256 digest")
        assets = self._assets_dir(project_id)
        assets.mkdir(parents=True, exist_ok=True)
        destination = assets / f"{asset_type}{extension}"
        temporary = assets / (
            f".{destination.stem}.upload-{uuid4().hex}{destination.suffix}"
        )
        return PendingUpload(
            self,
            project_id=project_id,
            asset_type=asset_type,
            original_filename=original_filename,
            temporary_path=temporary,
            destination=destination,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def validator_for(self, asset_type: str) -> Validator:
        return self._validators.get(asset_type, _DEFAULT_VALIDATORS[asset_type])

    def validation_report_path(self, project_id: str, asset_type: str) -> Path:
        self._validate_project_id(project_id)
        if asset_type not in ASSET_TYPES:
            raise ValueError(f"unsupported asset type: {asset_type}")
        return self._assets_dir(project_id) / f"{asset_type}.validation.json"

    def published_path(self, project_id: str, asset_type: str) -> Path:
        report_path = self.validation_report_path(project_id, asset_type)
        if report_path.is_file():
            try:
                report = json.loads(report_path.read_text(encoding="utf-8"))
                name = report.get("published_path")
                if report.get("status") == "published" and isinstance(name, str):
                    candidate = (report_path.parent / name).resolve()
                    if report_path.parent.resolve() in candidate.parents:
                        return candidate
            except (OSError, json.JSONDecodeError):
                pass
        return self._assets_dir(project_id) / asset_type

    def _notify_published(self, upload: PublishedUpload) -> None:
        if self._on_published is not None:
            self._on_published(upload.project_id, upload.asset_type, upload)

    def _write_report(
        self, project_id: str, asset_type: str, payload: Mapping[str, object]
    ) -> Path:
        path = self.validation_report_path(project_id, asset_type)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.tmp-{uuid4().hex}"
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _write_attempt_report(
        self, project_id: str, asset_type: str, payload: Mapping[str, object]
    ) -> Path:
        directory = self._assets_dir(project_id) / "validation_attempts"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{asset_type}-{uuid4().hex}.json"
        temporary = directory / f".{path.name}.tmp"
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            with temporary.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            self._fsync_directory(directory)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _assets_dir(self, project_id: str) -> Path:
        return self.projects_root / project_id / "assets"

    @staticmethod
    def _validate_project_id(project_id: str) -> str:
        return validate_project_id(project_id)

    @staticmethod
    def _validate_extension(asset_type: str, extension: str, filename: str) -> None:
        if asset_type == "video" and extension not in _VIDEO_EXTENSIONS:
            raise ValueError("unsupported video extension")
        if asset_type == "srt" and extension != ".srt":
            raise ValueError("unsupported SRT extension")
        if asset_type == "cad" and extension not in _CAD_EXTENSIONS:
            raise ValueError("unsupported CAD extension")
        if asset_type == "cad" and extension == ".json" and filename.lower() != "design.json":
            raise ValueError("CAD JSON upload must be named design.json")


def _validate_video(path: Path, _asset_type: str) -> Mapping[str, object]:
    from cadscene.video_analysis.pts import (
        probe_decoded_frame_index,
        resolve_ffmpeg_executable,
    )

    ffmpeg = resolve_ffmpeg_executable()
    process = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if process.returncode != 0 or process.stderr.strip():
        raise ValueError(f"video full decode failed: {process.stderr[-1000:]}")
    index = probe_decoded_frame_index(path, ffmpeg_executable=ffmpeg)
    return {
        "full_decode": True,
        "decoded_frame_count": len(index.frames),
        "source_start_pts": index.source_start_pts,
        "source_end_pts_exclusive": index.source_end_pts_exclusive,
        "source_time_base": {
            "numerator": index.time_base.numerator,
            "denominator": index.time_base.denominator,
        },
    }


def _validate_srt(path: Path, _asset_type: str) -> Mapping[str, object]:
    from cadscene.srt.parser import analyze_srt_stream

    with path.open("rb") as stream:
        analysis = analyze_srt_stream(stream, path.name)
    if not analysis.get("records"):
        raise ValueError("SRT contains no parseable records")
    return {
        "record_count": len(analysis["records"]),
        "detected_mode": analysis.get("detected_mode"),
        "coverage": analysis.get("coverage", {}),
    }


def _validate_cad(path: Path, _asset_type: str) -> Mapping[str, object]:
    extension = path.suffix.lower()
    if extension == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError("design.json must contain an object")
        return {"format": "design_json"}
    if extension == ".zip":
        with zipfile.ZipFile(path) as archive:
            for item in archive.infolist():
                candidate = PurePosixPath(item.filename.replace("\\", "/"))
                if (
                    candidate.is_absolute()
                    or ".." in candidate.parts
                    or any(":" in part for part in candidate.parts)
                ):
                    raise ValueError("CAD archive contains an unsafe path")
                mode = (item.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise ValueError("CAD archive contains a symbolic link")
            bad = archive.testzip()
            if bad is not None:
                raise ValueError(f"CAD archive member failed CRC: {bad}")
            return {"format": "assets_zip", "member_count": len(archive.infolist())}
    header = path.read_bytes()[:64]
    if extension == ".dwg" and not header.startswith(b"AC10"):
        raise ValueError("invalid DWG signature")
    if extension == ".dxf":
        from cadscene.cad.dxf_parser import parse_dxf

        _design, statistics = parse_dxf(path)
        return {
            "format": "dxf",
            "entity_count": int(statistics.get("entity_count", 0)),
            "segment_count": int(statistics.get("segment_count", 0)),
        }
    return {"format": extension.lstrip(".")}


_DEFAULT_VALIDATORS: Mapping[str, Validator] = {
    "video": _validate_video,
    "cad": _validate_cad,
    "srt": _validate_srt,
}
