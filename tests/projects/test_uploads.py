from __future__ import annotations

import hashlib
import json
from pathlib import Path
from io import BytesIO
import zipfile
from types import SimpleNamespace

import pytest

from cadscene.projects.uploads import (
    UploadValidationError,
    ValidatedUploadStore,
    _validate_video,
)


def _accept(path: Path, asset_type: str) -> dict[str, object]:
    assert path.is_file()
    return {"asset_type": asset_type, "decoded": True}


def test_interrupted_upload_never_becomes_analyzable(tmp_path: Path) -> None:
    triggered: list[tuple[str, str]] = []
    store = ValidatedUploadStore(
        tmp_path / "projects",
        validators={"video": _accept},
        on_published=lambda project_id, asset_type, _upload: triggered.append(
            (project_id, asset_type)
        ),
    )
    pending = store.begin("p1", "video", "source.mp4", expected_size=100)

    pending.write(b"short")
    pending.abort()

    assert not store.published_path("p1", "video").exists()
    assert triggered == []
    assert list((tmp_path / "projects" / "p1" / "assets").glob("*.upload-*")) == []


def test_truncated_or_wrong_fingerprint_upload_is_not_published(tmp_path: Path) -> None:
    store = ValidatedUploadStore(
        tmp_path / "projects", validators={"video": _accept}
    )
    payload = b"complete-video"
    pending = store.begin(
        "p1",
        "video",
        "source.mp4",
        expected_size=len(payload) + 1,
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    pending.write(payload)

    with pytest.raises(UploadValidationError, match="size"):
        pending.complete()

    assert not store.published_path("p1", "video").exists()


def test_validation_failure_preserves_report_but_never_publishes(
    tmp_path: Path,
) -> None:
    def reject(_path: Path, _asset_type: str) -> dict[str, object]:
        raise ValueError("video cannot be decoded completely")

    store = ValidatedUploadStore(
        tmp_path / "projects", validators={"video": reject}
    )
    pending = store.begin("p1", "video", "broken.mp4", expected_size=6)
    pending.write(b"broken")

    with pytest.raises(UploadValidationError, match="decoded completely"):
        pending.complete()

    report = json.loads(store.validation_report_path("p1", "video").read_text())
    assert report["status"] == "failed"
    assert report["sha256"] == hashlib.sha256(b"broken").hexdigest()
    assert not store.published_path("p1", "video").exists()


def test_valid_upload_publishes_atomically_before_triggering_analysis(
    tmp_path: Path,
) -> None:
    observations: list[tuple[bool, bool]] = []
    store: ValidatedUploadStore

    def published(project_id: str, asset_type: str, upload) -> None:
        observations.append(
            (
                store.published_path(project_id, asset_type).is_file(),
                store.validation_report_path(project_id, asset_type).is_file(),
            )
        )

    store = ValidatedUploadStore(
        tmp_path / "projects",
        validators={"video": _accept},
        on_published=published,
    )
    payload = b"valid-video"
    pending = store.begin(
        "p1",
        "video",
        "source.mp4",
        expected_size=len(payload),
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    pending.write(payload[:4])
    pending.write(payload[4:])

    result = pending.complete()

    assert result.path == store.published_path("p1", "video")
    assert result.sha256 == hashlib.sha256(payload).hexdigest()
    assert result.path.name == f"video-{result.sha256}.mp4"
    assert result.validation["decoded"] is True
    assert observations == [(True, True)]
    assert result.path.read_bytes() == payload
    assert result.validation_report_path.parent.name == "validation_attempts"
    immutable_report = json.loads(result.validation_report_path.read_text())
    assert immutable_report["status"] == "published"
    assert immutable_report["sha256"] == result.sha256


def test_content_addressed_upload_never_overwrites_conflicting_bytes(
    tmp_path: Path,
) -> None:
    store = ValidatedUploadStore(
        tmp_path / "projects", validators={"video": _accept}
    )
    payload = b"valid-video"
    fingerprint = hashlib.sha256(payload).hexdigest()
    pending = store.begin(
        "p1", "video", "source.mp4", expected_size=len(payload)
    )
    pending.write(payload)
    conflict = (
        tmp_path
        / "projects"
        / "p1"
        / "assets"
        / f"video-{fingerprint}.mp4"
    )
    conflict.write_bytes(b"wrong-bytes")

    with pytest.raises(UploadValidationError, match="different bytes"):
        pending.complete_staged()

    assert conflict.read_bytes() == b"wrong-bytes"


def test_validator_cannot_replace_upload_bytes_after_stream_digest(
    tmp_path: Path,
) -> None:
    original = b"valid-video"
    replacement = b"other-video"
    assert len(original) == len(replacement)

    def mutate_after_validation(path: Path, _asset_type: str):
        path.write_bytes(replacement)
        return {"decoded": True}

    store = ValidatedUploadStore(
        tmp_path / "projects", validators={"video": mutate_after_validation}
    )
    pending = store.begin(
        "p1", "video", "source.mp4", expected_size=len(original)
    )
    pending.write(original)

    with pytest.raises(UploadValidationError, match="changed during validation"):
        pending.complete_staged()

    assert not any(
        path.name.startswith("video-") and path.suffix == ".mp4"
        for path in (tmp_path / "projects" / "p1" / "assets").iterdir()
    )


def test_invalid_dxf_is_rejected_before_publication(tmp_path: Path) -> None:
    store = ValidatedUploadStore(tmp_path / "projects")
    pending = store.begin("p1", "cad", "broken.dxf", expected_size=7)
    pending.write(b"garbage")

    with pytest.raises(UploadValidationError):
        pending.complete()

    assert not store.published_path("p1", "cad").exists()


def test_failed_replacement_keeps_previous_valid_publication(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    first_store = ValidatedUploadStore(root, validators={"video": _accept})
    first = first_store.begin("p1", "video", "first.mp4", expected_size=5)
    first.write(b"first")
    published = first.complete()

    def reject(_path: Path, _asset_type: str) -> dict[str, object]:
        raise ValueError("replacement is corrupt")

    second_store = ValidatedUploadStore(root, validators={"video": reject})
    second = second_store.begin("p1", "video", "second.mp4", expected_size=6)
    second.write(b"second")
    with pytest.raises(UploadValidationError):
        second.complete()

    assert second_store.published_path("p1", "video") == published.path
    assert published.path.read_bytes() == b"first"
    attempts = [
        json.loads(path.read_text())
        for path in (root / "p1" / "assets" / "validation_attempts").glob(
            "video-*.json"
        )
    ]
    assert [attempt["status"] for attempt in attempts].count("failed") == 1


def test_cad_zip_rejects_drive_paths_and_symlinks_before_publication(
    tmp_path: Path,
) -> None:
    for member, external_attr in (("C:/escape.json", 0), ("link", 0o120777 << 16)):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo(member)
            info.external_attr = external_attr
            archive.writestr(info, "{}")
        payload = buffer.getvalue()
        store = ValidatedUploadStore(tmp_path / member.replace("/", "_") / "projects")
        pending = store.begin("p1", "cad", "assets.zip", expected_size=len(payload))
        pending.write(payload)

        with pytest.raises(UploadValidationError):
            pending.complete()

        assert not store.published_path("p1", "cad").exists()


@pytest.mark.parametrize(
    "project_id",
    [
        ".", "..", "../escape", "C:escape", "project.", "CON", "con.json",
        "PrN", "AUX.txt", "NUL", "COM1", "com9.log", "LPT1", "lpt9.data",
    ],
)
def test_upload_store_rejects_project_ids_that_can_escape_root(
    tmp_path: Path, project_id: str
) -> None:
    store = ValidatedUploadStore(tmp_path / "projects", validators={"video": _accept})

    with pytest.raises(ValueError, match="invalid project_id"):
        store.begin(project_id, "video", "source.mp4", expected_size=1)

    assert not (tmp_path / "escape").exists()


def test_video_validation_rejects_decode_error_even_when_ffmpeg_returns_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "broken.mp4"
    video.write_bytes(b"broken")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append([str(item) for item in command])
        return SimpleNamespace(returncode=0, stderr="Error while decoding stream #0:0")

    monkeypatch.setattr("cadscene.projects.uploads.subprocess.run", fake_run)
    monkeypatch.setattr(
        "cadscene.video_analysis.pts.resolve_ffmpeg_executable",
        lambda: Path("ffmpeg"),
    )

    with pytest.raises(ValueError, match="decode failed"):
        _validate_video(video, "video")

    assert "-xerror" in observed[0]


def test_video_upload_validation_is_bounded_and_defers_full_pts_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = tmp_path / "long.mp4"
    video.write_bytes(b"video")
    observed: list[list[str]] = []

    def fake_run(command, **_kwargs):
        observed.append([str(item) for item in command])
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr("cadscene.projects.uploads.subprocess.run", fake_run)
    monkeypatch.setattr(
        "cadscene.video_analysis.pts.resolve_ffmpeg_executable",
        lambda: Path("ffmpeg"),
    )
    monkeypatch.setattr(
        "cadscene.video_analysis.pts.probe_decoded_frame_index",
        lambda *_args, **_kwargs: pytest.fail(
            "upload validation must not build the authoritative full frame index"
        ),
    )

    result = _validate_video(video, "video")

    assert result == {"decodable": True, "validation_scope": "bounded_probe"}
    assert len(observed) == 1
    assert observed[0][observed[0].index("-frames:v") + 1] == "1"


def test_successful_validation_does_not_rehash_unchanged_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ValidatedUploadStore(
        tmp_path / "projects", validators={"video": _accept}
    )
    pending = store.begin("p1", "video", "source.mp4", expected_size=5)
    pending.write(b"video")
    rehashed: list[Path] = []

    def track_rehash(path: Path) -> str:
        rehashed.append(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr("cadscene.projects.uploads._file_fingerprint", track_rehash)

    result = pending.complete_staged()

    assert result.path.read_bytes() == b"video"
    assert rehashed == []


def test_failed_immutable_publish_report_keeps_previous_canonical_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "projects"
    store = ValidatedUploadStore(root, validators={"video": _accept})
    first = store.begin("p1", "video", "first.mp4", expected_size=5)
    first.write(b"first")
    previous = first.complete()
    original = store._write_attempt_report

    def fail_published_report(project_id, asset_type, payload):
        if payload.get("status") == "published":
            raise OSError("injected immutable report failure")
        return original(project_id, asset_type, payload)

    monkeypatch.setattr(store, "_write_attempt_report", fail_published_report)
    replacement = store.begin("p1", "video", "second.mp4", expected_size=6)
    replacement.write(b"second")

    with pytest.raises(UploadValidationError, match="immutable report failure"):
        replacement.complete()

    assert store.published_path("p1", "video") == previous.path
    assert previous.path.read_bytes() == b"first"
