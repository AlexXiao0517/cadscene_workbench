from __future__ import annotations

import socket
import subprocess
import sys
import time
import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from cadscene.cli import serve_viewer

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_serve_viewer_help_runs() -> None:
    result = subprocess.run([sys.executable, "-m", "cadscene.cli.serve_viewer", "--help"], text=True, capture_output=True, check=False)

    assert result.returncode == 0
    assert "--port" in result.stdout


def test_build_parser_accepts_storage_root() -> None:
    args = serve_viewer.build_parser().parse_args(["--storage-root", "storage"])

    assert args.storage_root == "storage"


def test_build_parser_accepts_pure_rotation_backend_configuration() -> None:
    args = serve_viewer.build_parser().parse_args(
        [
            "--pure-rotation-backend-root",
            "D:/pure_rotation_camera_poc",
            "--pure-rotation-python",
            "D:/anaconda3/envs/pure_rotation_poc/python.exe",
            "--pure-rotation-calibration-root",
            "D:/cadscene/calibrations",
        ]
    )

    assert args.pure_rotation_backend_root == "D:/pure_rotation_camera_poc"
    assert args.pure_rotation_python.endswith("pure_rotation_poc/python.exe")
    assert args.pure_rotation_calibration_root == "D:/cadscene/calibrations"


