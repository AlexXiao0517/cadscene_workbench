from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import mimetypes
import os
import re
import socket
import sys
import tempfile
from email.message import Message
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from cadscene.workflow.job_runner import JobAlreadyRunningError, JobRunner, save_camera_track
from cadscene.pure_rotation.placement import apply_global_placement
from cadscene.pure_rotation.corrections import apply_rotation_corrections
from cadscene.pure_rotation.rotation_matrix import normalize_rotation_fields
from cadscene.workflow.job_status import JobStatusStore, append_ignored_suggestion
from cadscene.workflow.data_import import (
    create_dataset,
    import_cad,
    import_srt,
    import_video,
    list_datasets,
    load_dataset_manifest,
    load_srt_analysis,
    slugify_dataset_name,
)
from cadscene.workflow.keyframe_plan import create_keyframe_plan, keyframe_plan_path, load_keyframe_plan, write_keyframe_plan
from cadscene.sfm.camera_init import load_sfm_camera_initialization

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class ViewerHTTPServer(ThreadingHTTPServer):
    """独占监听端口，避免多个旧服务同时处理上传请求。"""

    allow_reuse_address = False

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _storage_root(server: ThreadingHTTPServer) -> Path:
    return Path(getattr(server, "storage_root_dir", getattr(server, "root_dir", project_root()))).resolve()


def pure_rotation_options(payload: dict, extra_roots: dict[str, Path]) -> dict:
    options = dict(payload.get("options") or {})
    source_root = extra_roots.get("source")
    if source_root is not None and not options.get("cadscene_readonly"):
        options["cadscene_readonly"] = str(Path(source_root).resolve())
    return options


