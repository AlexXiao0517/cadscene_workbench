# CADScene Workbench

English | [中文](README.md)

CADScene Workbench is a local video-and-CAD workbench for architecture, roads, and related engineering work. It organizes site video, CAD drawings, video analysis, clip trajectories, manual calibration, engineering callouts, rendering, and project concatenation into one recoverable project workflow.

Here, SfM (3D reconstruction) means estimating camera motion and scene structure from video, then using manual keyframes to align the camera trajectory with CAD. This README covers normal operation. Start from the [documentation hub](docs/README.md) for deployment, APIs, storage, and follow-on development.

## Suitable scenarios and preparation

Use the workbench to compare site video with building or road designs, reconstruct camera views, align video with CAD, annotate engineering locations in delivered video, and iterate CAD drawings that retain the same coordinate system.

Prepare:

- a clear, continuous MP4 site video;
- a matching DXF drawing. The official upload UI currently supports only MP4 video and DXF drawings. Extra extensions accepted by compatibility-oriented backend endpoints are not a promise of current UI support or end-to-end qualification;
- an optional ordinary SRT file. It supplies metadata hints such as time and location, not high-precision position, pose, or CAD elevation truth. If the portal reports Interface only, create a project without SRT or contact a maintainer;
- someone familiar with the site and drawing who can confirm key images, CAD locations, and field of view (FOV).

DXF `TEXT`, `MTEXT`, and block-attribute text are imported into the CAD view with their layer, position, rotation, and text size. Station labels and other engineering notes can therefore be used while aligning keyframes.

## Quick start

Always specify a writable storage root for normal testing and long-lived projects so that project data and render outputs are not written into the source tree. PowerShell example:

```powershell
New-Item -ItemType Directory -Force -Path D:\cadscene-work | Out-Null
python -m cadscene.cli.serve_viewer `
  --bind 127.0.0.1 `
  --port 8300 `
  --storage-root D:\cadscene-work
