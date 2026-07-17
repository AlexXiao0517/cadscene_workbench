from __future__ import annotations

import io
import json
import socket
import subprocess
import sys
import time
import zipfile
import ezdxf
from http.client import HTTPConnection
from pathlib import Path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _start_server(root: Path, port: int) -> subprocess.Popen:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cadscene.cli.serve_viewer",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
            "--root",
            str(root),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=1)
            conn.request("GET", "/api/workflow/list-datasets")
            response = conn.getresponse()
            response.read()
            conn.close()
            if response.status in {200, 404}:
                return process
        except Exception:
            time.sleep(0.1)
    process.kill()
    raise AssertionError("server did not start")


def _json_request(port: int, method: str, route: str, payload: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(method, route, body=body, headers=headers)
    response = conn.getresponse()
    result = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, result


def _upload(port: int, route: str, filename: str, content: bytes) -> tuple[int, dict]:
    boundary = "----cadscene-test-boundary"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(
        "POST",
        route,
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(body))},
    )
    response = conn.getresponse()
    result = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, result


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return stream.getvalue()


def _dxf_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "api-upload.dxf"
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = 6
    doc.layers.add("road_center", color=1)
    doc.modelspace().add_line((100, 200), (120, 200), dxfattribs={"layer": "road_center", "color": 1})
    doc.saveas(path)
    return path.read_bytes()


def test_create_upload_and_manifest_api_complete_upload_stage(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, created = _json_request(
            port,
            "POST",
            "/api/workflow/create-dataset",
            {"dataset": "User Demo", "runId": "r1", "cadScale": 0.06, "originX": 1, "originY": 2},
        )
        assert status == 200
        assert created["manifest"]["dataset"] == "user-demo"

        status, video = _upload(
            port,
            "/api/workflow/upload-video?dataset=user-demo&runId=r1",
            "flight.mp4",
            b"video-data",
        )
        assert status == 200
        assert video["manifest"]["video"]["url"] == "/data/user-demo/flight.mp4"

        status, cad = _upload(
            port,
            "/api/workflow/upload-cad?dataset=user-demo&runId=r1",
            "design.json",
            b'{"layers":[]}',
        )
        assert status == 200
        assert cad["manifest"]["status"] == "ready"

        status, result = _json_request(
            port, "GET", "/api/workflow/dataset-manifest?dataset=user-demo"
        )
        assert status == 200
        assert result["manifest"]["cad"]["status"] == "ready"
        job = json.loads((tmp_path / "runs/user-demo/r1/job_status.json").read_text(encoding="utf-8"))
        assert job["stages"]["upload"]["status"] == "success"
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_upload_zip_rejects_zip_slip_and_dxf_is_parsed(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        _json_request(port, "POST", "/api/workflow/create-dataset", {"dataset": "demo", "runId": "r-dxf"})
        status, payload = _upload(
            port,
            "/api/workflow/upload-cad?dataset=demo",
            "assets.zip",
            _zip_bytes({"../escape.txt": b"bad"}),
        )
        assert status == 400
        assert "unsafe zip member" in payload["error"]
        assert not (tmp_path / "data/escape.txt").exists()

        status, _ = _upload(
            port,
            "/api/workflow/upload-video?dataset=demo&runId=r-dxf",
            "flight.mp4",
            b"video",
        )
        assert status == 200

        status, payload = _upload(
            port,
            "/api/workflow/upload-cad?dataset=demo&runId=r-dxf",
            "drawing.dxf",
            _dxf_bytes(tmp_path),
        )
        assert status == 200
        assert payload["manifest"]["cad"]["status"] == "ready"
        assert payload["manifest"]["cad"]["source_type"] == "dxf_parsed"
        assert (tmp_path / "data/demo/raw_cad/drawing.dxf").exists()
        assert (tmp_path / "data/demo/design.json").exists()
        assert (tmp_path / "data/demo/cad_import_report.md").exists()
        job = json.loads((tmp_path / "runs/demo/r-dxf/job_status.json").read_text(encoding="utf-8"))
        assert job["stages"]["upload"]["status"] == "success"
        assert job["stages"]["upload"]["message"] == "视频和 CAD 已准备完成"
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_dwg_upload_does_not_claim_ready_without_converter(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        _json_request(port, "POST", "/api/workflow/create-dataset", {"dataset": "demo"})
        status, payload = _upload(
            port,
            "/api/workflow/upload-cad?dataset=demo",
            "drawing.dwg",
            b"raw-dwg",
        )
        assert status == 200
        assert payload["manifest"]["cad"]["status"] == "raw_saved"
        assert payload["manifest"]["cad"]["source_type"] == "dwg_raw_saved"
        assert any("转换工具" in warning for warning in payload["manifest"]["warnings"])
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_assets_zip_and_dataset_listing_api(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        _json_request(port, "POST", "/api/workflow/create-dataset", {"dataset": "demo"})
        status, payload = _upload(
            port,
            "/api/workflow/upload-cad?dataset=demo",
            "assets.zip",
            _zip_bytes({"nested/design.json": b'{"layers":[]}'}),
        )
        assert status == 200
        assert payload["manifest"]["cad"]["status"] == "ready"
        assert (tmp_path / "data/demo/design.json").exists()

        status, payload = _json_request(port, "GET", "/api/workflow/list-datasets")
        assert status == 200
        assert [item["dataset"] for item in payload["datasets"]] == ["demo"]
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_upload_api_rejects_unsafe_run_id_before_writing_job_status(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        _json_request(port, "POST", "/api/workflow/create-dataset", {"dataset": "demo"})
        status, payload = _upload(
            port,
            "/api/workflow/upload-cad?dataset=demo&runId=..%2F..%2Fescape",
            "drawing.dxf",
            _dxf_bytes(tmp_path),
        )
        assert status == 400
        assert "invalid run_id" in payload["error"]
        assert not (tmp_path / "escape/job_status.json").exists()
    finally:
        server.terminate()
        server.wait(timeout=5)
