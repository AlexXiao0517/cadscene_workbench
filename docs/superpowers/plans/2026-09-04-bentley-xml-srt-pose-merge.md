# Bentley XML to Full-Pose SRT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Generate a new full-pose SRT by adding only Bentley XML camera orientation to every original SRT block while preserving all original telemetry and using 59.109° as the audited test FOV.

**Architecture:** Add a focused `cadscene.srt.bentley_pose_merge` module that parses BlocksExchange camera samples, maps XML frame numbers to zero-based SRT block indices, converts/interpolates rotations with quaternion SLERP, and appends `camera_*` tags without rewriting existing text. Add a small CLI that publishes the SRT and JSON report atomically and fails closed on frame/GPS inconsistencies.

**Tech Stack:** Python 3, stdlib `xml.etree.ElementTree`, SciPy `Rotation`/`Slerp`, existing DJI SRT parser and capability detector, pytest.

## Global Constraints

- Original SRT and XML files are read-only; never overwrite either input.
- Merge XML orientation only; never copy XML optimized `Pose/Center` into SRT.
- Preserve every original SRT block, sequence number, timecode, position, height, exposure field, line ending content, and block order.
- Map XML `frame_NNNNNN` to zero-based SRT block index `N` and reject GPS metadata disagreement above `1e-7` degree or `1e-3` metre altitude.
- Use quaternion shortest-arc SLERP between XML samples; hold the last sample for the final 16 SRT blocks.
- Append `[camera_yaw]`, `[camera_pitch]`, and `[camera_roll]` with six decimal places.
- Record `horizontal_fov_deg=59.109` in the report; do not repeat FOV in every SRT block.
- The output must parse with 100% camera-attitude coverage and route to `srt_full_pose`.
- Use TDD: every production change follows a witnessed failing test.

---

## File Structure

- Create `cadscene/srt/bentley_pose_merge.py`: XML parsing, SRT block splitting, pose interpolation, merge validation, report model.
- Create `cadscene/cli/merge_bentley_pose_srt.py`: command-line parsing and atomic output/report publication.
- Modify `cadscene/srt/__init__.py`: export only the public merge types/functions needed by callers.
- Create `tests/srt/test_bentley_pose_merge.py`: core parsing, preservation, interpolation and validation tests.
- Create `tests/cli/test_merge_bentley_pose_srt_cli.py`: CLI atomicity and complete-capability tests.

### Task 1: Parse Bentley camera samples and validate frame/GPS identity

**Files:**
- Create: `tests/srt/test_bentley_pose_merge.py`
- Create: `cadscene/srt/bentley_pose_merge.py`

**Interfaces:**
- Produces: `BentleyPoseSample(frame_index: int, yaw_deg: float, pitch_deg: float, roll_deg: float, metadata_lon_lat_alt: tuple[float, float, float] | None)`.
- Produces: `load_bentley_pose_samples(path: str | Path) -> tuple[BentleyPoseSample, ...]`.
- Consumes later: sorted samples with unique non-negative frame indices.

- [ ] **Step 1: Write the failing XML parser tests**

```python
def test_load_bentley_pose_samples_uses_image_frame_and_rotation(tmp_path: Path) -> None:
    xml = _write_bentley_xml(tmp_path / "block.xml", frames=(0, 120))
    samples = load_bentley_pose_samples(xml)
    assert [item.frame_index for item in samples] == [0, 120]
    assert samples[0].yaw_deg == pytest.approx(-100.0)
    assert samples[0].metadata_lon_lat_alt == pytest.approx((118.8, 30.1, 230.0))


def test_load_bentley_pose_samples_rejects_duplicate_or_unparseable_frames(tmp_path: Path) -> None:
    xml = _write_bentley_xml(tmp_path / "duplicate.xml", frames=(120, 120))
    with pytest.raises(ValueError, match="unique.*frame"):
        load_bentley_pose_samples(xml)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest tests/srt/test_bentley_pose_merge.py -q`

Expected: collection fails because `cadscene.srt.bentley_pose_merge` does not exist.

- [ ] **Step 3: Implement the minimal XML parser**

```python
@dataclass(frozen=True)
class BentleyPoseSample:
    frame_index: int
    yaw_deg: float
    pitch_deg: float
    roll_deg: float
    metadata_lon_lat_alt: tuple[float, float, float] | None


def load_bentley_pose_samples(path: str | Path) -> tuple[BentleyPoseSample, ...]:
    root = ElementTree.parse(Path(path)).getroot()
    samples = tuple(sorted((_photo_sample(photo) for photo in root.findall(".//Photo")), key=lambda item: item.frame_index))
    if not samples or any(a.frame_index == b.frame_index for a, b in zip(samples, samples[1:])):
        raise ValueError("Bentley pose samples require unique frame indices")
    return samples
```