def test_pure_rotation_runtime_is_discovered_from_worktree_and_conda_layout(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "zjic2026" / "cadscene_workbench" / ".worktrees" / "stage9"
    workspace.mkdir(parents=True)
    backend = tmp_path / "zjic2026" / "pure_rotation_camera_poc"
    runner = backend / "scripts" / "run_full_video_exploration.py"
    runner.parent.mkdir(parents=True)
    runner.write_text("# pinned backend", encoding="utf-8")
    base_python = tmp_path / "anaconda3" / "python.exe"
    base_python.parent.mkdir(parents=True)
    base_python.write_bytes(b"")
    backend_python = (
        tmp_path / "anaconda3" / "envs" / "pure_rotation_poc" / "python.exe"
    )
    backend_python.parent.mkdir(parents=True)
    backend_python.write_bytes(b"")

    resolved_backend, resolved_python = serve_viewer.resolve_pure_rotation_runtime(
        backend_root=None,
        backend_python=None,
        search_from=workspace,
        executable=base_python,
    )

    assert resolved_backend == backend.resolve()
    assert resolved_python == backend_python.resolve()


def test_explicit_pure_rotation_runtime_takes_precedence_over_discovery(
    tmp_path: Path,
) -> None:
    explicit_backend = tmp_path / "configured-backend"
    explicit_python = tmp_path / "configured-python.exe"

    resolved_backend, resolved_python = serve_viewer.resolve_pure_rotation_runtime(
        backend_root=str(explicit_backend),
        backend_python=str(explicit_python),
        search_from=tmp_path / "workspace",
        executable=tmp_path / "base" / "python.exe",
    )

    assert resolved_backend == explicit_backend.resolve()
    assert resolved_python == explicit_python.resolve()


def test_pure_rotation_calibration_defaults_to_pinned_application_config(
    tmp_path: Path,
) -> None:
    application_root = tmp_path / "worktree"
    calibration_root = (
        application_root / "configs" / "pure_rotation" / "adapter-calibration"
    )
    calibration_root.mkdir(parents=True)
    (calibration_root / "cameras.txt").write_text("camera\n", encoding="utf-8")

    resolved = serve_viewer.resolve_pure_rotation_calibration_root(
        configured=None,
        application_root=application_root,
    )

    assert resolved == calibration_root.resolve()


def test_pure_rotation_calibration_does_not_reuse_arbitrary_project_output(
    tmp_path: Path,
) -> None:
    application_root = tmp_path / "worktree"
    arbitrary = application_root / "projects" / "old-job" / "cameras.txt"
    arbitrary.parent.mkdir(parents=True)
    arbitrary.write_text("unrelated camera\n", encoding="utf-8")

    resolved = serve_viewer.resolve_pure_rotation_calibration_root(
        configured=None,
        application_root=application_root,
    )

    assert resolved is None


def test_explicit_pure_rotation_calibration_root_takes_precedence(
    tmp_path: Path,
) -> None:
    application_root = tmp_path / "worktree"
    configured = tmp_path / "calibrations"

    resolved = serve_viewer.resolve_pure_rotation_calibration_root(
        configured=str(configured),
        application_root=application_root,
    )

    assert resolved == configured.resolve()


def test_main_returns_one_for_missing_storage_root(tmp_path: Path) -> None:
    missing_storage_root = tmp_path / "missing-storage"

    assert serve_viewer.main(["--root", str(tmp_path), "--storage-root", str(missing_storage_root)]) == 1


def test_viewer_server_exclusively_owns_its_port() -> None:
    server_class = getattr(serve_viewer, "ViewerHTTPServer", ThreadingHTTPServer)
    first = server_class(("127.0.0.1", 0), serve_viewer.RangeRequestHandler)
    host, port = first.server_address
    try:
        with pytest.raises(OSError):
            second = server_class((host, port), serve_viewer.RangeRequestHandler)
            second.server_close()
    finally:
        first.server_close()


def test_serve_viewer_serves_index_and_supports_range(tmp_path: Path) -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [sys.executable, "-m", "cadscene.cli.serve_viewer", "--bind", "127.0.0.1", "--port", str(port), "--storage-root", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 8
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/apps/web_camera_viewer/index.html")
                resp = conn.getresponse()
                body = resp.read().decode("utf-8", errors="ignore")
                conn.close()
                if resp.status == 200:
                    break
            except Exception as exc:  # pragma: no cover - diagnostic aid on slow CI
                last_error = exc
                time.sleep(0.1)
        else:
            raise AssertionError(f"server did not respond: {last_error}")

        assert "sourceVideo" in body
        assert "sceneContainer" in body
        conn = HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/apps/web_camera_viewer/index.html", headers={"Range": "bytes=0-15"})
        range_resp = conn.getresponse()
        data = range_resp.read()
        conn.close()

        assert range_resp.status == 206
        assert len(data) == 16
        assert range_resp.getheader("Accept-Ranges") == "bytes"

        conn = HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/apps/web_camera_viewer/vendor/three.min.js")
        js_resp = conn.getresponse()
        js_body = js_resp.read(64).decode("utf-8", errors="ignore")
        conn.close()

        assert js_resp.status == 200
        assert "javascript" in (js_resp.getheader("Content-Type") or "").lower()
        assert "THREE" in js_body or "three" in js_body.lower()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_serve_viewer_extra_root_serves_named_mount(tmp_path: Path) -> None:
    legacy_root = tmp_path / "legacy_root"
    legacy_root.mkdir()
    (legacy_root / "hello.txt").write_text("legacy-data", encoding="utf-8")
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cadscene.cli.serve_viewer",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--extra-root",
            f"legacy={legacy_root}",
            "--storage-root",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                conn = HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request("GET", "/legacy/hello.txt")
                resp = conn.getresponse()
                body = resp.read().decode("utf-8", errors="ignore")
                conn.close()
                if resp.status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("extra-root server did not respond")

        assert body == "legacy-data"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_serve_viewer_writes_ignored_suggestion_inside_run(tmp_path: Path) -> None:
    (tmp_path / "runs" / "demo" / "r1").mkdir(parents=True)
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cadscene.cli.serve_viewer",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--root",
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 8
        while time.time() < deadline:
            try:
                body = json.dumps(
                    {
                        "dataset": "demo",
                        "run_id": "r1",
                        "frame_index": 600,
                        "reason": "用户确认无需补帧",
                    }
                ).encode("utf-8")
                conn = HTTPConnection("127.0.0.1", port, timeout=1)
                conn.request(
                    "POST",
                    "/api/workflow/ignore-suggestion",
                    body=body,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
                )
                response = conn.getresponse()
                response.read()
                conn.close()
                if response.status == 200:
                    break
            except Exception:
                time.sleep(0.1)
        else:
            raise AssertionError("workflow API did not respond")

        output = tmp_path / "runs" / "demo" / "r1" / "04_quality" / "ignored_suggestions.json"
        payload = json.loads(output.read_text(encoding="utf-8"))
        assert payload["ignored"][0]["frame_index"] == 600
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
