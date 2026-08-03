# Stage 8A–8E Video Project Pipeline Design

**Date:** 2026-08-03  
**Integration branch:** `feature/video-project-pipeline`  
**Baseline:** `feature/video-analysis-segmentation@c907b3e`  
**Pure Rotation ancestor:** `825b806`  
**Status:** Approved for implementation

## 1. Goal and scope

Build the first complete single-machine video-project loop:

```text
upload
→ automatic CAD and video analysis
→ project clip management
→ user workflow confirmation
→ resource-aware batch trajectory jobs
→ existing workbench
→ return to project
→ per-clip render
→ simple source-order concat
```

This work extends the existing `serve_viewer` service. It does not add a new
service, database, or file-polling control plane. Existing `sfm_only`,
`srt_sfm_fused`, `srt_full_pose`, and `pure_rotation` algorithm internals remain
unchanged.

Out of scope:

- labels, overlays, and visual tracking;
- automatic hover detection calibration;
- complex resource prediction;
- cross-clip trajectory merging or pose continuity;
- physical export of every clip immediately after upload;
- multi-machine access to one project directory.

## 2. Architecture

`serve_viewer` remains the only HTTP entry point. The handler is limited to
route matching, request validation, path-scope validation, service invocation,
and HTTP error mapping.

```text
serve_viewer HTTP layer
        │
        ▼
ProjectService ─────────────── Project snapshot/capabilities
   │       │       │
   │       │       └────────── Workbench session coordinator
   │       └────────────────── LocalResourceQueue
   └────────────────────────── Repository interfaces
                                      │
                                      ▼
                              Atomic JSON repositories

Adapters called by the queue:
- VideoAnalysisAdapter
- CadAnalysisAdapter
- WorkflowAdapter × 4
- ClipExportAdapter
- RenderAdapter
- ConcatAdapter
```

The project state machine, resource queue, workflow routing, export, render,
and concat logic must not be added directly to `RangeRequestHandler`.

Business services depend on repository and queue interfaces, not JSON file
operations. A future SQLite migration replaces repository/queue implementations
without rewriting project workflows.

## 3. Isolated integration baseline

Development uses:

```text
worktree: D:\zjic2026\cadscene_workbench\.worktrees\video-project-pipeline
branch:   feature/video-project-pipeline
base:     c907b3e
```

`c907b3e` contains both the current Pure Rotation workflow and Stage 8A. Main
and all pre-existing worktrees are left unchanged.

Baseline verification before implementation:

```text
530 passed, 1 skipped
```

## 4. Authoritative decoded-frame timeline

### 4.1 Authority

The only authoritative video timeline is the video stream time base plus the
integer PTS of decoded frames in presentation order.

- Use decoded frame PTS when present.
- Use `best_effort_timestamp` only when decoded frame PTS is absent.
- Packet PTS and DTS are diagnostic data only.
- Never derive frame numbers from `time × fps`.
- Never derive authoritative integer PTS by converting a UI float back to PTS.

The exact time base is represented by numerator and denominator:

```json
{"numerator": 1, "denominator": 90000}
```

Duplicate, missing, non-monotonic, or otherwise ambiguous decoded timestamps
are diagnosed explicitly. If a reliable frame mapping cannot be established,
analysis/export fails rather than silently degrading.

### 4.2 Half-open clip contract

All logical and physical clips use half-open intervals:

```text
[source_start_pts, source_end_pts_exclusive)
```

Adjacent clips satisfy:

```text
clip[i].source_end_pts_exclusive == clip[i + 1].source_start_pts
```

Public technical fields include:

```text
source_start_pts
source_end_pts_exclusive
source_time_base.numerator
source_time_base.denominator
source_start_pts_sec
source_end_pts_exclusive_sec
interval_semantics = "half_open"
```

Integer PTS and exact time base are authoritative. Seconds are derived values.

### 4.3 Shot-boundary ownership