The private `_photo_sample` must parse the trailing integer from `ImagePath`, require finite Yaw/Pitch/Roll values, and read `Pose/Metadata/Center/x,y,z` only for identity validation.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m pytest tests/srt/test_bentley_pose_merge.py -q`

Expected: parser tests pass.

- [ ] **Step 5: Commit the parser slice**

```bash
git add cadscene/srt/bentley_pose_merge.py tests/srt/test_bentley_pose_merge.py
git commit -m "feat: parse Bentley camera pose samples"
```

### Task 2: Interpolate camera rotations and preserve SRT blocks

**Files:**
- Modify: `tests/srt/test_bentley_pose_merge.py`
- Modify: `cadscene/srt/bentley_pose_merge.py`
- Modify: `cadscene/srt/__init__.py`

**Interfaces:**
- Produces: `BentleySrtMergeResult(text: str, report: dict[str, object])`.
- Produces: `merge_bentley_orientations_into_srt(srt_text: str, samples: Sequence[BentleyPoseSample], *, horizontal_fov_deg: float = 59.109) -> BentleySrtMergeResult`.
- Uses: existing `load_srt_records` semantics through a temporary-free in-memory block validator.

- [ ] **Step 1: Write failing preservation and interpolation tests**

```python
def test_merge_preserves_original_text_and_adds_slerped_camera_pose() -> None:
    source = _srt_with_three_blocks()
    samples = (
        BentleyPoseSample(0, 170.0, -45.0, 0.0, (118.8, 30.1, 230.0)),
        BentleyPoseSample(2, -170.0, -45.0, 0.0, (118.8002, 30.1, 230.2)),
    )
    result = merge_bentley_orientations_into_srt(source, samples)
    assert _remove_camera_tags(result.text) == source
    parsed = _parse_text(result.text)
    assert len(parsed) == 3
    assert abs(abs(parsed[1].gimbal_yaw) - 180.0) < 1e-6
    assert result.report["horizontal_fov_deg"] == 59.109


def test_merge_holds_last_pose_after_final_xml_sample() -> None:
    result = merge_bentley_orientations_into_srt(_srt_with_four_blocks(), _two_samples_ending_at_two())
    records = _parse_text(result.text)
    assert records[3].gimbal_yaw == pytest.approx(records[2].gimbal_yaw)


def test_merge_rejects_xml_metadata_that_disagrees_with_srt_position() -> None:
    samples = (BentleyPoseSample(0, 0.0, -45.0, 0.0, (119.8, 30.1, 230.0)),)
    with pytest.raises(ValueError, match="metadata.*SRT"):
        merge_bentley_orientations_into_srt(_srt_with_three_blocks(), samples)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python -m pytest tests/srt/test_bentley_pose_merge.py -q`

Expected: failures because the merge API is missing.

- [ ] **Step 3: Implement block-preserving quaternion interpolation**

```python
def merge_bentley_orientations_into_srt(
    srt_text: str,
    samples: Sequence[BentleyPoseSample],
    *,
    horizontal_fov_deg: float = 59.109,
) -> BentleySrtMergeResult:
    blocks, separators = _split_blocks_losslessly(srt_text)
    _validate_sample_indices_and_metadata(blocks, samples)
    key_times = [sample.frame_index for sample in samples]
    rotations = Rotation.from_euler(
        "ZYX",
        [[sample.yaw_deg, sample.pitch_deg, sample.roll_deg] for sample in samples],
        degrees=True,
    )
    interpolator = Slerp(key_times, rotations) if len(samples) > 1 else None
    orientations = [_rotation_at(index, samples, rotations, interpolator) for index in range(len(blocks))]
    merged = _append_camera_tags(blocks, separators, orientations)
    return BentleySrtMergeResult(merged, _merge_report(blocks, samples, orientations, horizontal_fov_deg))
```

Implement `_rotation_at` with bounded interpolation and final hold. `_append_camera_tags` must append before the block's existing trailing newline so deleting the exact appended tag string reconstructs the input byte-for-byte after decoding. Reject a source SRT that already contains camera/gimbal pose tags.

- [ ] **Step 4: Run focused and parser/capability tests**

Run: `python -m pytest tests/srt/test_bentley_pose_merge.py tests/srt/test_parser.py tests/srt/test_capability.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the merge core**

```bash
git add cadscene/srt/bentley_pose_merge.py cadscene/srt/__init__.py tests/srt/test_bentley_pose_merge.py
git commit -m "feat: merge Bentley orientation into DJI SRT"
```

### Task 3: Add atomic CLI publication and report

**Files:**
- Create: `tests/cli/test_merge_bentley_pose_srt_cli.py`
- Create: `cadscene/cli/merge_bentley_pose_srt.py`

