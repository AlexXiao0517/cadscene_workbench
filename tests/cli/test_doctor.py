from __future__ import annotations

import json
from pathlib import Path


def _healthy_probes():
    from cadscene.cli.doctor import DoctorProbes

    return DoctorProbes(
        module_available=lambda _name: True,
        executable_path=lambda name: f"C:/tools/{name}.exe",
        resources_check=lambda _root: (True, "official apps and configs found"),
        storage_check=lambda _root: (True, "storage root is writable"),
        pure_rotation_check=lambda _options: (
            True,
            "pinned OpenGV backend and Python environment found",
        ),
    )


def test_doctor_reports_a_complete_supported_environment(tmp_path: Path) -> None:
    from cadscene.cli.doctor import DoctorOptions, run_doctor

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=_healthy_probes(),
    )

    assert report.complete_capability is True
    assert {check.group for check in report.checks} == {
        "core",
        "media",
        "sfm",
        "pure_rotation",
        "storage",
    }
    assert all(check.status == "ok" for check in report.checks)


def test_missing_pycolmap_is_a_supported_sfm_failure(tmp_path: Path) -> None:
    from cadscene.cli.doctor import DoctorOptions, DoctorProbes, run_doctor

    healthy = _healthy_probes()
    probes = DoctorProbes(
        module_available=lambda name: name != "pycolmap",
        executable_path=healthy.executable_path,
        resources_check=healthy.resources_check,
        storage_check=healthy.storage_check,
        pure_rotation_check=healthy.pure_rotation_check,
    )

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=probes,
    )

    pycolmap = next(check for check in report.checks if check.name == "pycolmap")
    assert pycolmap.group == "sfm"
    assert pycolmap.status == "error"
    assert pycolmap.required is True
    assert report.complete_capability is False


def test_missing_ffmpeg_and_ffprobe_are_reported_separately(tmp_path: Path) -> None:
    from cadscene.cli.doctor import DoctorOptions, DoctorProbes, run_doctor

    healthy = _healthy_probes()
    probes = DoctorProbes(
        module_available=healthy.module_available,
        executable_path=lambda _name: None,
        resources_check=healthy.resources_check,
        storage_check=healthy.storage_check,
        pure_rotation_check=healthy.pure_rotation_check,
    )

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=probes,
    )

    media = {check.name: check.status for check in report.checks if check.group == "media"}
    assert media == {"ffmpeg": "error", "ffprobe": "error"}


def test_missing_pure_rotation_backend_is_not_described_as_experimental(
    tmp_path: Path,
) -> None:
    from cadscene.cli.doctor import DoctorOptions, DoctorProbes, run_doctor

    healthy = _healthy_probes()
    probes = DoctorProbes(
        module_available=healthy.module_available,
        executable_path=healthy.executable_path,
        resources_check=healthy.resources_check,
        storage_check=healthy.storage_check,
        pure_rotation_check=lambda _options: (
            False,
            "pinned OpenGV backend is unavailable",
        ),
    )

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=probes,
    )

    check = next(item for item in report.checks if item.group == "pure_rotation")
    assert check.status == "error"
    assert check.required is True
    assert "experimental" not in check.message.lower()
    assert "实验" not in check.message


def test_read_only_storage_root_is_a_failure(tmp_path: Path) -> None:
    from cadscene.cli.doctor import DoctorOptions, DoctorProbes, run_doctor

    healthy = _healthy_probes()
    probes = DoctorProbes(
        module_available=healthy.module_available,
        executable_path=healthy.executable_path,
        resources_check=healthy.resources_check,
        storage_check=lambda _root: (False, "storage root is not writable"),
        pure_rotation_check=healthy.pure_rotation_check,
    )

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=probes,
    )

    storage = next(check for check in report.checks if check.group == "storage")
    assert storage.status == "error"
    assert report.complete_capability is False


def test_doctor_json_cli_uses_the_same_report(monkeypatch, capsys, tmp_path: Path) -> None:
    from cadscene.cli import doctor

    monkeypatch.setattr(doctor, "default_probes", _healthy_probes)

    result = doctor.main(
        [
            "--json",
            "--application-root",
            str(tmp_path),
            "--storage-root",
            str(tmp_path),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert result == 0
    assert payload["complete_capability"] is True
    assert payload["checks"]


def test_discoverable_but_unimportable_binary_module_fails_doctor(tmp_path: Path) -> None:
    from cadscene.cli.doctor import DoctorOptions, DoctorProbes, run_doctor

    healthy = _healthy_probes()
    probes = DoctorProbes(
        module_available=healthy.module_available,
        executable_path=healthy.executable_path,
        resources_check=healthy.resources_check,
        storage_check=healthy.storage_check,
        pure_rotation_check=healthy.pure_rotation_check,
        module_importable=lambda name: (
            (False, "DLL load failed") if name == "pycolmap" else (True, f"imported {name}")
        ),
    )

    report = run_doctor(
        DoctorOptions(application_root=tmp_path, storage_root=tmp_path),
        probes=probes,
    )

    pycolmap = next(check for check in report.checks if check.name == "pycolmap")
    assert pycolmap.status == "error"
    assert "DLL load failed" in pycolmap.message
    assert report.complete_capability is False


def test_doctor_help_does_not_validate_broken_default_resources(monkeypatch) -> None:
    from cadscene.cli import doctor

    monkeypatch.setattr(
        doctor,
        "application_root",
        lambda: (_ for _ in ()).throw(FileNotFoundError("resources missing")),
    )

    try:
        doctor.main(["--help"])
    except SystemExit as exc:
        assert exc.code == 0
