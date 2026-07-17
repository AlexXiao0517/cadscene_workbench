from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
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
            conn.request("GET", "/missing")
            conn.getresponse().read()
            conn.close()
            return process
        except Exception:
            time.sleep(0.1)
    process.kill()
    raise AssertionError("server did not start")


def _post(port: int, route: str, payload: dict) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    conn = HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request("POST", route, body=body, headers={"Content-Type": "application/json", "Content-Length": str(len(body))})
    response = conn.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    conn.close()
    return response.status, data


def test_run_stage_api_rejects_non_whitelisted_stage(tmp_path: Path) -> None:
    (tmp_path / "runs/demo/r1").mkdir(parents=True)
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, payload = _post(
            port,
            "/api/workflow/run-stage",
            {"dataset": "demo", "runId": "r1", "stage": "arbitrary", "options": {}},
        )
        assert status == 400
        assert "stage" in payload["error"]
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_dataset_manifest_api_preserves_no_srt_workflow_defaults(tmp_path: Path) -> None:
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, _ = _post(port, "/api/workflow/create-dataset", {"dataset": "demo"})
        assert status == 200
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/workflow/dataset-manifest?dataset=demo")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        assert response.status == 200
        assert payload["manifest"]["srt"]["status"] == "missing"
        assert payload["manifest"]["workflow"] == {
            "trajectory_mode": "sfm_only",
            "debug_override": None,
            "implementation_status": "ready",
        }
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_run_stage_api_accepts_alignment_stage_for_route_fitting(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/demo/r-align"
    (run_dir / "02_sfm").mkdir(parents=True)
    (run_dir / "02_sfm/camera_trajectory.json").write_text("{}", encoding="utf-8")
    (run_dir / "02_sfm/sparse_points.ply").write_text("ply\n", encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        '{"keyframes":[{"frame":0,"source":"manual_anchor","camera":{}},{"frame":10,"source":"manual_anchor","camera":{}}]}',
        encoding="utf-8",
    )
    data = tmp_path / "data/demo"
    data.mkdir(parents=True)
    (data / "demo.mp4").write_bytes(b"video")
    (data / "cad").mkdir()
    (data / "cad/design.json").write_text("{}", encoding="utf-8")
    configs = tmp_path / "configs/datasets"
    configs.mkdir(parents=True)
    (configs / "demo.yaml").write_text(
        "dataset_name: demo\nvideo_path: data/demo/demo.mp4\ncad_dir: data/demo/cad\ncad_scale: 1.0\norigin_xy: [0, 0]\n",
        encoding="utf-8",
    )
    pipeline = tmp_path / "configs/pipelines"
    pipeline.mkdir()
    (pipeline / "sfm_overlay_existing_sfm.yaml").write_text("stages: {}\n", encoding="utf-8")
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, payload = _post(
            port,
            "/api/workflow/run-stage",
            {"dataset": "demo", "runId": "r-align", "stage": "alignment", "options": {}},
        )
        assert status == 200
        assert payload["stage"] == "alignment"
        assert payload["status"] == "running"
    finally:
        _post(port, "/api/workflow/cancel", {"dataset": "demo", "runId": "r-align"})
        server.terminate()
        server.wait(timeout=5)


def test_save_camera_track_api_writes_keyframe_artifact(tmp_path: Path) -> None:
    (tmp_path / "runs/demo/r2").mkdir(parents=True)
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        track = {"fps": 25, "keyframes": [{"frame": 0, "camera": {"x": 1}}]}
        status, payload = _post(
            port,
            "/api/workflow/save-camera-track",
            {"dataset": "demo", "runId": "r2", "cameraTrack": track},
        )
        assert status == 200
        output = tmp_path / "runs/demo/r2/01_keyframes/camera_track_manual.json"
        assert json.loads(output.read_text(encoding="utf-8")) == track
        assert Path(payload["path"]) == output
        job_status = json.loads((tmp_path / "runs/demo/r2/job_status.json").read_text(encoding="utf-8"))
        assert job_status["stages"]["keyframes"]["status"] == "success"
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_generate_keyframe_plan_api_persists_pending_frames(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/demo/r-plan"
    trajectory = run_dir / "02_sfm/camera_trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        json.dumps(
            {
                "poses": [
                    {"frame_index": frame, "registered": True, "center": [frame, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]}
                    for frame in (0, 120, 240, 300)
                ]
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        json.dumps({"keyframes": [{"frame": 0, "source": "manual_keyframe", "camera": {"x": 0}}]}),
        encoding="utf-8",
    )
    (run_dir / "03_alignment").mkdir()
    (run_dir / "03_alignment/alignment.json").write_text("{}", encoding="utf-8")
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, payload = _post(
            port,
            "/api/workflow/generate-keyframe-plan",
            {"dataset": "demo", "runId": "r-plan", "intervalFrames": 120},
        )

        assert status == 200
        assert [item["frame_index"] for item in payload["plan"]["frames"]] == [0, 120, 240, 300]
        assert (run_dir / "01_keyframes/keyframe_plan.json").exists()
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_generate_keyframe_plan_api_requires_initial_route_fit(tmp_path: Path) -> None:
    run_dir = tmp_path / "runs/demo/r-plan-no-fit"
    trajectory = run_dir / "02_sfm/camera_trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(json.dumps({"poses": [{"frame_index": 0, "registered": True, "center": [0, 0, 0], "cam_from_world_quat_wxyz": [1, 0, 0, 0]}]}), encoding="utf-8")
    (run_dir / "01_keyframes").mkdir()
    (run_dir / "01_keyframes/camera_track_manual.json").write_text(
        json.dumps({"keyframes": [{"frame": 0, "source": "manual_keyframe", "camera": {"x": 0}}]}),
        encoding="utf-8",
    )
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        status, payload = _post(
            port,
            "/api/workflow/generate-keyframe-plan",
            {"dataset": "demo", "runId": "r-plan-no-fit", "intervalFrames": 120},
        )

        assert status == 400
        assert "route fitting" in payload["error"]
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_job_log_api_returns_requested_tail(tmp_path: Path) -> None:
    log = tmp_path / "runs/demo/r3/logs/workflow/sfm.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/workflow/job-log?dataset=demo&runId=r3&stage=sfm&tail=2")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        assert response.status == 200
        assert payload["lines"] == ["two", "three"]
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_sfm_camera_init_api_returns_angles_without_position(tmp_path: Path) -> None:
    trajectory = tmp_path / "runs/demo/r4/02_sfm/camera_trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        json.dumps(
            {
                "width": 100,
                "intrinsics": [{"width": 100, "params": [50, 50, 50, 50]}],
                "poses": [
                    {
                        "frame_index": 0,
                        "registered": True,
                        "center": [9, 8, 7],
                        "cam_from_world_quat_wxyz": [1, 0, 0, 0],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/workflow/sfm-camera-init?dataset=demo&runId=r4")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        assert response.status == 200
        assert set(payload) >= {"frame_index", "yaw", "pitch", "roll", "fov"}
        assert not {"x", "y", "z", "center"}.intersection(payload)
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_sfm_camera_init_api_returns_angles_without_position(tmp_path: Path) -> None:
    trajectory = tmp_path / "runs/demo/r4/02_sfm/camera_trajectory.json"
    trajectory.parent.mkdir(parents=True)
    trajectory.write_text(
        json.dumps(
            {
                "width": 100,
                "intrinsics": [{"width": 100, "params": [50, 50, 50, 50]}],
                "poses": [
                    {
                        "frame_index": 0,
                        "registered": True,
                        "center": [9, 8, 7],
                        "cam_from_world_quat_wxyz": [1, 0, 0, 0],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/workflow/sfm-camera-init?dataset=demo&runId=r4")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        assert response.status == 200
        assert set(payload) >= {"frame_index", "yaw", "pitch", "roll", "fov"}
        assert not {"x", "y", "z", "center"}.intersection(payload)
    finally:
        server.terminate()
        server.wait(timeout=5)


def test_job_log_api_returns_requested_tail(tmp_path: Path) -> None:
    log = tmp_path / "runs/demo/r3/logs/workflow/sfm.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    port = _free_port()
    server = _start_server(tmp_path, port)
    try:
        conn = HTTPConnection("127.0.0.1", port, timeout=3)
        conn.request("GET", "/api/workflow/job-log?dataset=demo&runId=r3&stage=sfm&tail=2")
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        conn.close()

        assert response.status == 200
        assert payload["lines"] == ["two", "three"]
    finally:
        server.terminate()
        server.wait(timeout=5)