```

Open the upload portal:

```text
http://127.0.0.1:8300/apps/workflow_portal/
```

If the `project_id` is known, open project clip management directly:

```text
http://127.0.0.1:8300/apps/project_workspace/?projectId=<project_id>
```

`--root` serves static application files and defaults to the repository root. `--storage-root` holds writable project data under `projects/`. If `--storage-root` is omitted, it inherits `--root`, so a `projects/` directory is created in the source tree. Use the same explicit storage root after every restart.

## Current workflow status

| Workflow or capability | Status | Boundary |
|---|---|---|
| `sfm_only` | Stable | The stable end-to-end path: video analysis, clip management, SfM, manual alignment, quality inspection, rendering, and concatenation. |
| `pure_rotation` | Experimental | Automatically recommended when video analysis verifies strict rotation evidence. The camera center is fixed, translation and scale are not recovered, and an external OpenGV backend is required. |
| partial-SRT core | Experimental CLI | Provides PTS, ENU, and robust Sim3 fusion helpers outside the formal project queue. |
| `srt_sfm_fused` / `srt_full_pose` | Interface only | The portal can detect and describe these routes, but formal stage launch remains blocked. |
| SfM CUDA | Optional | Confirmed only for supported feature extraction and matching. Mapping and global BA are not represented as GPU processing. |
| CAD-anchored engineering callout | Supported | Selects a CAD world point and projects it through the current effective camera trajectory in preview and final rendering. |
| Video-target tracked callout | Hidden baseline | Backend and historical-data compatibility remain, but the creation control is hidden and this is not a supported user-facing capability. |

The upload page no longer asks the user to preselect a rotation mode. When no SRT interface route takes precedence, automatic motion analysis can conservatively recommend `sfm_only` or `pure_rotation`. For those executable routes, Project Clip Management shows the recommendation and uses a final-workflow selector containing only SfM and OpenGV. An Interface-only SRT recommendation can appear in the recommendation column while the selector visually falls back to SfM; that visual fallback does not prove an override was saved. The current project cannot rely on it to enter the stable path, so create a project without SRT instead. A recommendation is routing evidence, not an accuracy guarantee. Ordinary SRT does not replace SfM, manual calibration, SfM-to-CAD alignment, or quality inspection.

## From upload to project delivery

1. Create a project in the upload portal and select video, CAD, and optional SRT. The service stores immutable inputs, then asynchronously performs video analysis, scene segmentation, clip export, and CAD import.
2. Open Project Clip Management and review the automatically detected motion mode and recommended workflow. The final-workflow selector exposes only SfM and OpenGV. Normally keep the corresponding recommendation and override it when it conflicts with the known scene. Do not treat a visible SfM fallback for an Interface-only SRT recommendation as a saved override; create a project without SRT when the stable route is required. You can enqueue trajectory solving or rendering for selected clips, or open one clip in the workbench.
3. For an `sfm_only` clip, run SfM. During keyframe calibration, save at least two valid manual keyframes and fit the route. Generate the keyframe plan, finish the remaining calibration, and run the final fit.
4. Run quality inspection. After it passes, choose Complete Quality Inspection to enter Render and Export. If more frames are required, return to keyframe calibration.
5. In Render and Export, either create CAD-anchored engineering callouts or render without callouts. Render Video binds the current annotation state into the immutable render revision.
6. Return to Project Clip Management. When every delivery clip has a current render, run Merge Output. Concatenation uses each `render_frame_map.json` source-PTS partition and rejects duplicate or missing frames.
7. After a refresh or service restart, use the same `--storage-root` and `project_id`. Saved workbench outputs, annotations, jobs, renders, and version history are restored from project manifests.

`pure_rotation` follows the experimental sequence Start—Global Placement—Local Pose Corrections—Render, and skips the standard alignment/quality route. It cannot recover travel distance.

## CAD text and engineering callouts

Original CAD text and user-created engineering callouts are separate:

- Original CAD text comes from DXF and appears in the right-hand 3D CAD view for station labels and other drawing notes. Large drawings use visibility and display budgets to avoid a substantial interaction penalty.
- An engineering callout consists of a screen-space card, a polyline leader, and a circular anchor. The card has a title and multiline body, remains screen-facing, and does not inherit perspective tilt from a CAD plane.
- Pause the video, choose CAD Anchor, and click a point in the right-hand CAD view. Dragging the card changes only its `screen_offset`; it never changes the stored CAD world coordinate.
- During playback and seeking, the point is projected through the current effective Base/Corrected camera trajectory. The complete callout—card, leader, and anchor—is hidden when the point is behind the camera, outside the viewport, has no valid trajectory, or is outside its source-PTS range.
- Editing title, body, style, or offset marks only that clip render stale; it does not rerun camera trajectory solving. Rendering without callouts uses the existing fast path. When callouts exist, they are burned into `rendered.mp4`.

Authoritative synchronization uses source decoded-frame integer PTS and the exact `time_base`. It never derives source frames from `currentTime × fps` or a fixed-FPS frame number.

## Global CAD replacement

After at least one valid workbench output proves that the project coordinate system has been calibrated, the CAD card exposes a hover-only Replace rail:

DXF remains the officially qualified and user-supported replacement path. Other compatibility-oriented filters retained by the replacement dialog are for migration and are not an end-to-end format-support promise.

1. Select a new DXF and confirm “The new CAD uses the same coordinate system as this project.”
2. The service imports and validates the new drawing as a candidate. The previous CAD remains active until successful publication.
3. Success switches the entire project to the new CAD. Existing clips, SfM, trajectories, keyframes, and workbench outputs remain valid; CAD-dependent renders and the merged output become stale.
4. Reopen a clip, inspect the new drawing, make local adjustments if needed, and render again without repeating initial calibration or route fitting.
5. Failure keeps the previous CAD active and retains the error and CAD-version history for diagnosis.

Do not use this action if the new drawing changes coordinate system, unit, or origin; create a new project and recalibrate instead. On restart, the service validates pre-replacement analysis jobs against their immutable original input snapshots and then rebinds them to the current project contract. A valid replacement therefore does not rerun analysis or invalidate the entire project.

## Frequently asked questions

### Why is a task pending, or why does progress pause?

Project work uses a local resource queue. `pending` means waiting for a dependency or resource slot; it does not mean the algorithm is already running. Progress reaches 100% only after preparation, computation, validation, and publication all finish. Check the stage text, runtime details, and service log together.

### How long does processing take?

It depends on video length, image quality, hardware, and workflow. Video analysis, SfM, rendering, validation, and publication are different phases. Do not infer whole-job completion from an animation or one percentage transition.

### Do I need a GPU?

No. The default is `pycolmap + CPU`. CUDA must be explicitly selected and pass capability detection, and it covers only confirmed feature extraction and matching.

### What if FOV is inconsistent or alignment looks wrong?

Review mutually consistent manual-keyframe FOV, CAD anchors, scale, and origin. FOV alone does not guarantee SfM accuracy. Return to keyframes and add or correct anchors when needed.

### Why does a refreshed workbench URL report an expired session?

A workbench session token is temporary write authorization; a saved workbench output is the durable result. Return to the same Project Clip Management page and choose Open Workbench again instead of reusing an old token URL. Refreshing the project page does not delete saved output.

## Capability boundaries

- `sfm_only` is the stable primary path; SRT fusion and full-pose do not yet have formal end-to-end routes.
- `pure_rotation`, partial-SRT, and video-target tracked callouts remain experimental or hidden.
- CAD-anchored callouts do not infer real-object occlusion in the source video; they currently evaluate camera-facing, viewport, trajectory, and time-range visibility.
- Cross-clip tracking, cross-scene label inheritance, semantic recognition, and large neural trackers are not supported.
- Results require review by someone who understands the site and design intent. Experimental features do not replace engineering survey or professional acceptance.

## Storage, results, and troubleshooting

Current project data lives under `<storage-root>/projects/<project_id>/`, including five domain manifests, immutable analysis/workbench/render outputs, and job-attempt directories. Legacy `<storage-root>/data/` and `<storage-root>/runs/` serve compatibility workflows or historical assets; they are not the primary storage location for the current project domain.

Do not edit manifests manually, move individual revisions, or copy active workbench tokens. For upload, recovery, job, CAD, annotation, render, or concatenation problems, retain the `project_id`, `clip_id`, job ID, project snapshot, and relevant logs, then consult [troubleshooting](docs/technical/troubleshooting.md).

## Maintenance and follow-on development

Use the [documentation hub](docs/README.md) for system architecture, developer setup, HTTP APIs and artifacts, troubleshooting, workflow routing, SRT capability, CUDA, and viewer operation.

## Roadmap, changes, acknowledgements, and license

- See the [roadmap](docs/roadmap.md) for future work and priorities.
- See the [CHANGELOG](CHANGELOG.md) for delivered and unreleased behavior changes.
- Thanks to the communities and maintainers contributing to 3D reconstruction, CAD collaboration, video processing, and the open-source dependency ecosystem.
- This repository does not yet declare an open-source license. Do not treat it as granting open-source use before an explicit license is provided.
