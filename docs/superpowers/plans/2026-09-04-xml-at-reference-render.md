# XML AT Reference Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and run a diagnostic backend renderer that consumes Bentley BlocksExchange adjusted poses and calibrated camera parameters to produce the approved 1080p reference-interval video.

**Architecture:** Extend the existing Bentley XML reader with adjusted centers and a typed camera model, then add an isolated diagnostics module for ECEF/Slerp track interpolation and Brown–Conrady projection. A thin CLI loads the confirmed CAD georeference, renders the approved source-frame interval through FFmpeg, and writes auditable metadata without changing project workflow state.

**Tech Stack:** Python 3.10+, ElementTree, NumPy, SciPy Rotation/Slerp, pyproj, OpenCV, FFmpeg, pytest.

## Global Constraints

- This is a diagnostic baseline and must not change project manifests, workbench sessions, or SRT workflow routing.
- Use XML `Pose/Center` and `Pose/Rotation`; do not replace adjusted centers with SRT GPS.
- Render source frames 300 through 11439 inclusive at 1920×1080.
- Use XML focal length, sensor size, principal point, K1/K2/K3/P1/P2, aspect ratio, and skew.
- Preserve the real reference-interval duration and report the diagnostic sampling rate honestly.
- Produce browser-compatible H.264/yuv420p and verify the complete output by decoding it.

---

### Task 1: Parse adjusted Bentley poses and calibrated camera model

**Files:**
- Modify: `cadscene/srt/bentley_pose_merge.py`
- Test: `tests/srt/test_bentley_pose_merge.py`

**Interfaces:**
- Consumes: Bentley BlocksExchange 3.2 XML with one Perspective/XRightYDown photogroup.
- Produces: `BentleyPoseSample.adjusted_lon_lat_alt` and `load_bentley_camera_model(path) -> BentleyCameraModel`.

- [ ] **Step 1: Write failing parser tests**

Add XML fixture calibration fields and assert:

```python
model = load_bentley_camera_model(xml)
assert model.image_size == (1920, 1080)
assert model.horizontal_fov_deg == pytest.approx(60.12739034017952)
assert model.principal_point_px == pytest.approx((954.760480832254, 519.684612262061))
assert model.distortion == pytest.approx((-0.0124025934908946, 0.0171692916163385, 0.0449273882850133, 0.0, 0.0))
assert load_bentley_pose_samples(xml)[0].adjusted_lon_lat_alt == pytest.approx((119.071793042404, 28.896799226944, 228.399947432801))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest tests/srt/test_bentley_pose_merge.py -q`

Expected: import or attribute failure for `BentleyCameraModel`, `load_bentley_camera_model`, and `adjusted_lon_lat_alt`.

- [ ] **Step 3: Implement strict typed parsing**

Add:

```python
@dataclass(frozen=True)
class BentleyCameraModel:
    image_size: tuple[int, int]
    focal_length_mm: float
    sensor_size_mm: float
    principal_point_px: tuple[float, float]
    distortion: tuple[float, float, float, float, float]
    aspect_ratio: float
    skew: float

    @property
    def focal_pixels(self) -> tuple[float, float]:
        fx = self.focal_length_mm / self.sensor_size_mm * self.image_size[0]
        return fx, fx * self.aspect_ratio

    @property
    def horizontal_fov_deg(self) -> float:
        return float(np.degrees(2.0 * np.arctan(self.sensor_size_mm / (2.0 * self.focal_length_mm))))
```

Extend `BentleyPoseSample` with an optional adjusted center default so current callers remain compatible. Reject missing/non-finite calibration, non-Perspective models, non-XRightYDown orientation, non-positive dimensions/focal/sensor/aspect ratio, and non-zero skew unsupported by this renderer.

- [ ] **Step 4: Run parser tests and verify GREEN**

Run: `pytest tests/srt/test_bentley_pose_merge.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the parser increment**

```bash
git add cadscene/srt/bentley_pose_merge.py tests/srt/test_bentley_pose_merge.py
git commit -m "feat: parse Bentley adjusted camera calibration"
```

### Task 2: Build the adjusted AT track and distorted projection

**Files:**
- Create: `cadscene/diagnostics/xml_at_reference.py`
- Create: `tests/diagnostics/test_xml_at_reference.py`

**Interfaces:**
- Consumes: `BentleyPoseSample`, `BentleyCameraModel`, confirmed `CadGeoreference`, CAD origin/scale, SRT altitude reference, requested source-frame indexes.
- Produces: `build_adjusted_at_track(...) -> tuple[AtFramePose, ...]` and `project_distorted_segments(...) -> DistortedProjection`.

- [ ] **Step 1: Write failing track/projection tests**

Cover adjusted-center selection, midpoint ECEF interpolation, shortest-arc rotation interpolation, XML pitch conversion to positive-down `CameraState`, CAD-local projection, altitude baseline subtraction, principal point, and a radial distortion example:

```python
projection = project_distorted_segments(
    starts_camera=np.asarray([[0.1, 0.0, 1.0]]),
    ends_camera=np.asarray([[0.2, 0.0, 1.0]]),
    model=model,
)
assert projection.uv_starts[0, 0] == pytest.approx(cx + fx * 0.1 * (1 + k1 * 0.01 + k2 * 0.0001 + k3 * 0.000001))
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest tests/diagnostics/test_xml_at_reference.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement interpolation and Brown–Conrady projection**