For a hard cut from `P_old` to the first new-scene frame `P_new`:

```text
previous clip = [start, P_new)
next clip     = [P_new, end)
```

Sparse detection times are candidates only. A confirmed boundary is snapped to
the integer PTS of the actual first decoded frame of the new scene.

For a short black transition:

- record transition start/end evidence;
- assign short black frames to the preceding scene;
- start the next scene at the first stable non-black decoded frame;
- do not create a meaningless black-only clip;
- flag a long or ambiguous black interval for review.

### 4.4 End of video

`source_end_pts_exclusive` is strictly greater than the last decodable frame
PTS. Derive it from the last valid decoded-frame duration, then neighboring PTS
spacing, and finally at least one time-base tick with an anomaly marker.

The final clip ends at this exclusive sentinel, so the final decoded frame is
included.

### 4.5 Scene and duration segmentation policy

- Confirmed shot changes always split.
- Sustained motion-mode changes are recorded but do not force a split.
- Short static periods merge into surrounding motion evidence.
- Each continuous scene is divided into the minimum number of clips required
  for every duration to be strictly less than 60 seconds.
- Prefer low-motion, clear decoded frames for within-scene duration cuts.
- If no preferred frame exists, select the latest real decoded-frame PTS that
  keeps the interval strictly below 60 seconds.
- No general eight-second minimum is applied to a continuous scene.

Scene and display naming is separate from identity:

```text
场景 01 · 第 1 段
场景 01 · 第 2 段
场景 02 · 第 1 段
```

`clip_id` is permanent. Scene number and display name are never used as job or
output references.

## 5. Frame-exact physical export

Physical clips are created only when an adapter requires an independent MP4 or
when the user selects an original-video interval as a merge fallback.

Before encoding, build `clip_frame_map.json` from the authoritative decoded
frame index. It records source-frame order and integer PTS for the half-open
interval.

FFmpeg trim receives original absolute PTS:

```text
trim=start_pts=START:end_pts=END,setpts=PTS-STARTPTS
```

Constraints:

- no unverified input-level seeking that changes timestamp semantics;
- no forced frame rate;
- use `fps_mode passthrough` or `vsync 0`;
- no frame duplication or dropping;
- output video starts at zero after `setpts`;
- audio, when used for a clip input, is cut separately to the same time range
  and never controls the video boundary.

Output PTS are local after `setpts` and are not compared directly to source
PTS. Post-export validation uses output frame count/order plus the precomputed
sidecar mapping.

Validation proves:

- the complete source decoded-frame index is partitioned exactly once;
- adjacent mappings do not overlap and have no gaps;
- the first and last source frames are covered;
- each exported MP4 frame count equals its sidecar entry count;
- concatenated sidecars equal the authoritative source-frame sequence.

## 6. Project directory and manifests

```text
projects/<project_id>/
├── project_manifest.json
├── clips_manifest.json
├── jobs_manifest.json
├── render_manifest.json
├── analyses/<analysis_revision>/02_video_analysis/...
├── clip_inputs/<clip_id>/<export_revision>/...
├── jobs/<job_id>/attempt-<n>/...
└── renders/
    ├── clips/<clip_id>/<render_revision>/...
    └── merged/<merge_revision>/...
```

Every manifest contains:

```text
schema_version
revision
updated_at
project_id
```

Responsibilities:

- `project_manifest.json`: project identity, source assets, CAD/video analysis
  state, active/candidate analysis revisions, overall project state.
- `clips_manifest.json`: logical definitions, PTS ranges, analysis evidence,
  layered workflow values, trajectory/workbench/render references.
- `jobs_manifest.json`: job DAG, queue order, attempts, stages, errors, and
  validated outputs. It is the job-state authority.
- `render_manifest.json`: current render revisions, source fallback choices,
  merge plans, media specifications, and final published outputs.

### 6.1 Workflow layers

Each clip stores independent layers:

