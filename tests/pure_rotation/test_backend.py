from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cadscene.pure_rotation.backend import ExternalOpenGVBackend
from cadscene.pure_rotation.errors import (
    PureRotationBackendFailed,
    PureRotationBackendUnavailable,
)


POC_COMMIT = "85ab6404bfb5a07da8cdaaba0a1e5c4da10dc250"
OPENGV_COMMIT = "91f4b19c73450833a40e463ad3648aae80b3a7f3"


def _backend_root(tmp_path: Path) -> Path:
    root = tmp_path / "backend"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "run_full_video_exploration.py").write_text("# external cli\n", encoding="utf-8")
    (root / "backend_version.json").write_text(
        json.dumps({"poc_commit": POC_COMMIT, "opengv_commit": OPENGV_COMMIT}), encoding="utf-8"
    )
    return root


def test_backend_root_precedence_and_health_audits_pinned_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    configured = _backend_root(tmp_path)
    environment = _backend_root(tmp_path / "env")
    monkeypatch.setenv("PURE_ROTATION_BACKEND_ROOT", str(environment))

    backend = ExternalOpenGVBackend(backend_root=configured, backend_command=[sys.executable])

    report = backend.health_check()

    assert report["available"] is True
    assert Path(report["backend_root"]) == configured
    assert report["poc_commit"] == POC_COMMIT
    assert report["opengv_commit"] == OPENGV_COMMIT


def test_backend_command_environment_accepts_json_argument_array(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = tmp_path / "run backend.py"
    command = [sys.executable, str(runner)]
    monkeypatch.setenv("PURE_ROTATION_BACKEND_COMMAND", json.dumps(command))
    backend = ExternalOpenGVBackend()

    assert backend._command() == command


def test_missing_backend_is_a_typed_unavailable_failure(tmp_path: Path) -> None:
    backend = ExternalOpenGVBackend(backend_root=tmp_path / "missing")

    with pytest.raises(PureRotationBackendUnavailable, match="pure-rotation-backend-unavailable"):
        backend.inspect_backend()


def test_git_commit_audit_is_used_when_backend_metadata_file_is_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _backend_root(tmp_path)
    (root / "backend_version.json").unlink()
    backend = ExternalOpenGVBackend(backend_root=root, backend_command=[sys.executable])
    commits = iter((POC_COMMIT, OPENGV_COMMIT))
    monkeypatch.setattr(backend, "_git_commit", lambda path: next(commits))

    report = backend.inspect_backend()

    assert report["poc_commit"] == POC_COMMIT
    assert report["opengv_commit"] == OPENGV_COMMIT


def test_git_audit_scopes_safe_directory_to_the_read_only_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = {}
    class Completed:
        returncode = 0
        stdout = POC_COMMIT + "\n"
    def fake_run(command, **kwargs):
        recorded["command"] = command
        return Completed()
    monkeypatch.setattr("cadscene.pure_rotation.backend.subprocess.run", fake_run)

    ExternalOpenGVBackend._git_commit(tmp_path)

    assert recorded["command"][:3] == ["git", "-c", f"safe.directory={tmp_path.as_posix()}"]


def test_run_uses_argument_array_unicode_paths_and_captures_logs(tmp_path: Path) -> None:
    backend_root = _backend_root(tmp_path)
    runner = backend_root / "runner.py"
    runner.write_text(
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1]); out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'summary.json').write_text(json.dumps({'ok': True}), encoding='utf-8')\n"
        "(out / 'full_video_rotation_trajectory.json').write_text(json.dumps({'poses': []}), encoding='utf-8')\n"
        "(out / 'full_video_pairwise_rotations.csv').write_text('success\\n', encoding='utf-8')\n"
        "(out / 'pure_rotation_intervals.json').write_text('{}', encoding='utf-8')\n"
        "print('backend-ok')\n",
        encoding="utf-8",
    )
    video = tmp_path / "中文视频.mp4"
    video.write_bytes(b"video")
    output = tmp_path / "输出"
    backend = ExternalOpenGVBackend(backend_root=backend_root, backend_command=[sys.executable, str(runner)])

    result = backend.run_video(video=video, cadscene_readonly=tmp_path, output_dir=output, timeout_seconds=10)

    assert result["summary"]["ok"] is True
    assert (output / "backend_stdout.log").read_text(encoding="utf-8").strip() == "backend-ok"


def test_run_streams_actual_decoded_frame_progress(tmp_path: Path) -> None:
    backend_root = _backend_root(tmp_path)
    runner = backend_root / "progress_runner.py"
    runner.write_text(
        "import json, pathlib, sys\n"
        "out = pathlib.Path(sys.argv[sys.argv.index('--output') + 1]); out.mkdir(parents=True, exist_ok=True)\n"
        "print('evaluated through decoded frame 99', flush=True)\n"
        "print('evaluated through decoded frame 199', flush=True)\n"
        "(out / 'summary.json').write_text(json.dumps({'ok': True}), encoding='utf-8')\n"
        "(out / 'full_video_rotation_trajectory.json').write_text(json.dumps({'poses': []}), encoding='utf-8')\n"
        "(out / 'full_video_pairwise_rotations.csv').write_text('success\\n', encoding='utf-8')\n"
        "(out / 'pure_rotation_intervals.json').write_text('{}', encoding='utf-8')\n",
        encoding="utf-8",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    updates: list[tuple[int, int]] = []
    backend = ExternalOpenGVBackend(
        backend_root=backend_root,
        backend_command=[sys.executable, str(runner)],
    )

    backend.run_video(
        video=video,
        cadscene_readonly=tmp_path,
        output_dir=tmp_path / "output",
        timeout_seconds=10,
        expected_frame_count=250,
        progress_callback=lambda processed, total: updates.append((processed, total)),
    )

    assert updates == [(100, 250), (200, 250)]


def test_run_failure_reports_backend_stderr_tail(tmp_path: Path) -> None:
    backend_root = _backend_root(tmp_path)
    runner = backend_root / "failing_runner.py"
    runner.write_text(
        "import sys\n"
        "print('no same-resolution exploratory intrinsics candidate', file=sys.stderr)\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    video = tmp_path / "video.mp4"
    video.write_bytes(b"video")
    backend = ExternalOpenGVBackend(
        backend_root=backend_root,
        backend_command=[sys.executable, str(runner)],
    )

    with pytest.raises(
        PureRotationBackendFailed,
        match="no same-resolution exploratory intrinsics candidate",
    ):
        backend.run_video(
            video=video,
            cadscene_readonly=tmp_path,
            output_dir=tmp_path / "output",
            timeout_seconds=10,
        )