Use `Transformer.from_crs(4326, 4978, always_xy=True)` for anchor ECEF centers, linear interpolation between XML frame anchors, and the existing confirmed `project_wgs84_to_cad_raw` plus `cad_raw_to_local_m` for CAD-local XY. Use `Rotation.from_euler("ZYX", ...)` and `Slerp`; output `CameraState(yaw_deg=yaw, pitch_deg=-xml_pitch, roll_deg=roll)`.

Apply distortion in normalized coordinates:

```python
r2 = x * x + y * y
radial = 1.0 + k1 * r2 + k2 * r2**2 + k3 * r2**3
xd = x * radial + 2.0 * p1 * x * y + p2 * (r2 + 2.0 * x * x)
yd = y * radial + p1 * (r2 + 2.0 * y * y) + 2.0 * p2 * x * y
u = cx + fx * xd + skew * yd
v = cy + fy * yd
```

- [ ] **Step 4: Run diagnostics and adjacent regression tests**

Run: `pytest tests/diagnostics/test_xml_at_reference.py tests/srt/test_bentley_pose_merge.py tests/rendering/test_overlay.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the math increment**

```bash
git add cadscene/diagnostics/xml_at_reference.py tests/diagnostics/test_xml_at_reference.py
git commit -m "feat: build adjusted AT camera projection"
```

### Task 3: Add the isolated reference renderer and produce the video

**Files:**
- Create: `cadscene/cli/render_xml_at_reference.py`
- Create: `tests/cli/test_render_xml_at_reference_cli.py`
- Generated: `work/srt-full-pose-ui-test/manual-renders/xml-at-reference/XML空三外参_精确内参畸变_参考区间_1080p.mp4`
- Generated: `work/srt-full-pose-ui-test/manual-renders/xml-at-reference/render_report.json`

**Interfaces:**
- Consumes: XML, original SRT, prepared video, confirmed CAD directory/georeference, source-frame interval, sample stride.
- Produces: H.264 reference video plus JSON report containing source hashes, frame interval, output dimensions/fps/count, XML camera model, and altitude baseline.

- [ ] **Step 1: Write failing CLI contract tests**

Assert parser arguments, invalid interval failure, missing FFmpeg failure, report schema, and that a synthetic three-frame render passes adjusted poses and calibrated projection rather than `CameraState.fov_deg` pinhole projection.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `pytest tests/cli/test_render_xml_at_reference_cli.py -q`

Expected: module import failure.

- [ ] **Step 3: Implement the CLI**

The CLI must batch static CAD segments, spatially filter them around each adjusted camera center, render sampled source frames at 1920×1080, and stream BGR frames into FFmpeg `libopenh264`. Defaults for this approved diagnostic run are explicit command-line values, not hidden project behavior.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `pytest tests/cli/test_render_xml_at_reference_cli.py tests/diagnostics/test_xml_at_reference.py tests/srt/test_bentley_pose_merge.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit the renderer increment**

```bash
git add cadscene/cli/render_xml_at_reference.py tests/cli/test_render_xml_at_reference_cli.py
git commit -m "feat: render XML AT calibration baseline"
```

- [ ] **Step 6: Render the approved K181 interval**

Run:

```bash
python -m cadscene.cli.render_xml_at_reference \
  --xml "C:/Users/Hanshark/Desktop/1080p Block - 0.5秒.xml" \
  --srt "D:/zjic2026/cadscene_workbench/dji/yjq/K181+932-K184+575/DJI_20260826113512_0013_D.SRT" \
  --video "D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/projects/p-4fa71cfb477a488e/jobs/8065571f022b4009ab37f45795c54fff/attempt-1/clip_inputs/clip-0001.mp4" \
  --cad-dir "D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/manual-renders/p-4fa71cfb477a488e-full-pose-preview/cad-full-250m" \
  --central-meridian 118.83333333333333 \
  --cad-origin 484717.5726936237 3189945.713859801 \
  --start-frame 300 --end-frame 11439 --sample-every 5 \
  --output "D:/zjic2026/cadscene_workbench/work/srt-full-pose-ui-test/manual-renders/xml-at-reference/XML空三外参_精确内参畸变_参考区间_1080p.mp4"
```

Expected: report records `position_source=xml_adjusted_center`, `distortion_source=xml_photogroup`, 1920×1080, source frames 300–11439, and an output duration near 185.85 seconds.

- [ ] **Step 7: Verify the complete artifact**

Run full FFmpeg decode, ffprobe metadata inspection, SHA-256 hashing, and extract frames at output times 0, 90, and 180 seconds. Compare them with reference times 0, 90, and 180 and record remaining visual differences in `render_report.json`.

Expected: FFmpeg decode exits 0; codec is H.264, pixel format is yuv420p, dimensions are 1920×1080, and all three scenes correspond.