```text
analysis.recommended_workflow
user.workflow_override       # workflow name or null
resolved.resolved_workflow
```

Resolution:

```text
workflow_override when non-null
otherwise executable recommended_workflow
otherwise null
```

Setting `workflow_override` to null restores system recommendation. A workflow
change marks old trajectory, workbench, and render outputs stale without
deleting them.

### 6.2 Analysis revisions

Analysis outputs are immutable. Reanalysis creates a candidate revision:

```text
active_analysis_revision
candidate_analysis_revision
```

The initial analysis may activate automatically. Later revisions require an
explicit activation operation. Activating a revision never overwrites workflow
overrides or future manual split/merge definitions.

## 7. Atomic repositories and consistency recovery

Repository interfaces:

- `ProjectRepository`
- `ClipRepository`
- `JobRepository`
- `RenderRepository`

The first implementation uses atomic JSON files.

Rules:

- every mutation supplies `expected_revision`;
- a mismatch returns `revision_conflict` with the latest revision;
- each manifest path has a process-level reentrant lock;
- multi-manifest lock order is project → clips → jobs → render;
- write a temporary file in the destination directory;
- flush and fsync it, then atomically replace the target;
- revision increments only as part of the successful replacement.

Every cross-manifest operation receives a unique `operation_id`, stored in all
affected states and references. This is a recovery marker, not a database
transaction.

Each state has one authoritative manifest. Other manifests store stable
references. Startup/read-time reconciliation uses operation IDs and ownership
rules to complete or roll back visible references after a crash between atomic
file replacements.

The project root is single-machine and single-`serve_viewer` only. A process
lease prevents two scheduler instances from owning the same project root.

## 8. Jobs and local resource queue

### 8.1 Job fields

```text
job_id
project_id
clip_id
job_type
resource_class
status
stage
priority
depends_on_job_ids
exclusive_key
idempotency_key
input_revision
input_fingerprint
adapter_name
adapter_version
output_revision
operation_id
attempts[]
```

States:

```text
queued → preparing → running → validating → success
                       │             │
                       └─────────────┴→ failed / interrupted / cancelled

input changes while active → superseded / stale_input
```

`superseded/stale_input` is not reported as an algorithm failure and cannot
publish an old result.

Dependencies are explicit. A job is schedulable only when every
`depends_on_job_id` is successful and its output remains validated for current
inputs.

`exclusive_key` prevents concurrent duplicate trajectory solves or renders for
one clip and concurrent merge jobs for one project.

### 8.2 Resource classes

Static first-version classes:

- `heavy_compute`: trajectory solving and clip rendering;
- `light_compute`: CAD and video analysis;
- `media_io`: clip export, normalization, and concat;
- `control`: validation and manifest coordination.

Local defaults:

```text
heavy_compute = 1
light_compute = 1
media_io = 1
```

CLI settings may increase capacities for a server deployment. Batch actions
create queued jobs; they never launch all SfM jobs immediately.

### 8.3 Attempts, cancellation, and restart

Each attempt writes only to:

```text
jobs/<job_id>/attempt-<n>/
```

Adapters never modify manifests. They return structured execution and
validation results to `ProjectService`.

Cancellation terminates the complete child process tree. Logs and temporary
outputs remain for diagnosis, but no unvalidated result is published.

On restart:

- queued jobs are restored in persistent order;
- running jobs are adopted only if PID, start time, command fingerprint, and
  task token can all be verified;
- otherwise they become `interrupted`;
- interrupted jobs require explicit retry by default;
- existing output can succeed only after input and adapter fingerprints match
  and the adapter validates it.

### 8.4 Idempotency

The idempotency key includes job type, authoritative clip interval, active
analysis revision, resolved workflow, relevant manifest revisions, source file
fingerprints, adapter version, and parameter fingerprint.

Old-input outputs remain historical and can never be restored as current
success.

## 9. Workflow and render adapters

Four `WorkflowAdapter` implementations wrap existing behavior:

