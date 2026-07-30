# CADScene Workbench

English | [中文](README.md)

CADScene Workbench is a local video-and-CAD workbench for architecture, road, and related design work. It helps you organize site video, CAD drawings, and necessary human confirmation into results that can be viewed, checked, and exported.

Here, SfM (3D reconstruction) means estimating camera motion and scene structure from video images so that video content can be aligned with CAD references. This README focuses on using the interface. For deployment, maintenance, and follow-on development, start from the [documentation hub](docs/README.md).

## Suitable scenarios and preparation

Use the workbench when you need to check a building or road design against site video, inspect camera views, or support video-to-CAD alignment.

Prepare the following before you begin:

- a clear, continuous site video;
- CAD files that correspond to the project; whether DWG can be used directly depends on external conversion tools in the deployment environment;
- an optional ordinary SRT file. It can provide metadata hints such as time and location, but it is not high-precision position, pose, or CAD elevation truth;
- someone familiar with both the site and the drawings, to confirm key images and the field of view (FOV, the visible extent of an image).

## Inputs, processing, and outputs

The workbench accepts video, CAD files, and optional SRT. After processing, task results can include reconstruction and alignment summaries, keyframes, quality guidance, a viewer scene, road-surface diagnostics where applicable, and render previews, with usable outputs available for export.

Upload video and CAD together as one project set, then choose a workflow according to the workbench analysis and the project situation. When processing completes, check the keyframes, FOV, and quality guidance before using the results for design communication or further review.

## Quick start

This section assumes that a maintainer has already deployed the workbench. Start the local service:

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

Then open this address in a browser:

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

Create a task in the portal and upload the video and CAD files. Upload SRT as well when it is available. Read the workbench workflow recommendation before starting processing.

## Choosing a workflow

| Workflow or capability | Status | When to use it and its boundary |
|---|---|---|
| `sfm_only` | Stable | The only stable end-to-end primary path today. Use it for video-to-CAD alignment; SRT is not required. |
| `pure_rotation` | Experimental | For footage captured with a fixed camera center and pure rotation; it does not recover camera translation or scale, and depends on an external OpenGV backend. |
| partial-SRT core | Experimental CLI core | The fusion core covers timestamps (PTS), local east-north-up coordinates (ENU), and robust Sim3 (alignment of position, rotation, and scale); it is not connected to the formal job runner and is not a formal portal workflow. |
| `srt_sfm_fused` portal route | Interface only | The portal can identify and describe this route, but blocks formal workflow launch. |
| `srt_full_pose` | Interface only | No end-to-end execution workflow exists yet. |
| SfM CUDA | Optional | CUDA is confirmed only for feature extraction and matching; mapping and global bundle adjustment (global BA, whole-camera optimization) must not be understood as GPU processing. |

Choose `sfm_only` unless you have a specific experimental need. The workbench recommendation supports, but does not replace, your judgement about the capture method, CAD completeness, and site knowledge.

## From upload to result export

1. Create a task in the portal and upload the video, CAD files, and optional SRT.
2. Check the input analysis and recommended route. If an interface-only route appears, use `sfm_only` or contact a maintainer instead; do not treat the prompt as an executable fusion workflow.
3. Start the task and wait until its status makes results available for viewing.
4. Inspect the keyframes, quality guidance, and viewer scene. Check whether they are reasonable against the CAD and the site.
5. If adjustment is needed, confirm the FOV of manual keyframes first, then reprocess or ask a maintainer for help.
6. Preview, download, or deliver the required outputs from the task results page, and retain the task summary for later review.

## Frequently asked questions

### How long does processing take?

Time depends on video length, image quality, hardware, and the selected workflow. SfM is computationally intensive; use task progress and the result summary as the practical reference.

### Do I need a GPU?

No. The default is `pycolmap + CPU`. CUDA must be explicitly selected and pass capability detection. If it cannot be confirmed, the workbench falls back to CPU and records this in the log and summary. CUDA covers only the confirmed feature-extraction and matching scope.

### What if FOV is inconsistent or the alignment looks wrong?

Prioritize confirmed, mutually consistent manual-keyframe FOV values. FOV does not guarantee that SfM is accurate. Check the keyframes, quality guidance, and CAD/site comparison, and select keyframes again if needed.

### Does SRT provide high-precision position and pose?

No. Ordinary SRT mainly provides metadata capability hints. It does not replace SfM, manual calibration, SfM-to-CAD alignment, or quality checks, and it is not CAD elevation truth.

### Can pure-rotation video recover travel distance?

No. `pure_rotation` does not recover camera translation or scale, and it does not automatically decide whether footage is pure rotation.

## Capability boundaries

- The stable primary path is currently `sfm_only`; SRT fusion and full-pose entry points are not yet formal end-to-end workflows.
- Successful CAD import also depends on file contents and the external conversion capability of the deployment environment.
- Results must be reviewed by people who understand the site and design intent. Experimental capabilities do not replace engineering survey or professional acceptance.
- `--root` serves static interface resources, while `--storage-root` serves workflow data and results; maintainers must configure them separately for the deployment.

## Results and troubleshooting

Use the task results page to view summaries, previews, and downloadable outputs. If upload fails, a task cannot start, CUDA falls back, CAD is not ready, FOV is inconsistent, or the viewer looks wrong, retain the task summary and input details, then use the [documentation hub](docs/README.md) to find the relevant guidance or contact a maintainer.

## Maintenance and follow-on development

Installation, environment checks, batch operation, interfaces, artifact storage, troubleshooting, and testing are not expanded in this README. Start from the [documentation hub](docs/README.md) for maintainer-oriented technical documentation, including workflow routing, [SRT capability detection](docs/srt_capability_detection.md), the [CUDA backend](docs/sfm_cuda_backend.md), and [viewer usage](docs/web_viewer_usage.md).

## Roadmap, changes, acknowledgements, and license

- See the [roadmap](docs/roadmap.md) for future capabilities and priorities.
- See the [CHANGELOG](CHANGELOG.md) for released and unreleased behavior changes.
- Thanks to the communities and maintainers contributing to 3D reconstruction, CAD collaboration, video processing, and the open-source dependency ecosystem.
- This repository does not yet declare an open-source license. Do not treat it as granting open-source use before an explicit license is provided.
