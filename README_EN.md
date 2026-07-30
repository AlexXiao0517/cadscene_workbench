# CADScene Workbench

English | [中文](README.md)

CADScene Workbench is a local video-and-CAD workbench for architecture, road, and related design work. It helps you organize site video, CAD drawings, and necessary human confirmation into results that can be viewed, checked, and exported.

Here, SfM (3D reconstruction) means estimating camera motion and scene structure from video images so that video content can be aligned with CAD references. This README focuses on using the interface. For deployment, maintenance, and follow-on development, start from the [documentation hub](docs/README.md).

## Suitable scenarios and preparation

Use the workbench when you need to check a building or road design against site video, inspect camera views, or support video-to-CAD alignment.

Prepare the following before you begin:

- a clear, continuous site video; the portal supports MP4, MOV, AVI, and MKV;
- CAD files that correspond to the project. Basic upload accepts DXF or DWG; the workbench compatibility-asset import also accepts `design.json` or ZIP. DXF can be imported directly, while DWG requires an external converter in the deployment environment;
- an optional ordinary SRT file. It provides metadata hints such as time and location only; it is not high-precision position, pose, or CAD elevation truth. After upload, the portal displays a detected mode; if it is Interface only, create a new project without SRT or contact a maintainer;
- someone familiar with both the site and the drawings, to confirm key images and the field of view (FOV, the visible extent of an image).

## Inputs, processing, and outputs

The workbench accepts video, CAD files, and optional SRT. After processing, task results can include reconstruction and alignment summaries, keyframes, quality guidance, a viewer scene, road-surface diagnostics where applicable, and render previews, with usable outputs available for export.

Upload video and CAD together as one project set. The portal automatically displays a detected mode, and ordinary users do not have a control for switching normal workflows. If SRT produces an Interface only mode, create a new project without SRT or contact a maintainer. When processing completes, check the keyframes, FOV, and quality guidance before using the results for design communication or further review.

## Quick start

This section assumes that a maintainer has already deployed the workbench. Start the local service:

```bash
python -m cadscene.cli.serve_viewer --bind 127.0.0.1 --port 8300
```

Then open this address in a browser:

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

Create a task in the portal and upload the video and CAD files. SRT is optional metadata; when you upload it, check the detected mode that follows. If it is Interface only, create a new project without SRT or contact a maintainer before starting processing.

## Workflow status detected by the portal

| Workflow or capability | Status | When to use it and its boundary |
|---|---|---|
| `sfm_only` | Stable | The only stable end-to-end primary path today. Use it for video-to-CAD alignment; SRT is not required. |
| `pure_rotation` | Experimental | For footage captured with a fixed camera center and pure rotation; it does not recover camera translation or scale, and depends on an external OpenGV backend. |
| partial-SRT core | Experimental CLI | The fusion core covers timestamps (PTS), local east-north-up coordinates (ENU), and robust Sim3 (alignment of position, rotation, and scale); it is not connected to the formal job runner and is not a formal portal workflow. |
| `srt_sfm_fused` portal route | Interface only | The portal can identify and describe this route, but blocks formal workflow launch. |
| `srt_full_pose` | Interface only | No end-to-end execution workflow exists yet. |
| SfM CUDA | Optional | CUDA is confirmed only for feature extraction and matching; mapping and global bundle adjustment (global BA, whole-camera optimization) must not be understood as GPU processing. |

The portal displays a detected mode from the uploaded inputs; ordinary users do not have a control for switching normal workflows. Without SRT, the portal detects `sfm_only`. If uploading SRT produces an Interface only mode, create a new project without SRT or contact a maintainer; do not treat the prompt as an executable workflow.

## From upload to result export

1. In the portal, select video, CAD, and optional SRT for “新建项目” (New project), then click “创建并上传” (Create and upload). Confirm the “检测结果” (Detected result) and click “进入项目” (Enter project). If the result is Interface only, create a project without SRT or contact a maintainer.
2. In the “SfM 重建” (SfM reconstruction) stage, click “开始 SfM 重建” (Start SfM reconstruction) and wait for reconstruction to complete.
3. In “关键帧标定” (Keyframe calibration), confirm the initial manual keyframes and click “路线拟合” (Fit route). At least two manual keyframes are required. Then click “生成关键帧计划” (Generate keyframe plan) and complete the remaining planned keyframe calibrations.
4. Click “完成关键帧标定” (Complete keyframe calibration) so the workbench runs the final route fit and enters “质量检测” (Quality check).
5. Click “运行质量检测” (Run quality check), review the quality guidance, then click “完成质量检测” (Complete quality check). If more frames are needed, use “返回关键帧标定” (Return to keyframe calibration).
6. In “渲染导出” (Render and export), click “渲染视频” (Render video). When it finishes, use “预览视频” (Preview video) or “下载结果” (Download result), and retain the task summary for later review.

For `pure_rotation`, the workbench presents the experimental steps “开始运行” (Start), “进入关键帧标定” (Enter keyframe calibration), and “完成调试并进入渲染” (Complete adjustment and enter rendering). It is not for recovering travel distance.

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