- `sfm_only`
- `srt_sfm_fused`
- `srt_full_pose`
- `pure_rotation`

Common interface:

```text
prepare_inputs
build_command
validate_outputs
describe_workbench
describe_render
```

An adapter declares whether it requires a physical MP4, SRT coverage level,
output locations, adapter version, and validation rules. For a subclip, the
adapter layer may generate clip-local SRT input without changing SRT algorithm
semantics.

`WorkflowAdapter` and `RenderAdapter` return structured results only. They do
not update any manifest.

## 10. Project API

Core routes:

```text
POST   /api/projects
POST   /api/projects/{project_id}/uploads/{video|cad|srt}
POST   /api/projects/{project_id}/analysis/start
GET    /api/projects/{project_id}/snapshot

PATCH  /api/projects/{project_id}/clips/{clip_id}/workflow
POST   /api/projects/{project_id}/trajectory-jobs
POST   /api/projects/{project_id}/clips/{clip_id}/render
POST   /api/projects/{project_id}/merge

POST   /api/projects/{project_id}/jobs/{job_id}/cancel
POST   /api/projects/{project_id}/jobs/{job_id}/retry

POST   /api/projects/{project_id}/clips/{clip_id}/workbench-session
POST   /api/projects/{project_id}/clips/{clip_id}/workbench-save
```

All mutations include relevant expected revisions. Batch preflight returns
per-clip groups:

```text
eligible
needs_confirmation
skipped
```

The user may accept partial success.

The project snapshot contains:

- one unified snapshot revision or ETag;
- component manifest revisions;
- project-level capabilities;
- clip-level capabilities;
- display data and technical IDs;
- job, workbench, and render status.

The frontend never derives button availability from raw status fields.

Polling sends the known ETag/revisions every one to two seconds. No change
returns `304` with no response body. Polling pauses or slows when hidden and
must not overwrite unsubmitted local workflow edits.

Existing upload and workbench routes remain compatible for old single-video
usage.

## 11. Project workspace UI

After uploads finish, navigate immediately to the project workspace. CAD and
video analysis progress independently in the page. A failure remains retryable
without returning to upload.

The collapsible sidebar contains exactly:

- 项目概览
- 项目文件
- 片段管理
- 设置

Collapsed mode displays icons only.

The clip table uses product-facing labels:

- scene/segment display name;
- friendly time range and duration;
- detected motion mode and confidence;
- recommended workflow at normal text size;
- editable final workflow;
- current state;
- a separate progress column;
- workbench, log, retry, cancel, and render actions as capabilities allow.

Technical source PTS fields are not the primary user display. There is one
final merge entry point. Dynamic updates preserve selection and local edits,
avoid row flicker, and show errors next to the affected operation.

## 12. Workbench session and return

The project service creates a short-lived same-origin session bound to:

- project and permanent clip ID;
- resolved workflow;
- input revision and fingerprint;
- compatible dataset/run;
- save permissions;
- expiration time;
- allowlisted `return_to` path.

Workbench status:

```text
unavailable
ready
editing
saved
stale
```

The workbench continues writing its normal run output. The coordination save
endpoint validates the session, current input, and adapter output. Every valid
save creates an immutable `workbench_output_revision`; only then is the clip
marked `saved`.

An expired session or unexpectedly closed browser returns unsaved `editing` to
`ready`. Unverified output is never recovered as success.

After save, the allowlisted return URL opens the project page and focuses the
originating clip.

## 13. Project media specification

Each project has one standard media specification covering:

- raster width and height;
- baked display orientation;
- sample aspect ratio;
- pixel format;
- video codec/profile;
- output time base;
- color range, primaries, transfer, and matrix.

Clip renderers should emit this specification directly. Display rotation is
baked into pixels rather than retained as rotation metadata.

Every rendered source frame maps to exactly one output video frame. Rendering
must not add, duplicate, or drop frames:

```text
rendered frame count == render_frame_map entry count
```

