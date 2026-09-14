# DJI Grouped SRT Attitude Parser Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse every supported telemetry key/value inside a DJI bracket group so full-pose SRT uploads skip COLMAP sparse reconstruction.

**Architecture:** Keep `SrtRecord` and capability detection unchanged. Narrow the change to `cadscene.srt.parser._parse_values`: scan supported numeric fields throughout the text, including multiple fields inside one bracket, while retaining the existing bracket parser for short aliases not covered by the text-field pattern.

**Tech Stack:** Python 3, regular expressions, pytest.

## Global Constraints

- Preserve existing separate-bracket and unbracketed SRT formats.
- Preserve the existing field alias map and full-pose coverage threshold.
- Do not parse unsupported exposure/camera metadata.
- Do not change project workflow routing outside the corrected parser output.

---

### Task 1: Parse grouped DJI attitude and verify full-pose routing

**Files:**
- Modify: `tests/srt/test_parser.py`
- Modify: `tests/srt/test_capability.py`
- Modify: `tests/fixtures/srt/dji_short_aliases.srt`
- Modify: `cadscene/srt/parser.py`

**Interfaces:**
- Consumes: `parser._parse_record_block(block: str) -> SrtRecord | None` and `analyze_srt_stream(...) -> dict[str, Any]`.
- Produces: unchanged public interfaces whose records include all supported fields from grouped brackets.

- [x] **Step 1: Write the failing parser test**

```python
def test_parser_retains_multiple_gimbal_fields_from_one_bracket() -> None:
    record = parser._parse_record_block(
        "1\n00:00:00,000 --> 00:00:00,100\n"
        "[latitude: 30.0] [longitude: 120.0] [rel_alt: 12.5 abs_alt: 86.0] "
        "[gb_yaw: 90.0 gb_pitch: -45.0 gb_roll: 2.0]\n"
    )

    assert record is not None
    assert record.rel_alt == 12.5
    assert record.abs_alt == 86.0
    assert record.gimbal_yaw == 90.0
    assert record.gimbal_pitch == -45.0
    assert record.gimbal_roll == 2.0
```

- [x] **Step 2: Make the capability fixture use the real DJI grouped format**

Use this fixture content and keep `test_dji_rel_alt_and_gb_attitude_aliases_produce_full_pose` unchanged so it verifies end-to-end routing:

```srt
1
00:00:00,000 --> 00:00:01,000
[latitude:30.0001] [longitude:120.0001] [rel_alt:54.2] [gb_yaw:12.0 gb_pitch:-45.0 gb_roll:0.5]

2
00:00:01,000 --> 00:00:02,000
[latitude:30.0002] [longitude:120.0002] [abs_alt:54.4] [gb_yaw:13.0 gb_pitch:-44.8 gb_roll:0.4]
```

- [x] **Step 3: Run tests to verify RED**

Run: `D:\anaconda3\python.exe -m pytest tests/srt/test_parser.py::test_parser_retains_multiple_gimbal_fields_from_one_bracket tests/srt/test_capability.py::test_dji_rel_alt_and_gb_attitude_aliases_produce_full_pose -q`

Expected: FAIL because `abs_alt`, `gimbal_pitch`, and `gimbal_roll` are missing and the fixture is classified as `srt_fixed_track_visual_pose`.

- [x] **Step 4: Implement the minimal parser fix**

Add the established short alias alternative to `_TEXT_VALUE`; `_parse_values` already scans both bracket and text matches, so the text matches supply every grouped field while preserving bracket-only aliases:

```python
r"gimbal[_ ]?(?:yaw|pitch|roll)|gb[_ ]?(?:yaw|pitch|roll)|drone[_ ]?(?:yaw|pitch|roll)|"
```

- [x] **Step 5: Verify focused and SRT tests GREEN**

Run: `D:\anaconda3\python.exe -m pytest tests/srt/test_parser.py tests/srt/test_capability.py -q`

Expected: all parser and capability tests pass.

Run: `D:\anaconda3\python.exe -m pytest tests/srt -q`

Expected: all SRT tests pass.

- [x] **Step 6: Verify the real uploaded SRT**

Load the project asset with `analyze_srt_stream` and assert `gimbal_yaw`, `gimbal_pitch`, and `gimbal_roll` coverage are all `1.0`, and `detected_mode` is `srt_full_pose`.

- [x] **Step 7: Commit the fix**

```bash
git add cadscene/srt/parser.py tests/srt/test_parser.py tests/srt/test_capability.py tests/fixtures/srt/dji_short_aliases.srt docs/superpowers/plans/2026-09-14-dji-grouped-srt-attitude-parser.md
git commit -m "fix: parse grouped DJI SRT attitude"
```