**Interfaces:**
- Produces CLI: `python -m cadscene.cli.merge_bentley_pose_srt --srt PATH --xml PATH --output PATH --report PATH --horizontal-fov-deg 59.109`.
- Consumes: `load_bentley_pose_samples` and `merge_bentley_orientations_into_srt`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_cli_writes_full_pose_srt_and_audited_report(tmp_path: Path) -> None:
    source = _write_srt(tmp_path / "flight.srt")
    xml = _write_xml(tmp_path / "block.xml")
    output = tmp_path / "flight-full.srt"
    report = tmp_path / "flight-full.merge-report.json"
    assert main(["--srt", str(source), "--xml", str(xml), "--output", str(output), "--report", str(report), "--horizontal-fov-deg", "59.109"]) == 0
    analysis = analyze_srt_stream(output.open("rb"), output.name)
    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["full_pose_coverage"] == pytest.approx(1.0)
    assert json.loads(report.read_text(encoding="utf-8"))["horizontal_fov_deg"] == 59.109


def test_cli_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    source = _write_srt(tmp_path / "flight.srt")
    xml = _write_xml(tmp_path / "block.xml")
    output = tmp_path / "existing.srt"
    report = tmp_path / "existing.merge-report.json"
    output.write_text("keep", encoding="utf-8")
    assert main([
        "--srt", str(source), "--xml", str(xml), "--output", str(output),
        "--report", str(report), "--horizontal-fov-deg", "59.109",
    ]) == 1
    assert output.read_text(encoding="utf-8") == "keep"
    assert not report.exists()
```

- [ ] **Step 2: Run CLI tests and verify RED**

Run: `python -m pytest tests/cli/test_merge_bentley_pose_srt_cli.py -q`

Expected: collection fails because the CLI module does not exist.

- [ ] **Step 3: Implement atomic CLI output**

```python
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.output.exists() or args.report.exists():
            raise FileExistsError("refusing to overwrite merge output")
        with args.srt.open("r", encoding="utf-8-sig", newline="") as stream:
            source = stream.read()
        samples = load_bentley_pose_samples(args.xml)
        result = merge_bentley_orientations_into_srt(source, samples, horizontal_fov_deg=args.horizontal_fov_deg)
        _publish_pair_atomically(args.output, result.text, args.report, result.report)
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
```

`_publish_pair_atomically` writes sibling temporary files with exclusive creation, fsyncs, verifies neither destination exists, and replaces both destinations. On any failure it removes only its own temporary files and any destination it published in the current invocation.

- [ ] **Step 4: Run CLI and focused regression tests**

Run: `python -m pytest tests/cli/test_merge_bentley_pose_srt_cli.py tests/srt/test_bentley_pose_merge.py tests/srt/test_parser.py tests/srt/test_capability.py -q`

Expected: all pass.

- [ ] **Step 5: Commit the CLI**

```bash
git add cadscene/cli/merge_bentley_pose_srt.py tests/cli/test_merge_bentley_pose_srt_cli.py
git commit -m "feat: publish audited full-pose SRT merge"
```

### Task 4: Generate and verify the real merged file

**Files:**
- Input only: `D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/DJI_20260826113512_0013_D.SRT`
- Input only: `D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/Block_1 - AT - export.xml`
- Create: `D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.SRT`
- Create: `D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.merge-report.json`

**Interfaces:**
- Consumes the CLI from Task 3.
- Produces user-test artifact and audit report; no repository source changes.

- [ ] **Step 1: Run the real merge**

Run:

```powershell
python -m cadscene.cli.merge_bentley_pose_srt --srt "D:\zjic2026\cadscene_workbench\dji\yjq\K181+932-K184+575\DJI_20260826113512_0013_D.SRT" --xml "D:\zjic2026\cadscene_workbench\dji\yjq\K181+932-K184+575\Block_1 - AT - export.xml" --output "D:\zjic2026\cadscene_workbench\dji\yjq\K181+932-K184+575\DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.SRT" --report "D:\zjic2026\cadscene_workbench\dji\yjq\K181+932-K184+575\DJI_20260826113512_0013_D_FULL_POSE_FROM_BLOCK_AT.merge-report.json" --horizontal-fov-deg 59.109
```

Expected: exit 0 and both new files exist; inputs are unchanged.

- [ ] **Step 2: Verify report and capability**

Run: `python -m pytest tests/srt/test_bentley_pose_merge.py tests/cli/test_merge_bentley_pose_srt_cli.py -q`

Then run a read-only Python command that loads the generated SRT with `analyze_srt_stream` and prints block count, full-pose coverage, detected mode, source/output hashes, XML sample count and held-tail count.

Expected: `12017`, `1.0`, `srt_full_pose`, `101`, and `16`; source hash matches the pre-merge report.

- [ ] **Step 3: Inspect first, middle, XML-anchor, and tail records**

Read blocks 0, 1, 60, 120, 12000, and 12016. Confirm original position/height tokens remain and only three `camera_*` tags were appended. Confirm frame 12016 matches frame 12000 orientation.

- [ ] **Step 4: Report the clickable artifact paths to the user**

Provide the SRT and audit report as absolute clickable file links, plus the single test instruction: enter `59.109°` in the full-pose FOV field.