Per-clip render outputs:

```text
rendered.mp4
render_metadata.json
render_frame_map.json
validation_report.json
```

Each successful render creates an immutable `render_output_revision`.

## 14. Merge and source fallback

Merge preflight categorizes each clip as:

```text
ready_rendered
needs_source_fallback_confirmation
blocked
stale
```

An incomplete clip blocks merge unless the user explicitly chooses source
fallback for that individual clip. Source fallback uses the same decoded-frame
integer-PTS half-open exporter and produces a frame map; it never uses coarse
floating `-ss/-t` cutting.

Inputs are ordered strictly by `source_start_pts`. `render_order` is retained
for future compatibility but must equal source order in this version.

Before concat, validate:

- resolution and baked orientation;
- SAR and pixel format;
- codec/profile and video time base;
- color metadata;
- audio presence and parameters;
- zero-based, monotonic video PTS with no negative timestamps;
- current frame-map revisions.

If video parameters differ, normalize incompatible segments to the project
media specification while preserving one input frame per output frame with
passthrough frame timing. Revalidate before concat.

The current version does not edit or concatenate per-clip audio. Final merged
audio should preferentially use the complete original long-video audio track,
muxed against the finished video. This avoids audio boundary duplication and
re-encoding across clips. Audio never controls video-frame boundaries.

After concat, validate:

- zero start, monotonic PTS, and no negative timestamps;
- final decoded frame count;
- media compatibility;
- final frame map exactly equals the complete authoritative source decoded-frame
  sequence, with no duplicate or missing boundary frames.

Any input revision change during execution produces `superseded/stale_input`.
The task retains diagnostics and publishes nothing.

## 15. Test strategy and staged commits

Every stage follows red-green-refactor TDD and receives an independent commit.

1. **Stage 8A boundary repair**
   - decoded-frame presentation-order index;
   - hard cut, black transition, non-zero start PTS;
   - exclusive source end and final-frame coverage;
   - pre/post physical-export partition verification.
2. **Project domain and repositories**
   - four schemas;
   - expected revision and process locks;
   - operation IDs and recovery;
   - immutable analysis revision behavior.
3. **Queue and adapters**
   - dependency gating and exclusive keys;
   - bounded resource scheduling;
   - cancellation of process trees;
   - interruption and stale-input recovery;
   - four workflow adapters without algorithm changes.
4. **API and project workspace**
   - upload-to-project flow;
   - snapshot ETag/capabilities;
   - workflow override/reset;
   - batch partial preflight;
   - responsive collapsible navigation and dynamic status.
5. **Workbench round trip**
   - bound/expiring sessions;
   - validated immutable saves;
   - return and clip-state restoration.
6. **Render and concat**
   - project media specification;
   - frame-exact render maps;
   - explicit source fallback;
   - normalization and original-audio mux;
   - final frame-map equality.
7. **Real closure and regression**
   - `jinhuaorigin.mp4` and its seven known scene transitions;
   - old short-video single clip;
   - full user flow through merge;
   - performance time and peak resource measurement;
   - focused, workflow-related, and full regression suites.

Synthetic coverage includes hard cuts, multi-frame black transitions, non-zero
starting PTS, variable frame spacing, missing frame duration, differing render
media parameters, job failure/cancel/retry, restart interruption, revision
conflict, and old-result isolation.

## 16. Completion report

The final report must include:

- base commit, worktree, branch, and staged commits;
- algorithms and default analysis/queue parameters;
- boundary, motion, duration, and frame-ownership rules;
- clip/project/job/render schemas;
- real-video scene and clip results, workflows, and review flags;
- queue behavior, retry/restart behavior, and workbench round trip;
- boundary-frame and final-concat validation;
- timing and peak resources;
- focused, related, workflow, and full regression results;
- an explicit statement that project labels/tracking, complex scheduling,
  cross-clip pose merging, and existing algorithm math were not developed or
  modified.