class RangeRequestHandler(SimpleHTTPRequestHandler):
    server_version = "CadsceneViewerHTTP/0.1"

    def translate_path(self, path: str) -> str:
        root = Path(getattr(self.server, "root_dir", project_root())).resolve()
        clean = unquote(path.split("?", 1)[0].split("#", 1)[0]).lstrip("/")
        parts = clean.split("/", 1)
        extra_roots = getattr(self.server, "extra_roots", {})
        if parts and parts[0] in extra_roots:
            mount_root = Path(extra_roots[parts[0]]).resolve()
            rest = parts[1] if len(parts) > 1 else ""
            candidate = (mount_root / rest).resolve()
            if mount_root in (candidate, *candidate.parents):
                return str(candidate)
            return str(mount_root)
        candidate = (root / clean).resolve()
        if root not in (candidate, *candidate.parents):
            return str(root)
        return str(candidate)

    def end_headers(self) -> None:
        self.send_header("Accept-Ranges", "bytes")
        viewer_path = urlsplit(self.path).path
        if viewer_path.startswith("/apps/web_camera_viewer/") and Path(viewer_path).suffix.lower() in {".html", ".js", ".css"}:
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json_response(self, status: HTTPStatus, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _project_response(self, response) -> None:
        body = response.encoded_body
        self.send_response(response.status)
        for name, value in response.headers.items():
            self.send_header(name, value)
        if body:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _dispatch_project_api(self, method: str) -> None:
        from cadscene.projects.http_api import ApiResponse, UploadRequest

        api = getattr(self.server, "project_api", None)
        if api is None:
            self._project_response(ApiResponse(503, {"error": "project_api_unavailable"}))
            return
        parsed = urlsplit(self.path)
        try:
            if method == "GET":
                response = api.handle(
                    method, parsed.path, headers={name: value for name, value in self.headers.items()}
                )
            elif method == "POST" and "/uploads/" in parsed.path:
                filename, stream = self._multipart_upload()
                try:
                    stream.seek(0, os.SEEK_END)
                    size = stream.tell()
                    stream.seek(0)
                    query = parse_qs(parsed.query)
                    raw_revision = (query.get("expectedRevision") or query.get("expected_revision") or [""])[0]
                    if not str(raw_revision).isdigit():
                        raise ValueError("expectedRevision query parameter is required")
                    response = api.handle(
                        method,
                        parsed.path,
                        json_body={"expected_revision": int(raw_revision)},
                        upload=UploadRequest(
                            filename=filename,
                            stream=stream,
                            size_bytes=size,
                            sha256=self.headers.get("X-Content-SHA256"),
                        ),
                    )
                finally:
                    stream.close()
            else:
                response = api.handle(
                    method,
                    parsed.path,
                    headers={name: value for name, value in self.headers.items()},
                    json_body=self._read_json_body(),
                )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            response = ApiResponse(400, {"error": str(exc)})
        self._project_response(response)

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 10 * 1024 * 1024:
            raise ValueError("invalid request body length")
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _query_value(self, name: str, *, required: bool = False) -> str:
        values = parse_qs(urlsplit(self.path).query).get(name) or []
        value = str(values[0]).strip() if values else ""
        if required and not value:
            raise ValueError(f"missing query parameter: {name}")
        return value

    def _multipart_upload(self) -> tuple[str, object]:
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Content-Type must be multipart/form-data")
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            raise ValueError("empty upload body")
        message = Message()
        message["Content-Type"] = content_type
        boundary_text = message.get_boundary()
        if not boundary_text:
            raise ValueError("multipart boundary is missing")
        boundary = boundary_text.encode("ascii", errors="strict")
        remaining = content_length

        first_line = self.rfile.readline(min(remaining + 1, 64 * 1024))
        remaining -= len(first_line)
        if first_line.rstrip(b"\r\n") != b"--" + boundary:
            raise ValueError("invalid multipart boundary")

        part_headers: dict[str, str] = {}
        while remaining > 0:
            line = self.rfile.readline(min(remaining + 1, 64 * 1024))
            remaining -= len(line)
            if line in {b"\r\n", b"\n", b""}:
                break
            name, separator, value = line.decode("latin-1").partition(":")
            if not separator:
                raise ValueError("invalid multipart header")
            part_headers[name.strip().lower()] = value.strip()

        disposition = Message()
        disposition["Content-Disposition"] = part_headers.get("content-disposition", "")
        field_name = disposition.get_param("name", header="content-disposition")
        filename = disposition.get_filename()
        if field_name != "file" or not filename:
            raise ValueError("missing multipart field: file")
        try:
            filename = filename.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass

        temporary = tempfile.TemporaryFile(mode="w+b")
        marker = b"\r\n--" + boundary
        keep = len(marker) + 4
        buffer = b""
        try:
            while remaining > 0:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                buffer += chunk
                marker_index = buffer.find(marker)
                if marker_index >= 0:
                    temporary.write(buffer[:marker_index])
                    temporary.seek(0)
                    return Path(str(filename)).name, temporary
                if len(buffer) > keep:
                    temporary.write(buffer[:-keep])
                    buffer = buffer[-keep:]
        except Exception:
            temporary.close()
            raise
        temporary.close()
        raise ValueError("multipart closing boundary is missing")

    def _update_upload_status(self, dataset: str, run_id: str, manifest: dict) -> None:
        if not run_id:
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("invalid run_id")
        run_dir = _storage_root(self.server) / "runs" / dataset / run_id
        ready = manifest.get("status") == "ready"
        failed = manifest.get("status") == "failed" or (manifest.get("cad") or {}).get("status") == "failed"
        video_ready = bool((manifest.get("video") or {}).get("path"))
        cad_ready = (manifest.get("cad") or {}).get("status") == "ready"
        progress = 1.0 if ready else (0.5 if video_ready or cad_ready else 0.0)
        if failed:
            message = str((manifest.get("cad") or {}).get("error") or "CAD 解析失败")
        elif ready:
            message = "视频和 CAD 已准备完成"
        elif (manifest.get("cad") or {}).get("status") == "raw_saved":
            message = "DWG 已保存，但当前环境缺少转换工具，请上传 DXF 或安装转换器"
        elif video_ready:
            message = "视频已上传，等待 CAD assets"
        elif cad_ready:
            message = "CAD 已准备，等待视频"
        else:
            message = "等待上传视频和 CAD assets"
        trajectory_mode = str((manifest.get("workflow") or {}).get("trajectory_mode", "sfm_only"))
        if not failed and trajectory_mode == "srt_sfm_fused":
            message = "SRT/SfM fusion functionality pending activation"
        elif not failed and trajectory_mode == "srt_full_pose":
            message = "SRT full-pose functionality pending activation"
        JobStatusStore(run_dir / "job_status.json", run_id=run_id).update_stage(
            "upload",
            status="failed" if failed else ("success" if ready else "pending"),
            progress=progress,
            message=message,
            error=message if failed else None,
        )

    @staticmethod
    def _srt_api_payload(dataset: str, manifest: dict, analysis: dict) -> dict:
        srt = manifest.get("srt") or {}
        mode = str((manifest.get("workflow") or {}).get("trajectory_mode", "sfm_only"))
        if mode == "srt_sfm_fused":
            message = "SRT/SfM fusion functionality pending activation"
        elif mode == "srt_full_pose":
            message = "SRT full-pose functionality pending activation"
        else:
            message = "SRT analysis is available; SfM-only workflow remains active"
        return {
            "ok": True,
            "dataset": dataset,
            "srt_status": str(srt.get("status", "missing")),
            "trajectory_mode": mode,
            "analysis": analysis,
            "message": message,
        }

    def _set_upload_progress(self, dataset: str, run_id: str, message: str) -> None:
        if not run_id:
            return
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
            raise ValueError("invalid run_id")
        progress_by_message = {
            "正在保存原始 CAD": 0.55,
            "正在转换 DWG": 0.65,
            "正在解析 DXF": 0.72,
            "正在生成 design.json": 0.88,
            "CAD 解析完成": 0.95,
            "CAD 解析失败": 0.5,
        }
        run_dir = _storage_root(self.server) / "runs" / dataset / run_id
        failed = message == "CAD 解析失败"
        JobStatusStore(run_dir / "job_status.json", run_id=run_id).update_stage(
            "upload",
            status="failed" if failed else "running",
            progress=progress_by_message.get(message, 0.5),
            message=message,
            error=message if failed else None,
        )

    def _workflow_identity(self, payload: dict) -> tuple[str, str]:
        dataset = str(payload.get("dataset", ""))
        run_id = str(payload.get("runId") or payload.get("run_id") or "")
        safe_name = re.compile(r"^[A-Za-z0-9_.-]+$")
        if not safe_name.fullmatch(dataset) or not safe_name.fullmatch(run_id):
            raise ValueError("invalid dataset or run_id")
        return dataset, run_id

    def _workflow_run_dir(self, payload: dict, *, create: bool = False) -> Path:
        dataset, run_id = self._workflow_identity(payload)
        root = _storage_root(self.server)
        run_dir = (root / "runs" / dataset / run_id).resolve()
        runs_root = (root / "runs").resolve()
        if runs_root not in run_dir.parents:
            raise ValueError("run directory escapes runs root")
        if create:
            run_dir.mkdir(parents=True, exist_ok=True)
        elif not run_dir.exists():
            raise FileNotFoundError("run directory not found")
        return run_dir

    def do_POST(self) -> None:
        route = urlsplit(self.path).path
        if route == "/api/projects" or route.startswith("/api/projects/"):
            self._dispatch_project_api("POST")
            return
        upload_routes = {
            "/api/workflow/create-dataset",
            "/api/workflow/upload-video",
            "/api/workflow/upload-cad",
            "/api/workflow/upload-srt",
        }
        allowed_routes = {
            "/api/workflow/ignore-suggestion",
            "/api/workflow/job-status",
            "/api/workflow/run-stage",
            "/api/workflow/cancel",
            "/api/workflow/save-camera-track",
            "/api/workflow/generate-keyframe-plan",
            "/api/pure-rotation/run",
            "/api/pure-rotation/placement",
            "/api/pure-rotation/corrections",
        } | upload_routes
        if route not in allowed_routes:
            self.send_error(HTTPStatus.NOT_FOUND, "API not found")
            return
        try:
            if route == "/api/workflow/create-dataset":
                payload = self._read_json_body()
                manifest = create_dataset(
                    _storage_root(self.server),
                    str(payload.get("dataset", "")),
                    cad_scale=float(payload.get("cadScale", 0.06)),
                    origin_xy=(
                        float(payload.get("originX", 567747.5756295)),
                        float(payload.get("originY", 3330464.2234675)),
                    ),
                    hovering_declared=(bool(payload["hoveringDeclared"]) if "hoveringDeclared" in payload else None),
                )
                self._update_upload_status(
                    str(manifest["dataset"]),
                    str(payload.get("runId") or payload.get("run_id") or ""),
                    manifest,
                )
                self._json_response(HTTPStatus.OK, {"ok": True, "manifest": manifest})
                return
            if route in {"/api/workflow/upload-video", "/api/workflow/upload-cad", "/api/workflow/upload-srt"}:
                dataset = slugify_dataset_name(self._query_value("dataset", required=True))
                run_id = self._query_value("runId") or self._query_value("run_id")
                if run_id and not re.fullmatch(r"[A-Za-z0-9_.-]+", run_id):
                    raise ValueError("invalid run_id")
                filename, stream = self._multipart_upload()
                try:
                    if route.endswith("upload-video"):
                        manifest = import_video(_storage_root(self.server), dataset, filename, stream)
                    elif route.endswith("upload-srt"):
                        manifest = import_srt(_storage_root(self.server), dataset, filename, stream)
                    else:
                        manifest = import_cad(
                            _storage_root(self.server),
                            dataset,
                            filename,
                            stream,
                            status_callback=lambda value: self._set_upload_progress(dataset, run_id, value),
                        )
                finally:
                    stream.close()
                self._update_upload_status(dataset, run_id, manifest)
                if route.endswith("upload-srt"):
                    analysis = load_srt_analysis(_storage_root(self.server), dataset)
                    self._json_response(HTTPStatus.OK, self._srt_api_payload(dataset, manifest, analysis))
                else:
                    self._json_response(HTTPStatus.OK, {"ok": True, "manifest": manifest})
                return
            payload = self._read_json_body()
            dataset, run_id = self._workflow_identity(payload)
            run_dir = self._workflow_run_dir(
                payload,
                create=route in {
                    "/api/workflow/run-stage",
                    "/api/workflow/save-camera-track",
                    "/api/workflow/generate-keyframe-plan",
                },
            )
            runner: JobRunner = self.server.job_runner
            if route == "/api/pure-rotation/run":
                manifest = load_dataset_manifest(_storage_root(self.server), dataset)
                if (manifest.get("workflow") or {}).get("trajectory_mode") != "pure_rotation":
                    raise ValueError("dataset is not routed to pure_rotation")
                options = pure_rotation_options(payload, getattr(self.server, "extra_roots", {}))
                result = {"ok": True, **runner.start_stage(dataset, run_id, "pure_rotation", options)}
            elif route == "/api/pure-rotation/placement":
                raw_path = run_dir / "02_pure_rotation" / "camera_rotation_raw.json"
                if not raw_path.exists():
                    raise FileNotFoundError("pure-rotation raw trajectory not found")
                raw = json.loads(raw_path.read_text(encoding="utf-8-sig"))
                placement = normalize_rotation_fields(
                    payload.get("placement") or {},
                    ("manual_rotation_cad_from_camera",),
                    allow_legacy_reflection=True,
                )
                base = apply_global_placement(raw, segment_id=int(placement["segment_id"]), anchor_decoded_frame_index=int(placement["anchor_decoded_frame_index"]), camera_center_web=placement["camera_center_web"], manual_rotation_cad_from_camera=placement["manual_rotation_cad_from_camera"], fov=float(placement["fov"]))
                output = run_dir / "03_pure_rotation_placement" / "camera_track_cad_base.json"
                _atomic_json(run_dir / "03_pure_rotation_placement" / "global_camera_placement.json", placement)
                _atomic_json(output, base)
                result = {"ok": True, "path": str(output)}
            elif route == "/api/pure-rotation/corrections":
                base_path = run_dir / "03_pure_rotation_placement" / "camera_track_cad_base.json"
                if not base_path.exists():
                    raise FileNotFoundError("pure-rotation segment is not calibrated")
                corrections = [
                    normalize_rotation_fields(
                        item,
                        ("base_rotation_cad_from_camera", "manual_rotation_cad_from_camera"),
                        allow_legacy_reflection=True,
                    )
                    for item in (payload.get("corrections") or [])
                ]
                corrected = apply_rotation_corrections(json.loads(base_path.read_text(encoding="utf-8-sig")), corrections)
                output = run_dir / "04_pure_rotation_corrections" / "camera_track_corrected.json"
                _atomic_json(run_dir / "04_pure_rotation_corrections" / "rotation_correction_keyframes.json", {"schema_version": 1, "corrections": corrections})
                _atomic_json(output, corrected)
                result = {"ok": True, "path": str(output)}
            elif route == "/api/workflow/run-stage":
                stage = str(payload.get("stage", ""))
                try:
                    manifest = load_dataset_manifest(_storage_root(self.server), dataset)
                except FileNotFoundError:
                    # Preserve legacy direct-run compatibility for no-SRT datasets.
                    manifest = {}
                workflow = manifest.get("workflow") or {}
                trajectory_mode = str(workflow.get("trajectory_mode", "sfm_only"))
                if trajectory_mode in {"srt_sfm_fused", "srt_full_pose"} or workflow.get("implementation_status") == "interface_only":
                    self._json_response(
                        HTTPStatus.CONFLICT,
                        {"error": "SRT 轨迹功能待启用，当前模式不能启动处理流程。"},
                    )
                    return
                result = runner.start_stage(dataset, run_id, stage, payload.get("options") or {})
                result = {"ok": True, **result}
            elif route == "/api/workflow/cancel":
                result = {"ok": True, **runner.cancel(dataset, run_id)}
            elif route == "/api/workflow/save-camera-track":
                camera_track = payload.get("cameraTrack")
                if not isinstance(camera_track, dict):
                    raise ValueError("cameraTrack must be a JSON object")
                output = save_camera_track(_storage_root(self.server), dataset, run_id, camera_track)
                result = {"ok": True, "path": str(output)}
            elif route == "/api/workflow/generate-keyframe-plan":
                if not (run_dir / "03_alignment" / "alignment.json").exists():
                    raise ValueError("complete initial route fitting before generating a keyframe plan")
                track_path = run_dir / "01_keyframes" / "camera_track_manual.json"
                if not track_path.exists():
                    raise FileNotFoundError("save at least one manual keyframe before generating a plan")
                camera_track = json.loads(track_path.read_text(encoding="utf-8-sig"))
                plan = create_keyframe_plan(
                    run_dir / "02_sfm" / "camera_trajectory.json",
                    camera_track,
                    interval_frames=int(payload.get("intervalFrames", 0)),
                )
                output = write_keyframe_plan(keyframe_plan_path(run_dir), plan)
                result = {"ok": True, "path": str(output), "plan": plan}
            elif route.endswith("ignore-suggestion"):
                result = append_ignored_suggestion(
                    run_dir / "04_quality" / "ignored_suggestions.json",
                    frame_index=int(payload["frame_index"]),
                    reason=str(payload.get("reason") or "用户确认无需补帧"),
                )
            else:
                result = JobStatusStore(
                    run_dir / "job_status.json",
                    run_id=run_id,
                ).update_stage(
                    str(payload["stage"]),
                    status=str(payload.get("status", "running")),
                    progress=float(payload.get("progress", 0.0)),
                    message=str(payload.get("message", "")),
                    error=str(payload["error"]) if payload.get("error") else None,
                )
            self._json_response(HTTPStatus.OK, result)
        except JobAlreadyRunningError as exc:
            self._json_response(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (ValueError, KeyError, TypeError) as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json_response(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:
            self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_PATCH(self) -> None:
        route = urlsplit(self.path).path
        if route.startswith("/api/projects/"):
            self._dispatch_project_api("PATCH")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "API not found")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/api/projects/"):
            self._dispatch_project_api("GET")
            return
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/apps/workflow_portal/index.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        api_routes = {
            "/api/workflow/job-log",
            "/api/workflow/sfm-camera-init",
            "/api/workflow/keyframe-plan",
            "/api/workflow/dataset-manifest",
            "/api/workflow/list-datasets",
            "/api/workflow/srt-analysis",
            "/api/pure-rotation/status",
            "/api/pure-rotation/trajectory",
        }
        if parsed.path not in api_routes:
            return super().do_GET()
        try:
            query = parse_qs(parsed.query)
            if parsed.path == "/api/workflow/list-datasets":
                self._json_response(
                    HTTPStatus.OK,
                    {"ok": True, "datasets": list_datasets(_storage_root(self.server))},
                )
                return
            if parsed.path == "/api/workflow/dataset-manifest":
                dataset = slugify_dataset_name((query.get("dataset") or [""])[0])
                self._json_response(
                    HTTPStatus.OK,
                    {"ok": True, "manifest": load_dataset_manifest(_storage_root(self.server), dataset)},
                )
                return
            if parsed.path == "/api/workflow/srt-analysis":
                dataset = slugify_dataset_name(self._query_value("dataset", required=True))
                manifest = load_dataset_manifest(_storage_root(self.server), dataset)
                analysis = load_srt_analysis(_storage_root(self.server), dataset)
                self._json_response(HTTPStatus.OK, self._srt_api_payload(dataset, manifest, analysis))
                return
            payload = {
                "dataset": (query.get("dataset") or [""])[0],
                "runId": (query.get("runId") or [""])[0],
            }
            dataset, run_id = self._workflow_identity(payload)
            if parsed.path == "/api/pure-rotation/status":
                run_dir = self._workflow_run_dir(payload)
                summary = run_dir / "02_pure_rotation" / "backend_summary.json"
                if not summary.exists():
                    summary = run_dir / "02_pure_rotation" / "summary.json"
                placement = run_dir / "03_pure_rotation_placement" / "global_camera_placement.json"
                corrections = run_dir / "04_pure_rotation_corrections" / "rotation_correction_keyframes.json"
                self._json_response(HTTPStatus.OK, {"ok": True, "summary": json.loads(summary.read_text(encoding="utf-8-sig")) if summary.exists() else None, "placement": json.loads(placement.read_text(encoding="utf-8-sig")) if placement.exists() else None, "corrections": json.loads(corrections.read_text(encoding="utf-8-sig")) if corrections.exists() else {"schema_version": 1, "corrections": []}})
                return
            if parsed.path == "/api/pure-rotation/trajectory":
                run_dir = self._workflow_run_dir(payload)
                kind = str((query.get("kind") or ["raw"])[0])
                paths = {"raw": run_dir / "02_pure_rotation" / "camera_rotation_raw.json", "base": run_dir / "03_pure_rotation_placement" / "camera_track_cad_base.json", "corrected": run_dir / "04_pure_rotation_corrections" / "camera_track_corrected.json"}
                if kind not in paths or not paths[kind].exists():
                    raise FileNotFoundError("pure-rotation trajectory not found")
                self._json_response(HTTPStatus.OK, {"ok": True, "kind": kind, "trajectory": json.loads(paths[kind].read_text(encoding="utf-8-sig"))})
                return
            if parsed.path == "/api/workflow/sfm-camera-init":
                run_dir = self._workflow_run_dir(payload)
                result = load_sfm_camera_initialization(run_dir / "02_sfm" / "camera_trajectory.json")
                self._json_response(HTTPStatus.OK, result)
                return
            if parsed.path == "/api/workflow/keyframe-plan":
                run_dir = self._workflow_run_dir(payload)
                plan_path = keyframe_plan_path(run_dir)
                if not plan_path.exists():
                    raise FileNotFoundError("keyframe plan not found")
                self._json_response(HTTPStatus.OK, load_keyframe_plan(plan_path))
                return
            stage = (query.get("stage") or [""])[0]
            tail = int((query.get("tail") or ["200"])[0])
            result = self.server.job_runner.tail_log(dataset, run_id, stage, tail)
            self._json_response(HTTPStatus.OK, result)
        except (ValueError, KeyError, TypeError) as exc:
            self._json_response(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except FileNotFoundError as exc:
            self._json_response(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:
            self._json_response(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def send_head(self):
        path = Path(self.translate_path(self.path))
        if path.is_dir():
            path = path / "index.html"
        if not path.exists() or not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None
        size = path.stat().st_size
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        range_header = self.headers.get("Range")
        if range_header:
            start, end = self._parse_range(range_header, size)
            if start is None:
                self.send_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid range")
                return None
            f = path.open("rb")
            f.seek(start)
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-type", ctype)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.range = (start, end)
            return f
        f = path.open("rb")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-type", ctype)
        self.send_header("Content-Length", str(size))
        self.end_headers()
        self.range = None
        return f

    def copyfile(self, source, outputfile) -> None:
        byte_range = getattr(self, "range", None)
        if not byte_range:
            return super().copyfile(source, outputfile)
        start, end = byte_range
        remaining = end - start + 1
        while remaining > 0:
            chunk = source.read(min(64 * 1024, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    @staticmethod
    def _parse_range(header: str, size: int) -> tuple[int | None, int | None]:
        if not header.startswith("bytes=") or "," in header:
            return None, None
        spec = header[len("bytes=") :]
        start_s, _, end_s = spec.partition("-")
        try:
            if start_s:
                start = int(start_s)
                end = int(end_s) if end_s else size - 1
            else:
                suffix = int(end_s)
                start = max(0, size - suffix)
                end = size - 1
        except ValueError:
            return None, None
        if start < 0 or end < start or start >= size:
            return None, None
        return start, min(end, size - 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Serve cadscene web_camera_viewer and run artifacts with HTTP Range support.")
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8300)
    parser.add_argument("--root", default=str(project_root()))
    parser.add_argument(
        "--storage-root",
        default=None,
        help="Store workflow data and runs under this root; defaults to --root.",
    )
    parser.add_argument("--extra-root", action="append", default=[], metavar="NAME=PATH", help="Mount an additional read-only root, for example legacy=D:\\data.")
    return parser


def parse_extra_roots(items: list[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for item in items:
        name, sep, raw_path = item.partition("=")
        if not sep or not name or "/" in name or "\\" in name:
            raise ValueError(f"invalid --extra-root {item!r}, expected NAME=PATH")
        path = Path(raw_path).resolve()
        if not path.exists():
            raise FileNotFoundError(f"extra-root not found: {path}")
        roots[name] = path
    return roots


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(args.root).resolve()
    storage_root = Path(args.storage_root).resolve() if args.storage_root else root
    if not root.exists():
        print(f"root not found: {root}", file=sys.stderr)
        return 1
    if not storage_root.exists():
        print(f"storage root not found: {storage_root}", file=sys.stderr)
        return 1
    try:
        extra_roots = parse_extra_roots(args.extra_root)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if storage_root != root and {"data", "runs"} & extra_roots.keys():
        print("--extra-root names data and runs are reserved when --storage-root differs from --root", file=sys.stderr)
        return 1
    served_roots = dict(extra_roots)
    if storage_root != root:
        served_roots["data"] = storage_root / "data"
        served_roots["runs"] = storage_root / "runs"
    try:
        server = ViewerHTTPServer((args.bind, args.port), RangeRequestHandler)
    except OSError as exc:
        print(
            f"无法启动 viewer：{args.bind}:{args.port} 已被占用。请先关闭旧的 serve_viewer 进程。({exc})",
            file=sys.stderr,
        )
        return 1
    server.root_dir = root
    server.storage_root_dir = storage_root
    server.extra_roots = served_roots
    server.job_runner = JobRunner(storage_root)
    from cadscene.projects.executor import LocalJobExecutor
    from cadscene.projects.http_api import ProjectApi
    from cadscene.projects.json_repositories import project_repositories
    from cadscene.projects.queue import LocalResourceQueue
    from cadscene.projects.service import ProjectService
    from cadscene.projects.runtime import ProjectRuntime
    from cadscene.projects.uploads import ValidatedUploadStore
    from cadscene.projects.workflow_adapters import default_workflow_adapters

    projects_root = storage_root / "projects"
    projects_root.mkdir(parents=True, exist_ok=True)
    repositories = project_repositories(projects_root)
    queue = LocalResourceQueue()
    project_service = ProjectService(
        repositories,
        queue,
        default_workflow_adapters(),
        projects_root=projects_root,
        now=lambda: datetime.now(timezone.utc).isoformat(),
    )
    project_runtime = ProjectRuntime(
        projects_root=projects_root,
        repositories=repositories,
        service=project_service,
        executor=LocalJobExecutor(project_service),
        analysis=None,
    )
    try:
        project_runtime.start()
    except Exception as exc:
        server.server_close()
        print(f"unable to acquire or recover project root: {exc}", file=sys.stderr)
        return 1
    server.project_api = ProjectApi(
        repositories=repositories,
        service=project_service,
        uploads=ValidatedUploadStore(projects_root),
        now=lambda: datetime.now(timezone.utc).isoformat(),
        analysis_trigger=lambda project_id, _asset_type, _upload: (
            project_service.enqueue_analysis_jobs(project_id)
        ),
    )
    server.project_runtime = project_runtime
    url = f"http://{args.bind}:{args.port}/apps/web_camera_viewer/?dataset=<dataset>&runId=<run_id>"
    print(f"Serving {root}")
    for name, path in served_roots.items():
        print(f"Extra root /{name}/ -> {path}")
    print(f"Viewer URL: {url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            project_runtime.close()
        finally:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
