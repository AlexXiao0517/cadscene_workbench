(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const dataset = params.get("dataset") || "";
  const runId = params.get("runId") || "";
  const projectWorkbenchToken = params.get("projectWorkbenchToken") || "";
  const projectWorkbenchProjectId = params.get("projectId") || dataset;
  const requestedWorkflowStage = params.get("workflowStage") || "";
  const THEME_STORAGE_KEY = "mediaflow-theme";
  const PROJECT_WORKBENCH_HEARTBEAT_MS = 60_000;
  const PROJECT_WORKBENCH_RESUME_DEBOUNCE_MS = 300;
  const debugEnabled = params.get("debug") === "1" || window.VIEWER_DEBUG === true; // debug=1
  const stageOrder = ["upload", "sfm", "keyframes", "quality", "render"];
  const stageTitles = {
    upload: "上传数据",
    sfm: "SfM重建",
    keyframes: "关键帧标定",
    quality: "质量检测",
    render: "渲染导出",
  };
  const panel = document.querySelector("#workflowPanel");
  if (!panel) return;

  const progress = document.querySelector("#workflowProgress");
  const message = document.querySelector("#workflowMessage");
  const stateLabel = document.querySelector("#workflowJobState");
  const title = document.querySelector("#workflowTaskTitle");
  let trajectoryWorkflow = null;
  let trajectoryWorkflowLoaded = !dataset;
  let viewerReadyForSfmCameraInit = false;
  let sfmCameraInitializationPromise = null;
  let sfmCameraInitializationComplete = false;
  let selectedWorkflowStage = null;
  let latestJobStatus = null;
  let workflowSuggestions = [];
  let ignoredSuggestionFrames = new Set();
  let selectedSuggestionFrame = null;
  let runningStage = null;
  let alignmentArtifactReady = false;
  let keyframePlan = null;
  let keyframeSaveInFlight = false;
  let renderProgressState = null;
  let projectRenderVisibleProgress = 0;
  let projectRenderOutputUrl = null;
  let pureRotationCorrections = [];
  let pureRotationTrajectory = null;
  let pureRotationTrajectoryKind = null;
  let pureRotationRawTrajectory = null;
  let pureRotationDraftPlacement = null;
  let pureRotationDisplayFov = 70;
  let pureRotationFovSource = "default";
  let pureRotationEditMode = "placement";
  let pureRotationHasPlacement = false;
  let pureRotationSavedPlacement = null;
  let pureRotationEditModeReady = Promise.resolve(null);
  let pureRotationEditModeTransitioning = false;
  let pureRotationCorrectionDraftBase = null;
  let pureRotationCorrectionDraftDirty = false;
  let pureRotationWorldYawDeg = 0;
  let pureRotationLocalDelta = { yaw: 0, pitch: 0, roll: 0 };
  let projectWorkbenchSession = null;
  let projectWorkbenchBootstrapPromise = null;
  let projectWorkbenchBootstrapFailed = false;
  let projectWorkbenchEditingHeartbeatTimer = null;
  let projectWorkbenchEditingHeartbeatPromise = null;
  let projectWorkbenchSaveInFlight = false;
  let projectWorkbenchTrajectoryJobId = null;
  let projectWorkbenchTrajectoryStatus = null;
  let projectWorkbenchRenderJobId = null;
  let projectWorkbenchRenderStatus = null;
  let projectWorkbenchTrajectoryStartPromise = null;
  let projectWorkbenchInternalNavigation = false;
  let projectWorkbenchResumeTimer = null;
  let projectWorkbenchResumePromise = null;
  let projectWorkbenchResumePositionApplied = false;
  let focusPureRotationCameraOnce = true;
  let pureRotationHandledCompletion = null;

  function applyWorkbenchTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const toggle = document.querySelector("#workbenchThemeToggle");
    if (!toggle) return;
    const isLight = theme === "light";
    const help = isLight ? "切换为深色模式" : "切换为浅色模式";
    toggle.setAttribute("aria-pressed", String(isLight));
    toggle.setAttribute("aria-label", help);
    toggle.setAttribute("title", help);
  }

  function initializeWorkbenchTheme() {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    const preferred = window.matchMedia?.("(prefers-color-scheme: light)").matches
      ? "light"
      : "dark";
    applyWorkbenchTheme(saved === "light" || saved === "dark" ? saved : preferred);
  }

  function uploadTimestamp() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  }

  function renderTrajectoryWorkflow(workflow) {
    const mode = String(workflow?.trajectory_mode || "sfm_only");
    const implementation = String(workflow?.implementation_status || "ready");
    document.querySelector("#sfmPanel")?.toggleAttribute("hidden", mode === "pure_rotation");
    applyPureRotationWorkflowLayout(mode);
    if (mode === "pure_rotation") {
      window.setTimeout(async () => {
        await initializePureRotationViewer();
        const resumeStage = projectWorkbenchSession?.resume_state?.workflow_stage;
        if (stageOrder.includes(resumeStage)) {
          setWorkflowStage(resumeStage);
          await refreshRenderOutputState();
          return;
        }
        if (
          projectWorkbenchToken
          && projectWorkbenchSession?.workbench_output_revision
        ) {
          setWorkflowStage("render");
          await refreshRenderOutputState();
          return;
        }
        setWorkflowStage(await detectWorkflowStageFromArtifacts());
      }, 0);
    }
    if (implementation !== "interface_only") {
      refreshSupplementalWorkflowActionAvailability();
      return;
    }
    const explanation = "当前 SRT 轨迹能力仅提供界面提示；融合或直接姿态驱动尚未实现，因此不会启动相关流程。";
    document.querySelectorAll("[data-job-action], #workflowGenerateKeyframes, #workflowContinueKeyframes, #workflowFinishKeyframes, #workflowFinishQuality, #workflowReturnKeyframes").forEach((button) => {
      button.disabled = true;
      button.title = explanation;
    });
    if (message) message.textContent = explanation;
  }

  function isPureRotationWorkflow() {
    return trajectoryWorkflow?.trajectory_mode === "pure_rotation";
  }

  function applyProjectWorkbenchSessionWorkflow(session) {
    const mode = session?.workflow === "pure_rotation" ? "pure_rotation" : "sfm_only";
    trajectoryWorkflow = {
      ...(trajectoryWorkflow || {}),
      trajectory_mode: mode,
      implementation_status: "ready",
    };
    trajectoryWorkflowLoaded = true;
    renderTrajectoryWorkflow(trajectoryWorkflow);
  }

  function setPureVisible(selector, visible) {
    document.querySelectorAll(selector).forEach((node) => {
      node.classList.toggle("is-pure-visible", visible);
    });
  }

  function updatePureRotationRecoveryActions({ ready = false, running = false } = {}) {
    const runButton = document.querySelector("#workflowStartPureRotation");
    const rerunButton = document.querySelector("#workflowRerunPureRotation");
    const blocked = trajectoryWorkflowActionsAreBlocked();
    if (runButton) {
      runButton.hidden = false;
      runButton.disabled = running || blocked;
    }
    if (rerunButton) {
      rerunButton.hidden = !ready;
      rerunButton.disabled = !ready || running || blocked;
    }
  }

  function applyPureRotationWorkflowLayout(mode) {
    const pure = mode === "pure_rotation";
    document.querySelector("#workflowSteps")?.classList.toggle("is-pure-rotation", pure);
    stageTitles.sfm = pure ? "OpenGV 旋转轨迹恢复" : "SfM重建";
    const sfmLabel = document.querySelector("#workflowSfmStageLabel");
    if (sfmLabel) sfmLabel.textContent = pure ? "旋转轨迹恢复" : "SfM重建";
    const keyframeLabel = document.querySelector("#workflowKeyframeStageLabel");
    if (keyframeLabel) keyframeLabel.textContent = pure ? "调试" : "关键帧标定";
    document.querySelector('#workflowSteps li[data-stage="quality"]')?.toggleAttribute("hidden", pure);
    const renderOrdinal = document.querySelector('#workflowSteps li[data-stage="render"] span');
    if (renderOrdinal) renderOrdinal.textContent = pure ? "4" : "5";
    document.querySelector("#workflowStartSfm")?.toggleAttribute("hidden", pure);
    document.querySelector("#pureRotationRecoveryActions")?.classList.toggle("is-pure-visible", pure);
    const pureRunButton = document.querySelector("#workflowStartPureRotation");
    if (pure && pureRunButton && !runningStage && !trajectoryWorkflowActionsAreBlocked()) {
      pureRunButton.disabled = false;
      pureRunButton.removeAttribute("title");
    }
    document.querySelector("#workflowSfmRuntimeSummary")?.toggleAttribute("hidden", pure);
    document.querySelector("#standardSfmAdvanced")?.toggleAttribute("hidden", pure);
    document.querySelector("#standardKeyframeActions")?.toggleAttribute("hidden", pure);
    document.querySelector("#pureRotationKeyframeActions")?.classList.toggle("is-pure-visible", pure);
    document.querySelector("#qualityTimelineWrap")?.toggleAttribute("hidden", pure);
    setPureVisible(
      "#pureRotationCalibrationPanel, #pureRotationControlNotice, "
        + "#workflowReturnPureCalibration",
      pure,
    );
    document.querySelectorAll("#viewCurrentSuggestion, #ignoreCurrentSuggestion").forEach((node) => {
      node.toggleAttribute("hidden", pure);
    });
    for (const id of [
      "resetCamera",
      "addKeyframe",
      "deleteKeyframe",
      "previousKeyframe",
      "nextKeyframe",
      "toggleGizmo",
      "toggleCadText",
    ]) {
      document.querySelector(`#${id}`)?.toggleAttribute("hidden", pure);
    }
    document.querySelectorAll("#cameraToolbar .dev-only-control").forEach((node) => {
      node.toggleAttribute("hidden", pure);
    });
    const keyframeStatus = document.querySelector("#workflowKeyframePlanStatus");
    if (pure && keyframeStatus) {
      keyframeStatus.textContent = "调整固定相机的位置与方向，确认后播放视频检查整段旋转效果；满意后进入渲染导出。";
    }
    const sfmStatus = document.querySelector("#workflowSfmStatus");
    if (pure && sfmStatus) sfmStatus.textContent = "等待 OpenGV 旋转轨迹任务；该任务只恢复旋转，不恢复平移。";
    if (pure) {
      setPureRotationEditMode("placement");
      updatePureRotationRecoveryActions();
    }
    if (selectedWorkflowStage) {
      renderWorkflowPanel(selectedWorkflowStage);
    }
  }

  function pureRotationPoseAtPts(trajectory, pts_time_sec) {
    return window.CadscenePureRotationMath.poseAtPts(trajectory?.poses || [], pts_time_sec);
  }
  async function loadPureRotationTrack(kind) {
    const response = await fetch(`/api/pure-rotation/trajectory?dataset=${encodeURIComponent(dataset)}&runId=${encodeURIComponent(runId)}&kind=${encodeURIComponent(kind)}`, { cache: "no-store" });
    if (!response.ok) throw new Error("pure-rotation trajectory unavailable");
    return (await response.json()).trajectory;
  }
  async function initializePureRotationViewer() {
    if (!dataset || !runId) return;
    const response = await fetch(`/api/pure-rotation/status?dataset=${encodeURIComponent(dataset)}&runId=${encodeURIComponent(runId)}`, { cache: "no-store" });
    if (response.ok) {
      const status = await response.json();
      pureRotationCorrections = status.corrections?.corrections || [];
      const placement = status.placement;
      pureRotationSavedPlacement = placement || null;
      pureRotationHasPlacement = Boolean(placement);
      const candidate = status.summary?.candidate;
      const candidateFov = window.CadscenePureRotationMath.horizontalFovDeg(candidate?.width, candidate?.fx);
      if (placement && Number.isFinite(Number(placement.fov))) {
        pureRotationDisplayFov = Number(placement.fov);
        pureRotationFovSource = "saved_placement";
      } else if (Number.isFinite(candidateFov)) {
        pureRotationDisplayFov = candidateFov;
        pureRotationFovSource = "unverified_candidate_intrinsics";
      }
    }
    pureRotationRawTrajectory = await loadPureRotationTrack("raw");
    const initialTrajectoryKind = pureRotationHasPlacement ? "base" : "raw";
    pureRotationTrajectory = initialTrajectoryKind === "raw"
      ? pureRotationRawTrajectory
      : await loadPureRotationTrack(initialTrajectoryKind);
    pureRotationTrajectoryKind = initialTrajectoryKind;
    updatePureRotationFovSource();
    seedPureRotationDraftPlacement();
    applyPureRotationPose();
  }

  function seedPureRotationDraftPlacement() {
    if (pureRotationHasPlacement) return null;
    if (pureRotationDraftPlacement || !pureRotationRawTrajectory) return pureRotationDraftPlacement;
    const video = document.querySelector("#sourceVideo");
    const rawPose = pureRotationPoseAtPts(pureRotationRawTrajectory, video?.currentTime || 0);
    const manual = window.cadsceneGetDefaultCameraPose?.() || window.cadsceneGetCurrentCameraPose?.();
    if (!rawPose?.rotation_local_from_camera || !manual) return null;
    pureRotationDraftPlacement = {
      segment_id: Number(rawPose.segment_id),
      anchor_pts_time_sec: Number(rawPose.pts_time_sec),
      anchor_local_rotation: rawPose.rotation_local_from_camera,
      manual_anchor_rotation: manualRotationMatrix(manual),
      camera_center_web: [Number(manual.x), Number(manual.y), Number(manual.z)],
      fov: Number(pureRotationDisplayFov || manual.fov),
    };
    return pureRotationDraftPlacement;
  }

  function updatePureRotationFovSource() {
    const label = document.querySelector("#pureRotationFovSource");
    if (!label) return;
    const copy = {
      saved_placement: "已保存的全局放置",
      unverified_candidate_intrinsics: "未经验证的候选内参换算",
      manual_preview: "当前未保存预览",
      default: "默认值",
    };
    label.textContent = `FOV ${pureRotationDisplayFov.toFixed(1)}° · 来源：${copy[pureRotationFovSource] || copy.default}`;
  }

  function setPureRotationWorldYawInputs(value) {
    const normalized = Number(value);
    const slider = document.querySelector("#pureRotationWorldYaw");
    const number = document.querySelector("#pureRotationWorldYawNumber");
    if (slider) slider.value = String(normalized);
    if (number) number.value = String(normalized);
  }

  function setPureRotationLocalDeltaInputs(value) {
    for (const key of ["yaw", "pitch", "roll"]) {
      const input = document.querySelector(`#pureRotationLocal${key[0].toUpperCase()}${key.slice(1)}`);
      if (input) input.value = String(Number(value[key]) || 0);
    }
  }

  function manualRotationMatrix(manual) {
    if (Array.isArray(manual?.rotation_cad_from_camera)) {
      return manual.rotation_cad_from_camera.map((row) => row.map(Number));
    }
    return window.CadscenePureRotationMath.viewerEulerToMatrix(manual);
  }

  function seedPureRotationCorrectionDraftFromManual(manual) {
    const active = window.pureRotationViewer?.pose;
    if (!active || !manual) return null;
    const rotation = manualRotationMatrix(manual);
    const center = [Number(manual.x), Number(manual.y), Number(manual.z)];
    pureRotationCorrectionDraftBase = {
      decoded_frame_index: Number(active.decoded_frame_index),
      pts_time_sec: Number(active.pts_time_sec),
      segment_id: Number(active.segment_id),
      rotation_cad_from_camera: rotation,
    };
    pureRotationCorrectionDraftDirty = false;
    pureRotationWorldYawDeg = 0;
    pureRotationLocalDelta = { yaw: 0, pitch: 0, roll: 0 };
    setPureRotationWorldYawInputs(0);
    setPureRotationLocalDeltaInputs(pureRotationLocalDelta);
    window.cadsceneApplyPureRotationPose?.({
      ...active,
      rotation_cad_from_camera: rotation,
      camera_center_web: center,
      display_fov: Number(manual.fov),
    });
    return pureRotationCorrectionDraftBase;
  }

  function refreshPureRotationCorrectionDraftBase() {
    const active = window.pureRotationViewer?.pose;
    if (!active || !window.CadscenePureRotationMath) return null;
    const sameFrame = pureRotationCorrectionDraftBase
      && Number(pureRotationCorrectionDraftBase.decoded_frame_index) === Number(active.decoded_frame_index)
      && Number(pureRotationCorrectionDraftBase.segment_id) === Number(active.segment_id);
    if (pureRotationCorrectionDraftDirty && sameFrame) return pureRotationCorrectionDraftBase;
    const rotation = active.rotation_cad_from_camera
      || window.CadscenePureRotationMath.localRotationToViewerMatrix(active.rotation_local_from_camera);
    if (!Array.isArray(rotation)) return null;
    pureRotationCorrectionDraftBase = {
      decoded_frame_index: Number(active.decoded_frame_index),
      pts_time_sec: Number(active.pts_time_sec),
      segment_id: Number(active.segment_id),
      rotation_cad_from_camera: rotation.map((row) => row.map(Number)),
    };
    pureRotationCorrectionDraftDirty = false;
    pureRotationWorldYawDeg = 0;
    pureRotationLocalDelta = { yaw: 0, pitch: 0, roll: 0 };
    setPureRotationWorldYawInputs(0);
    setPureRotationLocalDeltaInputs(pureRotationLocalDelta);
    return pureRotationCorrectionDraftBase;
  }

  function applyPureRotationCorrectionPreview() {
    const base = pureRotationCorrectionDraftBase || refreshPureRotationCorrectionDraftBase();
    if (!base) return;
    const localRotation = window.CadscenePureRotationMath.applyLocalCameraDelta(
      base.rotation_cad_from_camera,
      pureRotationLocalDelta,
    );
    const rotation = window.CadscenePureRotationMath.rotateAboutWorldUp(
      localRotation,
      pureRotationWorldYawDeg,
    );
    window.cadsceneApplyManualPureRotationMatrix?.(rotation);
  }

  async function previewPureRotationWorldYaw(value) {
    await setPureRotationEditMode("placement");
    const active = window.pureRotationViewer?.pose;
    const sameFrame = pureRotationCorrectionDraftBase
      && Number(pureRotationCorrectionDraftBase.decoded_frame_index) === Number(active?.decoded_frame_index)
      && Number(pureRotationCorrectionDraftBase.segment_id) === Number(active?.segment_id);
    if (!pureRotationCorrectionDraftDirty || !sameFrame) {
      seedPureRotationCorrectionDraftFromManual(window.cadsceneGetCurrentCameraPose?.());
    }
    pureRotationWorldYawDeg = Math.max(-180, Math.min(180, Number(value) || 0));
    pureRotationCorrectionDraftDirty = true;
    setPureRotationWorldYawInputs(pureRotationWorldYawDeg);
    applyPureRotationCorrectionPreview();
    capturePureRotationDraftPlacement();
    message.textContent = "当前位置与方向草稿已自动保留；可继续切换平移或旋转，满意后点击确认。";
  }

  async function previewPureRotationLocalDelta(key, value) {
    await setPureRotationEditMode("correction");
    pureRotationLocalDelta = {
      ...pureRotationLocalDelta,
      [key]: Math.max(-180, Math.min(180, Number(value) || 0)),
    };
    pureRotationCorrectionDraftDirty = true;
    setPureRotationLocalDeltaInputs(pureRotationLocalDelta);
    applyPureRotationCorrectionPreview();
  }

  function capturePureRotationDraftPlacement() {
    if (!isPureRotationWorkflow() || pureRotationEditMode !== "placement") return;
    const video = document.querySelector("#sourceVideo");
    const rawPose = pureRotationPoseAtPts(pureRotationRawTrajectory, video?.currentTime || 0);
    const manual = window.cadsceneGetCurrentCameraPose?.();
    if (!rawPose?.rotation_local_from_camera || !manual) return;
    pureRotationDraftPlacement = {
      segment_id: Number(rawPose.segment_id),
      anchor_pts_time_sec: Number(rawPose.pts_time_sec),
      anchor_local_rotation: rawPose.rotation_local_from_camera,
      manual_anchor_rotation: manualRotationMatrix(manual),
      camera_center_web: [Number(manual.x), Number(manual.y), Number(manual.z)],
      fov: Number(manual.fov),
    };
    pureRotationDisplayFov = pureRotationDraftPlacement.fov;
    pureRotationFovSource = "manual_preview";
    updatePureRotationFovSource();
  }

  function applyPureRotationPose() {
    const video = document.querySelector("#sourceVideo");
    let pose = pureRotationPoseAtPts(pureRotationTrajectory, video?.currentTime || 0);
    if (pureRotationDraftPlacement) {
      const rawPose = pureRotationPoseAtPts(pureRotationRawTrajectory, video?.currentTime || 0);
      if (rawPose && Number(rawPose.segment_id) === pureRotationDraftPlacement.segment_id) {
        pose = {
          ...rawPose,
          rotation_cad_from_camera: window.CadscenePureRotationMath.applyDraftPlacement(
            rawPose.rotation_local_from_camera,
            pureRotationDraftPlacement.anchor_local_rotation,
            pureRotationDraftPlacement.manual_anchor_rotation,
          ),
          camera_center_web: pureRotationDraftPlacement.camera_center_web,
        };
      }
    }
    window.pureRotationViewer = { trajectory: pureRotationTrajectory, pose };
    if (!pose) return;
    window.cadsceneApplyPureRotationPose?.({ ...pose, display_fov: pureRotationDisplayFov });
    if (pureRotationEditMode === "correction" && video?.paused) {
      refreshPureRotationCorrectionDraftBase();
    }
    if (focusPureRotationCameraOnce) {
      window.cadsceneSetPureRotationEditMode?.("placement");
      window.cadsceneFocusVirtualCamera?.();
      focusPureRotationCameraOnce = false;
    }
  }
  window.cadsceneRefreshPureRotationPose = applyPureRotationPose;

  const pureRotationVideo = document.querySelector("#sourceVideo");
  pureRotationVideo?.addEventListener("play", capturePureRotationDraftPlacement);
  pureRotationVideo?.addEventListener("timeupdate", applyPureRotationPose, { passive: true });
  pureRotationVideo?.addEventListener("loadedmetadata", applyPureRotationPose);
  pureRotationVideo?.addEventListener("seeked", applyPureRotationPose);
  pureRotationVideo?.addEventListener("pause", applyPureRotationPose);
  pureRotationVideo?.addEventListener("ended", applyPureRotationPose);
  window.addEventListener("cadsceneViewerReady", () => {
    seedPureRotationDraftPlacement();
    applyPureRotationPose();
  });
  window.addEventListener("cadsceneManualCameraChanged", () => {
    if (!isPureRotationWorkflow() || pureRotationEditMode !== "placement") return;
    pureRotationCorrectionDraftBase = null;
    pureRotationCorrectionDraftDirty = false;
    pureRotationWorldYawDeg = 0;
    pureRotationLocalDelta = { yaw: 0, pitch: 0, roll: 0 };
    setPureRotationWorldYawInputs(0);
    setPureRotationLocalDeltaInputs(pureRotationLocalDelta);
    capturePureRotationDraftPlacement();
    message.textContent = "当前位置与方向草稿已自动保留；可继续切换平移或旋转，满意后点击确认。";
  });

  function setPureRotationEditMode(mode) {
    const nextMode = mode === "correction" ? "correction" : "placement";
    const target = nextMode === "placement"
      ? (pureRotationHasPlacement ? "base" : "raw")
      : (pureRotationCorrections.length ? "corrected" : "base");
    if (pureRotationEditModeTransitioning && pureRotationEditMode === nextMode) {
      return pureRotationEditModeReady;
    }
    if (
      pureRotationEditMode === nextMode
      && pureRotationTrajectory
      && pureRotationTrajectoryKind === target
    ) {
      window.cadsceneSetPureRotationEditMode?.(nextMode);
      return pureRotationEditModeReady;
    }
    const preservedManual = nextMode === "correction"
      ? window.cadsceneGetCurrentCameraPose?.()
      : null;
    pureRotationEditMode = nextMode;
    pureRotationEditModeTransitioning = true;
    document.querySelector("#sourceVideo")?.pause();
    window.cadsceneSetPureRotationEditMode?.(pureRotationEditMode);
    if (pureRotationEditMode === "placement") {
      pureRotationEditModeReady = loadPureRotationTrack(target).then((trajectory) => {
        pureRotationTrajectory = trajectory;
        pureRotationTrajectoryKind = target;
        applyPureRotationPose();
        return trajectory;
      }).catch((error) => {
        message.textContent = `旋转轨迹不可用：${error.message}`;
        return null;
      }).finally(() => {
        pureRotationEditModeTransitioning = false;
      });
      window.cadsceneEnsureVirtualCameraNearCad?.();
      window.cadsceneFocusVirtualCamera?.();
      message.textContent = "全局放置：使用下方 X/Y/Z/Yaw/Pitch/Roll/FOV 和 3D Gizmo 调整固定相机。";
    } else {
      const target = pureRotationCorrections.length ? "corrected" : "base";
      pureRotationEditModeReady = loadPureRotationTrack(target).then((trajectory) => {
        pureRotationTrajectory = trajectory;
        pureRotationTrajectoryKind = target;
        applyPureRotationPose();
        if (preservedManual) seedPureRotationCorrectionDraftFromManual(preservedManual);
        return trajectory;
      }).catch((error) => {
        message.textContent = `旋转轨迹不可用：${error.message}`;
        return null;
      }).finally(() => {
        pureRotationEditModeTransitioning = false;
      });
      message.textContent = "姿态关键帧：位置和 FOV 已锁定，只调整 Yaw/Pitch/Roll。";
    }
    return pureRotationEditModeReady;
  }

  async function savePureRotationPlacement() {
    const active = window.pureRotationViewer?.pose;
    const manual = window.cadsceneGetCurrentCameraPose?.();
    if (!active || !manual) throw new Error("请先加载 OpenGV 原始旋转轨迹");
    const placement = {
      segment_id: Number(active.segment_id),
      anchor_decoded_frame_index: Number(active.decoded_frame_index),
      anchor_pts_time_sec: Number(active.pts_time_sec),
      camera_center_web: [Number(manual.x), Number(manual.y), Number(manual.z)],
      manual_rotation_cad_from_camera: manualRotationMatrix(manual),
      fov: Number(manual.fov),
    };
    await apiPost("/api/pure-rotation/placement", { dataset, runId, placement });
    pureRotationDisplayFov = placement.fov;
    pureRotationFovSource = "saved_placement";
    pureRotationHasPlacement = true;
    pureRotationSavedPlacement = placement;
    pureRotationDraftPlacement = null;
    pureRotationCorrectionDraftBase = null;
    pureRotationCorrectionDraftDirty = false;
    updatePureRotationFovSource();
    pureRotationTrajectory = await loadPureRotationTrack("base");
    pureRotationTrajectoryKind = "base";
    applyPureRotationPose();
    refreshPureRotationCorrectionDraftBase();
    window.cadsceneClearUnsavedCameraDraft?.();
    message.textContent = "固定相机放置已保存；播放时位置保持不变。";
  }

  async function restorePureRotationPlacement() {
    pureRotationDraftPlacement = null;
    pureRotationCorrectionDraftBase = null;
    pureRotationCorrectionDraftDirty = false;
    window.cadsceneClearUnsavedCameraDraft?.();
    if (!pureRotationSavedPlacement) {
      seedPureRotationDraftPlacement();
      pureRotationFovSource = "unverified_candidate_intrinsics";
      updatePureRotationFovSource();
      applyPureRotationPose();
      message.textContent = "已撤销未确认的调整，恢复进入调试时的相机设置。";
      return;
    }
    pureRotationDisplayFov = Number(pureRotationSavedPlacement.fov);
    pureRotationFovSource = "saved_placement";
    updatePureRotationFovSource();
    pureRotationTrajectory = await loadPureRotationTrack("base");
    pureRotationTrajectoryKind = "base";
    applyPureRotationPose();
    message.textContent = "已恢复上次保存的固定相机放置。";
  }

  async function refreshPureRotationFittedPreview() {
    if (!pureRotationHasPlacement) throw new Error("请先保存全局固定相机放置");
    const target = "base";
    pureRotationTrajectory = await loadPureRotationTrack(target);
    pureRotationTrajectoryKind = target;
    pureRotationDraftPlacement = null;
    applyPureRotationPose();
    refreshPureRotationCorrectionDraftBase();
    return pureRotationTrajectory;
  }

  async function addPureRotationCorrection() {
    const active = window.pureRotationViewer?.pose;
    const manual = window.cadsceneGetCurrentCameraPose?.();
    if (!active || !manual) throw new Error("当前帧没有可修正姿态");
    const correction = { schema_version: 1, decoded_frame_index: Number(active.decoded_frame_index), pts_time_sec: Number(active.pts_time_sec), segment_id: Number(active.segment_id), base_rotation_cad_from_camera: active.rotation_cad_from_camera, manual_rotation_cad_from_camera: manualRotationMatrix(manual), source: "manual_rotation_correction", orientation_confirmed: true, note: "" };
    pureRotationCorrections = pureRotationCorrections.filter((item) => item.decoded_frame_index !== correction.decoded_frame_index || item.segment_id !== correction.segment_id);
    pureRotationCorrections.push(correction);
    await apiPost("/api/pure-rotation/corrections", { dataset, runId, corrections: pureRotationCorrections });
    pureRotationCorrectionDraftDirty = false;
    window.cadsceneClearUnsavedCameraDraft?.();
    await refreshPureRotationFittedPreview();
    message.textContent = `已保存姿态关键帧 ${correction.decoded_frame_index}。`;
  }

  async function deletePureRotationCorrection() {
    const active = window.pureRotationViewer?.pose;
    if (!active) return;
    pureRotationCorrections = pureRotationCorrections.filter((item) => item.decoded_frame_index !== Number(active.decoded_frame_index) || item.segment_id !== Number(active.segment_id));
    await apiPost("/api/pure-rotation/corrections", { dataset, runId, corrections: pureRotationCorrections });
    pureRotationCorrectionDraftDirty = false;
    window.cadsceneClearUnsavedCameraDraft?.();
    await refreshPureRotationFittedPreview();
    message.textContent = `已删除姿态关键帧 ${active.decoded_frame_index}。`;
  }

  async function finishPureRotationCalibration() {
    await ensureProjectWorkbenchSession();
    if (pureRotationEditMode === "placement" || !pureRotationHasPlacement) {
      await savePureRotationPlacement();
    }
    if (!pureRotationHasPlacement) throw new Error("请先保存全局固定相机放置");
    await refreshPureRotationFittedPreview();
    await finalizeProjectWorkbenchSave(
      { ok: true, kind: "pure_rotation_calibration" },
      { navigate: false },
    );
    setWorkflowStage("render");
    message.textContent = "当前相机设置和关键帧拟合轨迹已保存，可以预览或开始渲染。";
  }

  async function previewPureRotationFittedTrack() {
    await refreshPureRotationFittedPreview();
    const video = document.querySelector("#sourceVideo");
    if (video) {
      video.currentTime = 0;
      await video.play().catch(() => {});
    }
    message.textContent = "正在预览关键帧拟合后的固定中心旋转轨迹。";
  }

  function jumpPureRotationCorrection(direction) {
    const currentPts = Number(document.querySelector("#sourceVideo")?.currentTime || 0);
    const ordered = pureRotationCorrections
      .slice()
      .sort((a, b) => Number(a.pts_time_sec) - Number(b.pts_time_sec));
    const target = direction < 0
      ? ordered.filter((item) => Number(item.pts_time_sec) < currentPts - 1e-6).at(-1)
      : ordered.find((item) => Number(item.pts_time_sec) > currentPts + 1e-6);
    if (!target) {
      message.textContent = direction < 0 ? "没有上一个姿态关键帧。" : "没有下一个姿态关键帧。";
      return;
    }
    const video = document.querySelector("#sourceVideo");
    if (video) video.currentTime = Number(target.pts_time_sec);
  }

  function undoPureRotationDraft() {
    pureRotationDraftPlacement = null;
    pureRotationCorrectionDraftDirty = false;
    applyPureRotationPose();
    refreshPureRotationCorrectionDraftBase();
    message.textContent = "已撤销当前未保存的姿态调整。";
  }

  function isInterfaceOnlyTrajectoryWorkflow() {
    return trajectoryWorkflow?.implementation_status === "interface_only";
  }

  function trajectoryWorkflowActionsAreBlocked() {
    return !trajectoryWorkflowLoaded || isInterfaceOnlyTrajectoryWorkflow();
  }

  function refreshSupplementalWorkflowActionAvailability(isRunning = false) {
    const blocked = Boolean(isRunning) || trajectoryWorkflowActionsAreBlocked();
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = blocked;
      if (!blocked) button.removeAttribute("title");
    });
    const generate = document.querySelector("#workflowGenerateKeyframes");
    const finishQuality = document.querySelector("#workflowFinishQuality");
    const returnKeyframes = document.querySelector("#workflowReturnKeyframes");
    if (generate) generate.disabled = blocked;
    if (finishQuality) finishQuality.disabled = blocked;
    if (returnKeyframes) returnKeyframes.disabled = blocked;
    if (!blocked) updateKeyframePlanUi();
  }

  function blockTrajectoryWorkflowActionsUntilResolved() {
    if (!trajectoryWorkflowActionsAreBlocked()) return;
    const explanation = isInterfaceOnlyTrajectoryWorkflow()
      ? "当前 SRT 轨迹功能待启用，不能启动处理流程。"
      : "正在确认轨迹工作模式，请稍候。";
    document.querySelectorAll("[data-job-action], #workflowGenerateKeyframes, #workflowContinueKeyframes, #workflowFinishKeyframes, #workflowFinishQuality, #workflowReturnKeyframes").forEach((button) => {
      button.disabled = true;
      button.title = explanation;
    });
  }

  function ensureManifestViewerPaths(manifest) {
    if (!dataset || !runId) return false;
    const videoUrl = String(manifest?.video?.url || "");
    const cadUrl = String(manifest?.cad?.url || "");
    if (!videoUrl && !cadUrl) return false;

    const target = new URL(window.location.href);
    let changed = false;
    if (!target.searchParams.get("video") && videoUrl) {
      target.searchParams.set("video", videoUrl);
      changed = true;
    }
    if (!target.searchParams.get("cad") && cadUrl) {
      target.searchParams.set("cad", cadUrl);
      changed = true;
    }
    const defaults = manifest?.defaults || {};
    if (!target.searchParams.get("cadScale") && Number.isFinite(Number(defaults.cad_scale))) {
      target.searchParams.set("cadScale", String(defaults.cad_scale));
      changed = true;
    }
    if (!target.searchParams.get("originXY") && Array.isArray(defaults.origin_xy) && defaults.origin_xy.length >= 2) {
      target.searchParams.set("originXY", defaults.origin_xy.slice(0, 2).join(","));
      changed = true;
    }
    if (changed) window.location.replace(target.toString());
    return changed;
  }

  async function loadManifestBackedTrajectoryWorkflow() {
    if (!dataset) return;
    let projectWorkbenchWorkflowMode = null;
    if (projectWorkbenchToken) {
      try {
        await ensureProjectWorkbenchSession();
        projectWorkbenchWorkflowMode = projectWorkbenchSession.workflow === "pure_rotation"
          ? "pure_rotation"
          : "sfm_only";
      } catch (error) {
        trajectoryWorkflowLoaded = false;
        message.textContent = `项目会话不可用：${error.message}`;
        return;
      }
    }
    try {
      const response = await fetch(`/api/workflow/dataset-manifest?dataset=${encodeURIComponent(dataset)}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (ensureManifestViewerPaths(payload?.manifest)) return;
      const manifestWorkflow = payload?.manifest?.workflow || {};
      trajectoryWorkflow = projectWorkbenchWorkflowMode
        ? {
            ...manifestWorkflow,
            trajectory_mode: projectWorkbenchWorkflowMode,
            implementation_status: "ready",
          }
        : (payload?.manifest?.workflow || { trajectory_mode: "sfm_only", implementation_status: "ready" });
    } catch (error) {
      if (projectWorkbenchWorkflowMode) {
        trajectoryWorkflow = {
          trajectory_mode: projectWorkbenchWorkflowMode,
          implementation_status: "ready",
        };
      } else {
      // Preserve legacy dataset/run URLs when a manifest is unavailable.
        trajectoryWorkflow = { trajectory_mode: "sfm_only", implementation_status: "ready" };
      }
    }
    trajectoryWorkflowLoaded = true;
    renderTrajectoryWorkflow(trajectoryWorkflow);
    pollJobStatus();
    loadKeyframePlan();
  }

  let uploadRunId = runId || `import_${uploadTimestamp()}`;
  let activeUploadDataset = dataset;

  function runPath(relative) {
    const resolvedDataset = dataset || activeUploadDataset;
    const resolvedRunId = runId || uploadRunId;
    if (!resolvedDataset || !resolvedRunId) return "";
    return `/runs/${encodeURIComponent(resolvedDataset)}/${encodeURIComponent(resolvedRunId)}/${relative}`;
  }

  function postAlignmentStageKey() {
    return `cadscenePostAlignmentStage:${dataset}:${runId}`;
  }

  function restoredWorkflowStageKey() {
    return `cadsceneRestoredWorkflowStage:${dataset}:${runId}`;
  }

  function nextStageAfterSuccess(stage) {
    if (isPureRotationWorkflow()) {
      const pureNext = {
        upload: "sfm",
        sfm: "keyframes",
        pure_rotation: "keyframes",
        keyframes: "render",
        render: "render",
      };
      return pureNext[stage] || "upload";
    }
    const next = {
      upload: "sfm",
      sfm: "keyframes",
      keyframes: "quality",
      alignment: "keyframes",
      quality: "render",
      render: "render",
    };
    return next[stage] || "upload";
  }

  async function resourceExists(path) {
    if (!path) return false;
    try {
      const response = await fetch(path, { method: "HEAD", cache: "no-store" });
      return response.ok;
    } catch (error) {
      return false;
    }
  }

  async function updateSfmSummary() {
    const status = document.querySelector("#workflowSfmStatus");
    const path = runPath("02_sfm/sfm_stats.json");
    if (!status || !path) return null;
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) return null;
      const stats = await response.json();
      const inferredSuitableFor3d =
        Number(stats.registered_count || 0) >= 3 &&
        Number(stats.point_count || 0) >= Math.max(100, Number(stats.registered_count || 0));
      const suitableFor3d = typeof stats.suitable_for_3d === "boolean"
        ? stats.suitable_for_3d
        : inferredSuitableFor3d;
      if (!suitableFor3d) {
        const hasExplicitParallaxDiagnosis =
          typeof stats.low_parallax_or_rotation_suspected === "boolean";
        const shouldReshoot = hasExplicitParallaxDiagnosis
          ? stats.low_parallax_or_rotation_suspected
          : stats.next_step_recommendation === "reshoot_with_translation";
        status.textContent = shouldReshoot
          ? `SfM 已完成，但当前结果不适合三维重建（注册 ${stats.registered_count}/${stats.extracted_frame_count} 帧，` +
            `${stats.point_count} 个点）。可能存在低视差、纯旋转或纹理不足；请重新拍摄并让无人机产生明显平移，` +
            `例如沿路线飞行或围绕场地移动。该点云不会用于 CAD 配准和道路诊断。`
          : `SfM 注册失败（注册 ${stats.registered_count}/${stats.extracted_frame_count} 帧，${stats.point_count} 个点）。` +
            `点云数量不能替代完整相机轨迹，也不能据此判断视频缺少平移；请使用前向飞行参数重新运行 SfM。`;
        return false;
      }
      status.textContent =
        `SfM 已完成：注册 ${stats.registered_count}/${stats.extracted_frame_count} 帧，` +
        `${stats.point_count} 个点；等待关键帧对齐后叠加 CAD。`;
      return true;
    } catch (error) {
      // 统计文件可选，不阻塞工作流。
      return null;
    }
  }

  async function detectWorkflowStageFromArtifacts() {
    if (!dataset || !runId) return "upload";
    if (isPureRotationWorkflow()) {
      const rawReady = await resourceExists(runPath("02_pure_rotation/camera_rotation_raw.json"));
      if (rawReady) {
        updatePureRotationRecoveryActions({ ready: true });
        return "keyframes";
      }
      const viewerPaths = window.resolveViewerPaths ? window.resolveViewerPaths() : {};
      const videoReady = await resourceExists(viewerPaths.video);
      const cadReady = await resourceExists(viewerPaths.cad);
      return videoReady && cadReady ? "sfm" : "upload";
    }
    const renderReady = await resourceExists(runPath("08_render/sfm_align_overlay.mp4"));
    if (renderReady) return "render";
    const qualityReady = await resourceExists(runPath("04_quality/quality_timeline.csv"));
    if (qualityReady) return "quality";
    const alignmentReady = await resourceExists(runPath("03_alignment/alignment.json"));
    if (alignmentReady) return "keyframes";
    const trajectoryReady = await resourceExists(runPath("02_sfm/camera_trajectory.json"));
    const pointsReady = await resourceExists(runPath("02_sfm/sparse_points.ply"));
    if (trajectoryReady && pointsReady) {
      const sfmSuitable = await updateSfmSummary();
      if (sfmSuitable === false) return "sfm";
      maybeAutoApplySfmCameraInit();
      return "keyframes";
    }
    const viewerPaths = window.resolveViewerPaths ? window.resolveViewerPaths() : {};
    const videoReady = await resourceExists(viewerPaths.video);
    const cadReady = await resourceExists(viewerPaths.cad);
    return videoReady && cadReady ? "sfm" : "upload";
  }

  function updateWorkflowStepActive(stage) {
    document.querySelectorAll("#workflowSteps li").forEach((node) => {
      node.classList.toggle("is-active", node.dataset.stage === stage);
    });
  }

  function renderWorkflowPanel(stage) {
    document.querySelectorAll(".workflow-stage-panel").forEach((node) => {
      node.classList.toggle("active", node.dataset.stage === stage);
    });
    const stageStatus = latestJobStatus?.stages?.[stage];
    const operation = latestJobStatus?.operation || stageStatus?.operation || "";
    const taskTitle = operation === "pure_rotation"
      ? "OpenGV 旋转轨迹恢复"
      : (latestJobStatus?.status === "running" && operation === "alignment" ? "路线拟合" : stageTitles[stage]);
    title.textContent = `当前任务：${taskTitle}`;
    stateLabel.textContent = stageStatus?.status || "待启动";
    progress.value = Number(stageStatus?.progress || 0);
    message.textContent = stageStatus?.error || stageStatus?.message || "等待任务";
    if (stage === "render" && projectWorkbenchRenderJobId) {
      progress.value = projectRenderVisibleProgress;
      stateLabel.textContent = projectRenderStatusCopy(
        projectWorkbenchRenderStatus,
        projectWorkbenchRenderStatus,
      );
      message.textContent = projectWorkbenchRenderStatus === "validating"
        ? "正在封装并验证渲染结果"
        : projectRenderStatusCopy(projectWorkbenchRenderStatus, projectWorkbenchRenderStatus);
      return;
    }
    if (stage === "render" && operation === "render" && latestJobStatus?.status === "running") {
      if (renderProgressState) progress.value = renderProgressState.completed / renderProgressState.total;
      message.textContent = "正在渲染";
    } else if (stage === "render" && latestJobStatus?.status !== "running") {
      renderProgressState = null;
    }
    if (stage === "keyframes" && isPureRotationWorkflow()) {
      message.textContent = "悬停旋转调试：移动固定相机并调整姿态；播放检查旋转效果，完成后直接进入渲染导出。";
    } else if (
      stage === "keyframes" &&
      !isPureRotationWorkflow() &&
      !stageStatus?.message &&
      latestJobStatus?.stages?.sfm?.status === "success"
    ) {
      message.textContent = alignmentArtifactReady
        ? "路线已拟合。继续补关键帧，完成后再次「路线拟合」刷新点云/轨迹。"
        : "SfM 已完成，已完成首帧标定后点击「路线拟合」，之后才会显示 CAD-aligned 点云。";
    }
    if (stage === "sfm" && !isPureRotationWorkflow()) {
      const sfmStatus = document.querySelector("#workflowSfmStatus");
      if (sfmStatus && stageStatus?.status === "running") sfmStatus.textContent = stageStatus.message || "SfM 正在运行";
      if (sfmStatus && stageStatus?.status === "failed") sfmStatus.textContent = stageStatus.error || "SfM 运行失败";
      if (sfmStatus && stageStatus?.status === "success") updateSfmSummary();
    }
  }

  function setWorkflowStage(stage) {
    const requested = isPureRotationWorkflow() && stage === "quality" ? "render" : stage;
    selectedWorkflowStage = stageOrder.includes(requested) ? requested : "upload";
    const annotationPanel = document.querySelector("#annotationPanel");
    const cameraSettings = document.querySelector("#cameraSettingsDetails");
    if (annotationPanel) annotationPanel.hidden = selectedWorkflowStage !== "render";
    if (cameraSettings) cameraSettings.open = selectedWorkflowStage !== "render";
    const calibrationPanel = document.querySelector("#pureRotationCalibrationPanel");
    if (calibrationPanel) {
      calibrationPanel.hidden = !(isPureRotationWorkflow() && selectedWorkflowStage === "keyframes");
    }
    updateWorkflowStepActive(selectedWorkflowStage);
    renderWorkflowPanel(selectedWorkflowStage);
    if (isPureRotationWorkflow() && selectedWorkflowStage === "keyframes") {
      setPureRotationEditMode("placement");
    }
    if (
      selectedWorkflowStage === "render"
      && projectWorkbenchToken
      && projectWorkbenchSession?.clip_id
    ) {
      refreshRenderOutputState().catch((error) => {
        console.warn("[cadscene workflow] 已发布渲染结果恢复失败", error);
      });
    }
    scheduleProjectWorkbenchResume();
  }

  async function refreshQualityArtifactsAfterSuccess(payload) {
    const operation = payload.operation || payload.current_stage;
    if (payload.status !== "success" || operation !== "quality") return;

    const completedAt = payload.updated_at || payload.ended_at || payload.message || "success";
    const refreshKey = `cadsceneQualityArtifactsLoaded:${dataset}:${runId}:${completedAt}`;
    if (sessionStorage.getItem(refreshKey)) return;

    sessionStorage.setItem(refreshKey, "loading");
    try {
      if (typeof window.cadsceneReloadQualityArtifacts === "function") {
        await window.cadsceneReloadQualityArtifacts();
      }
      await loadWorkflowSuggestions();
      message.textContent = "质量时间线和补帧建议已重新加载";
    } catch (error) {
      console.warn("[cadscene workflow] 质量产物刷新失败", error);
      message.textContent = "质量任务已完成，但质量产物刷新失败，请刷新页面";
    } finally {
      sessionStorage.setItem(refreshKey, "1");
    }
  }

  async function renderStatus(payload) {
    latestJobStatus = payload;
    const stages = payload.stages || {};
    document.querySelectorAll("#workflowSteps li").forEach((node) => {
      node.classList.toggle("is-success", stages[node.dataset.stage]?.status === "success");
    });
    if (!selectedWorkflowStage) {
      const backendStage = (payload.operation || payload.current_stage) === "pure_rotation"
        ? "sfm"
        : (payload.current_stage || "upload");
      const detectedStage = latestJobStatus?.status !== "running"
        ? await detectWorkflowStageFromArtifacts()
        : "";
      setWorkflowStage(detectedStage || (payload.status === "success" ? nextStageAfterSuccess(backendStage) : backendStage));
    } else {
      renderWorkflowPanel(selectedWorkflowStage);
    }
    // SfM 重建成功后，自动读取 SfM 的 FOV 作为虚拟相机初值，无需用户手动触发。
    // applySfmCameraInitializationOnce 内部用 sessionStorage 去重，重复调用安全。
    if (stages.sfm?.status === "success" && !isPureRotationWorkflow()) {
      const sfmSuitable = await updateSfmSummary();
      if (sfmSuitable !== false) maybeAutoApplySfmCameraInit();
    }
    const isRunning = payload.status === "running";
    const operation = payload.operation || payload.current_stage;
    if (isPureRotationWorkflow() && operation === "pure_rotation") {
      updatePureRotationRecoveryActions({
        ready: payload.status === "success",
        running: isRunning,
      });
      const completionKey = String(stages.sfm?.updated_at || payload.updated_at || "success");
      if (
        payload.status === "success"
        && pureRotationHandledCompletion !== completionKey
      ) {
        pureRotationHandledCompletion = completionKey;
        try {
          await initializePureRotationViewer();
          sessionStorage.setItem(restoredWorkflowStageKey(), "keyframes");
          if (!selectedWorkflowStage || selectedWorkflowStage === "sfm") {
            setWorkflowStage("keyframes");
          }
        } catch (error) {
          pureRotationHandledCompletion = null;
          throw error;
        }
      }
    }
    await refreshQualityArtifactsAfterSuccess(payload);
    if (payload.status === "success" && operation === "render") {
      const renderReady = await refreshRenderOutputState();
      if (renderReady) {
        setWorkflowStage("render");
        message.textContent = "渲染视频已生成，可预览或下载。";
      }
    }
    runningStage = isRunning ? operation : null;
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = isRunning || trajectoryWorkflowActionsAreBlocked();
    });
    refreshSupplementalWorkflowActionAvailability(isRunning);
    const cancel = document.querySelector("#workflowCancel");
    if (cancel) cancel.hidden = !isRunning;
    const reloadKey = `cadsceneJobReload:${dataset}:${runId}`;
    const pendingReload = sessionStorage.getItem(reloadKey);
    if (pendingReload && payload.status === "success" && (payload.operation || payload.current_stage) === pendingReload) {
      sessionStorage.removeItem(reloadKey);
      if (pendingReload === "alignment") {
        const postAlignmentStage = sessionStorage.getItem(postAlignmentStageKey());
        if (stageOrder.includes(postAlignmentStage)) {
          sessionStorage.setItem(restoredWorkflowStageKey(), postAlignmentStage);
          setWorkflowStage(postAlignmentStage);
        }
        sessionStorage.removeItem(postAlignmentStageKey());
      }
      await persistProjectWorkbenchResumeNow();
      projectWorkbenchInternalNavigation = true;
      window.location.reload();
    } else if (pendingReload && ["failed", "cancelled"].includes(payload.status)) {
      sessionStorage.removeItem(reloadKey);
      sessionStorage.removeItem(postAlignmentStageKey());
    }
  }

  function maybeAutoApplySfmCameraInit() {
    if (!trajectoryWorkflowLoaded) return;
    if (!viewerReadyForSfmCameraInit || isPureRotationWorkflow()) return;
    if (projectWorkbenchTrajectoryIsPending()) {
      message.textContent = "片段视频和项目 CAD 已就绪，请点击开始 SfM 重建";
      return;
    }
    if (typeof window.cadsceneApplyCameraParameters !== "function") return;
    applySfmCameraInitializationOnce().catch((error) => {
      message.textContent = `SfM 相机参数初值不可用：${error.message}`;
    });
  }

  async function refreshAlignmentArtifactState() {
    if (!dataset || !runId) return;
    const ready = await resourceExists(runPath("03_alignment/alignment.json"));
    if (ready !== alignmentArtifactReady) {
      alignmentArtifactReady = ready;
      if (selectedWorkflowStage) renderWorkflowPanel(selectedWorkflowStage);
    }
    // 点云展示约束：CAD-aligned 点云要等路线拟合之后才可靠。
    // alignment 之前 viewer 无法加载 sfm_viewer_scene.json，这里给出明确文字提示。
    const sfmInfo = document.querySelector("#sfmInfo");
    if (sfmInfo && !ready && latestJobStatus?.stages?.sfm?.status === "success") {
      sfmInfo.textContent = "已有 SfM 点云，等待路线拟合后显示到 CAD 场景。";
    }
  }

  async function pollJobStatus() {
    if (projectWorkbenchTrajectoryOwnsStatus()) return;
    if (projectWorkbenchToken && !projectWorkbenchSession) {
      stateLabel.textContent = projectWorkbenchBootstrapFailed ? "会话不可用" : "正在载入项目会话";
      return;
    }
    if (projectWorkbenchTrajectoryIsPending()) {
      stateLabel.textContent = "待启动";
      if (!selectedWorkflowStage) setWorkflowStage("sfm");
      message.textContent = isPureRotationWorkflow()
        ? "片段视频和项目 CAD 已就绪，请点击开始旋转轨迹恢复"
        : "片段视频和项目 CAD 已就绪，请点击开始 SfM 重建";
      return;
    }
    const path = runPath("job_status.json");
    if (!path) {
      stateLabel.textContent = "未指定 dataset/runId";
      return;
    }
    try {
      const response = await fetch(`${path}?t=${Date.now()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      await renderStatus(await response.json());
      refreshAlignmentArtifactState();
    } catch (error) {
      stateLabel.textContent = "等待 job_status";
      if (!selectedWorkflowStage) {
        const detected = await detectWorkflowStageFromArtifacts();
        if (!selectedWorkflowStage) setWorkflowStage(detected);
      }
    }
  }

  function stageOptions() {
    const options = {};
    if (params.get("video")) options.video = params.get("video");
    if (params.get("cad")) options.cad = params.get("cad");
    if (params.get("cadScale")) options.cad_scale = Number(params.get("cadScale"));
    const origin = params.get("originXY");
    if (origin) options.origin_xy = origin.split(",").map(Number);
    return options;
  }

  async function apiPost(path, payload) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function uploadControlValue(selector, fallback) {
    const value = document.querySelector(selector)?.value;
    return value === undefined || value === "" ? fallback : value;
  }

  function deriveDatasetFromVideo(file) {
    if (dataset || activeUploadDataset) return activeUploadDataset || dataset;
    const basename = String(file?.name || "video").replace(/\.[^.]+$/, "");
    const normalized = basename
      .normalize("NFKD")
      .replace(/[\u0300-\u036f]/g, "")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "video";
    activeUploadDataset = `${normalized}_${uploadTimestamp()}`;
    uploadRunId = `import_${uploadTimestamp()}`;
    const datasetInput = document.querySelector("#workflowDatasetName");
    if (datasetInput) datasetInput.value = activeUploadDataset;
    return activeUploadDataset;
  }

  function setUploadUi(text, value = null) {
    const status = document.querySelector("#workflowUploadStatus");
    const uploadProgress = document.querySelector("#workflowUploadProgress");
    if (status) status.textContent = text;
    if (uploadProgress && value !== null) uploadProgress.value = Math.max(0, Math.min(1, Number(value)));
  }

  async function ensureUploadDataset() {
    const requestedDataset = String(uploadControlValue("#workflowDatasetName", activeUploadDataset)).trim();
    if (!requestedDataset) throw new Error("请先填写 dataset 名称");
    const cadScale = Number(uploadControlValue("#workflowCadScale", 0.06));
    const originX = Number(uploadControlValue("#workflowOriginX", 567747.5756295));
    const originY = Number(uploadControlValue("#workflowOriginY", 3330464.2234675));
    const hoveringDeclared = Boolean(document.querySelector("#workflowHoveringDeclared")?.checked);
    if (!(cadScale > 0) || !Number.isFinite(originX) || !Number.isFinite(originY)) {
      throw new Error("CAD scale / origin XY 参数无效");
    }
    const result = await apiPost("/api/workflow/create-dataset", {
      dataset: requestedDataset,
      runId: uploadRunId,
      cadScale,
      originX,
      originY,
      hoveringDeclared,
    });
    activeUploadDataset = result.manifest.dataset;
    const input = document.querySelector("#workflowDatasetName");
    if (input) input.value = activeUploadDataset;
    return result.manifest;
  }

  function uploadFile(path, file) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const form = new FormData();
      form.append("file", file, file.name);
      xhr.open("POST", path);
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) setUploadUi(`正在上传 ${file.name}`, event.loaded / event.total);
      });
      xhr.addEventListener("load", () => {
        const result = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300) resolve(result);
        else reject(new Error(result.error || `HTTP ${xhr.status}`));
      });
      xhr.addEventListener("error", () => reject(new Error("上传连接失败")));
      xhr.send(form);
    });
  }

  function applyImportedManifest(manifest) {
    const videoReady = Boolean(manifest?.video?.url);
    const cadReady = manifest?.cad?.status === "ready";
    const cadStatus = document.querySelector("#workflowCadStatus");
    if (cadStatus) {
      cadStatus.textContent = cadReady
        ? `CAD 已准备：${manifest.cad.original_name}`
        : manifest?.cad?.status === "raw_saved"
          ? "DWG 已保存，但缺少 DWG→DXF 转换工具；请上传 DXF 或安装转换器。"
          : manifest?.cad?.status === "failed"
            ? `CAD 解析失败：${manifest.cad.error || "请检查文件"}`
            : "等待 CAD 解析";
    }
    if (!videoReady || !cadReady) {
      setUploadUi(`数据尚未完整：${videoReady ? "视频已就绪" : "等待视频"}，${cadReady ? "CAD 已就绪" : "等待 CAD"}`, 0.5);
      return;
    }
    setUploadUi("视频和 CAD 已导入，正在打开 SfM 阶段", 1);
    const nextUrl = new URL(window.location.href);
    nextUrl.searchParams.set("dataset", manifest.dataset);
    nextUrl.searchParams.set("runId", uploadRunId);
    nextUrl.searchParams.set("video", manifest.video.url);
    nextUrl.searchParams.set("cad", manifest.cad.url);
    nextUrl.searchParams.set("cadScale", String(manifest.defaults.cad_scale));
    nextUrl.searchParams.set("originXY", manifest.defaults.origin_xy.join(","));
    window.setTimeout(() => window.location.assign(nextUrl.toString()), 500);
  }

  async function importSelectedFile(kind, file) {
    if (!file) return;
    if (kind === "video") deriveDatasetFromVideo(file);
    if (!activeUploadDataset && !dataset) {
      throw new Error("请先上传视频，系统会自动创建 dataset 和 runId");
    }
    setUploadUi(`准备上传 ${file.name}`, 0);
    await ensureUploadDataset();
    const route = kind === "video" ? "/api/workflow/upload-video" : "/api/workflow/upload-cad";
    const query = `dataset=${encodeURIComponent(activeUploadDataset)}&runId=${encodeURIComponent(uploadRunId)}`;
    const result = await uploadFile(`${route}?${query}`, file);
    applyImportedManifest(result.manifest);
  }

  async function loadDatasetManifestForUpload() {
    const input = document.querySelector("#workflowDatasetName");
    if (input && !input.value) input.value = dataset;
    if (!dataset) return;
    try {
      const response = await fetch(`/api/workflow/dataset-manifest?dataset=${encodeURIComponent(dataset)}`, { cache: "no-store" });
      if (!response.ok) return;
      const manifest = (await response.json()).manifest;
      document.querySelector("#workflowCadScale").value = manifest.defaults.cad_scale;
      document.querySelector("#workflowOriginX").value = manifest.defaults.origin_xy[0];
      document.querySelector("#workflowOriginY").value = manifest.defaults.origin_xy[1];
      const videoReady = Boolean(manifest?.video?.url);
      const cadReady = manifest?.cad?.status === "ready";
      setUploadUi(`${videoReady ? "视频已就绪" : "等待视频"}，${cadReady ? "CAD 已就绪" : "等待 CAD"}`, manifest.status === "ready" ? 1 : 0.5);
    } catch (error) {
      setUploadUi(`读取 dataset manifest 失败：${error.message}`, 0);
    }
  }

  async function runStage(stage) {
    if (!trajectoryWorkflowLoaded) {
      throw new Error("正在确认轨迹工作模式，请稍候。");
    }
    if (isInterfaceOnlyTrajectoryWorkflow()) {
      throw new Error("轨迹功能尚未启用，当前模式不能启动处理流程。");
    }
    if (!dataset || !runId) {
      throw new Error("请先通过 URL 指定 dataset 和 runId。");
    }
    if (projectWorkbenchToken && stage === "sfm") {
      await ensureProjectWorkbenchSession();
      return runProjectWorkbenchTrajectory();
    }
    const result = await apiPost("/api/workflow/run-stage", {
      dataset,
      runId,
      stage,
      options: stageOptions(),
    });
    runningStage = stage;
    sessionStorage.setItem(`cadsceneJobReload:${dataset}:${runId}`, stage);
    message.textContent = "任务已启动";
    document.querySelector("#workflowCancel").hidden = false;
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = true;
    });
    pollJobLog();
    return result;
  }

  async function runPureRotationStage({ force = false } = {}) {
    if (!isPureRotationWorkflow()) {
      throw new Error("当前 dataset 未选择悬停旋转模式。");
    }
    if (!dataset || !runId) {
      throw new Error("请先通过 URL 指定 dataset 和 runId。");
    }
    if (projectWorkbenchToken) {
      await ensureProjectWorkbenchSession();
      return runProjectWorkbenchTrajectory();
    }
    if (!force && await resourceExists(runPath("02_pure_rotation/camera_rotation_raw.json"))) {
      await initializePureRotationViewer();
      updatePureRotationRecoveryActions({ ready: true });
      sessionStorage.setItem(restoredWorkflowStageKey(), "keyframes");
      setWorkflowStage("keyframes");
      message.textContent = "旋转轨迹恢复已完成，已进入调试环节。";
      return { ok: true, reused: true };
    }
    const result = await apiPost("/api/pure-rotation/run", {
      dataset,
      runId,
      options: { ...stageOptions(), force },
    });
    runningStage = "pure_rotation";
    sessionStorage.setItem(`cadsceneJobReload:${dataset}:${runId}`, "pure_rotation");
    message.textContent = "OpenGV 正在恢复逐帧相对旋转；不会估计平移。";
    document.querySelector("#workflowCancel").hidden = false;
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = true;
    });
    pollJobLog();
    return result;
  }

  async function saveCurrentCameraTrack(cameraTrack = null) {
    if (typeof window.cadsceneGetCameraTrack !== "function") {
      throw new Error("当前相机轨迹尚未初始化");
    }
    const result = await apiPost("/api/workflow/save-camera-track", {
      dataset,
      runId,
      cameraTrack: cameraTrack || window.cadsceneGetCameraTrack(),
    });
    await loadKeyframePlan();
    return result;
  }

  async function recoverStaleProjectWorkbenchSession() {
    if (!projectWorkbenchProjectId || !runId) {
      throw new Error("旧工作台会话无法确定所属项目或片段");
    }
    const snapshotResponse = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
      { cache: "no-store" },
    );
    const snapshot = await snapshotResponse.json().catch(() => ({}));
    if (!snapshotResponse.ok) {
      throw new Error(snapshot.message || snapshot.error || `HTTP ${snapshotResponse.status}`);
    }
    const clip = snapshot.clips.find((item) => item.clip_id === runId);
    if (!clip) throw new Error("项目中已找不到当前片段");
    if (!clip.capabilities?.can_open_workbench) {
      throw new Error("当前片段暂时不能恢复工作台，请返回项目管理页面后重试");
    }
    const returnParams = new URLSearchParams({
      projectId: projectWorkbenchProjectId,
      focusClip: runId,
    });
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/clips/${encodeURIComponent(runId)}/workbench-sessions`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: snapshot.component_revisions.clips,
          expected_jobs_revision: snapshot.component_revisions.jobs,
          return_to: `/apps/project_workspace/?${returnParams.toString()}`,
        }),
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (response.status !== 201 || !payload.workbench_url) {
      throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    }
    projectWorkbenchInternalNavigation = true;
    window.location.replace(payload.workbench_url);
    return new Promise(() => {});
  }

  async function bootstrapProjectWorkbenchSession() {
    if (!projectWorkbenchToken || !projectWorkbenchProjectId) return null;
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}`,
      { cache: "no-store" },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      if (response.status === 409 && payload.error === "stale_workbench_session") {
        return recoverStaleProjectWorkbenchSession();
      }
      throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    }
    if (!new Set(["editing", "pending_save"]).has(payload.state)) {
      return recoverStaleProjectWorkbenchSession();
    }
    projectWorkbenchSession = payload;
    applyProjectWorkbenchSessionWorkflow(projectWorkbenchSession);
    if (projectWorkbenchTrajectoryIsPending()) {
      document.querySelector('#workflowSteps li[data-stage="upload"]')?.classList.add("is-success");
      setWorkflowStage("sfm");
      const attached = await attachActiveProjectWorkbenchTrajectory();
      if (!attached) {
        stateLabel.textContent = "待启动";
        message.textContent = isPureRotationWorkflow()
          ? "片段视频和项目 CAD 已就绪，请点击开始旋转轨迹恢复"
          : "片段视频和项目 CAD 已就绪，请点击开始 SfM 重建";
      }
    } else {
      await heartbeatProjectWorkbenchEditingSession();
      startProjectWorkbenchEditingHeartbeat();
      maybeAutoApplySfmCameraInit();
    }
    return projectWorkbenchSession;
  }

  function projectWorkbenchTrajectoryIsPending() {
    return Boolean(
      projectWorkbenchToken
      && projectWorkbenchSession?.launch_mode === "workflow_start"
    );
  }

  function projectWorkbenchTrajectoryOwnsStatus() {
    return Boolean(
      projectWorkbenchToken
      && (projectWorkbenchTrajectoryStatus || projectWorkbenchRenderStatus),
    );
  }

  async function ensureProjectWorkbenchSession() {
    if (!projectWorkbenchToken) return null;
    if (projectWorkbenchSession) return projectWorkbenchSession;
    if (!projectWorkbenchBootstrapPromise) {
      projectWorkbenchBootstrapPromise = bootstrapProjectWorkbenchSession();
    }
    return projectWorkbenchBootstrapPromise;
  }

  async function projectWorkbenchRequest(path, body) {
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}${path}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.message || payload.error || `HTTP ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  async function refreshProjectWorkbenchSession() {
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}`,
      { cache: "no-store" },
    );
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
    projectWorkbenchSession = { ...projectWorkbenchSession, ...payload };
    return projectWorkbenchSession;
  }

  function resumeOperationId() {
    return window.crypto?.randomUUID?.() || `resume-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function writeProjectWorkbenchResume({ retryConflict = true } = {}) {
    if (!projectWorkbenchToken || !projectWorkbenchSession) return null;
    const sourcePts = window.CadsceneAnnotationPts?.currentSourcePts?.();
    const sourceTimeBase = window.CadsceneAnnotationPts?.sourceTimeBase?.();
    if (!Number.isInteger(sourcePts) || !sourceTimeBase) return null;
    const workflowStage = selectedWorkflowStage === "upload"
      ? "sfm"
      : (selectedWorkflowStage || "sfm");
    try {
      const payload = await projectWorkbenchRequest(
        `/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/resume`,
        {
          expected_resume_revision: projectWorkbenchSession.resume_state?.revision ?? null,
          operation_id: resumeOperationId(),
          workflow_stage: workflowStage,
          source_pts: sourcePts,
          source_time_base: sourceTimeBase,
          quality_revision: null,
          render_revision: null,
        },
      );
      projectWorkbenchSession = { ...projectWorkbenchSession, ...payload };
      return payload.resume_state;
    } catch (error) {
      if (
        retryConflict
        && error.status === 409
        && error.payload?.error === "revision_conflict"
      ) {
        await refreshProjectWorkbenchSession();
        return writeProjectWorkbenchResume({ retryConflict: false });
      }
      throw error;
    }
  }

  async function persistProjectWorkbenchResumeNow() {
    if (projectWorkbenchResumeTimer !== null) {
      window.clearTimeout(projectWorkbenchResumeTimer);
      projectWorkbenchResumeTimer = null;
    }
    const previous = projectWorkbenchResumePromise;
    const current = (async () => {
      if (previous) await previous.catch(() => null);
      return writeProjectWorkbenchResume();
    })();
    projectWorkbenchResumePromise = current;
    try {
      return await current;
    } finally {
      if (projectWorkbenchResumePromise === current) {
        projectWorkbenchResumePromise = null;
      }
    }
  }

  function scheduleProjectWorkbenchResume() {
    if (!projectWorkbenchToken) return;
    if (projectWorkbenchResumeTimer !== null) {
      window.clearTimeout(projectWorkbenchResumeTimer);
    }
    projectWorkbenchResumeTimer = window.setTimeout(() => {
      projectWorkbenchResumeTimer = null;
      persistProjectWorkbenchResumeNow().catch((error) => {
        console.warn("[cadscene workflow] 工作台恢复状态保存失败", error);
      });
    }, PROJECT_WORKBENCH_RESUME_DEBOUNCE_MS);
  }

  function applyProjectWorkbenchResumePosition() {
    if (projectWorkbenchResumePositionApplied) return true;
    const sourcePts = projectWorkbenchSession?.resume_state?.source_pts;
    if (!Number.isInteger(sourcePts)) return false;
    const clipTime = window.CadsceneAnnotationPts?.seekSourcePts?.(sourcePts);
    if (clipTime === null || clipTime === undefined) return false;
    projectWorkbenchResumePositionApplied = true;
    return true;
  }

  async function renewProjectWorkbenchSession(expectedRevision) {
    const renewed = await projectWorkbenchRequest(
      `/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/heartbeat`,
      { expected_revision: expectedRevision },
    );
    projectWorkbenchSession = { ...projectWorkbenchSession, ...renewed };
    return renewed;
  }

  function stopProjectWorkbenchEditingHeartbeat() {
    if (projectWorkbenchEditingHeartbeatTimer === null) return;
    window.clearInterval(projectWorkbenchEditingHeartbeatTimer);
    projectWorkbenchEditingHeartbeatTimer = null;
  }

  async function heartbeatProjectWorkbenchEditingSession() {
    if (projectWorkbenchEditingHeartbeatPromise) {
      return projectWorkbenchEditingHeartbeatPromise;
    }
    if (!projectWorkbenchSession || projectWorkbenchSession.state !== "editing") return null;
    projectWorkbenchEditingHeartbeatPromise = renewProjectWorkbenchSession(
      projectWorkbenchSession.clips_revision,
    );
    try {
      return await projectWorkbenchEditingHeartbeatPromise;
    } finally {
      projectWorkbenchEditingHeartbeatPromise = null;
    }
  }

  function startProjectWorkbenchEditingHeartbeat() {
    if (projectWorkbenchEditingHeartbeatTimer !== null) return;
    projectWorkbenchEditingHeartbeatTimer = window.setInterval(() => {
      heartbeatProjectWorkbenchEditingSession().catch((error) => {
        stateLabel.textContent = "会话续租失败";
        message.textContent = `项目工作台会话续租失败：${error.message}`;
      });
    }, PROJECT_WORKBENCH_HEARTBEAT_MS);
  }

  function projectTrajectoryStatusCopy(status, stage) {
    const labels = {
      queued: "已进入资源队列",
      preparing: "正在准备轨迹解算输入",
      running: "正在进行轨迹解算",
      validating: "正在验证轨迹结果",
      success: "轨迹解算完成",
      failed: "轨迹解算失败",
      interrupted: "轨迹解算已中断",
      cancelled: "轨迹解算已取消",
      stale_input: "输入已变化，本次结果未发布",
      superseded: "本次结果已被新任务取代",
    };
    return labels[status] || stage || "等待轨迹任务";
  }

  function renderProjectTrajectorySnapshot(clip) {
    const fraction = clip.progress?.fraction;
    if (typeof fraction === "number") {
      progress.value = Math.max(0, Math.min(1, fraction));
    }
    stateLabel.textContent = projectTrajectoryStatusCopy(clip.status, clip.stage);
    message.textContent = clip.progress?.message
      || projectTrajectoryStatusCopy(clip.status, clip.stage);
  }

  async function attachActiveProjectWorkbenchTrajectory() {
    const response = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
      { cache: "no-store" },
    );
    const snapshot = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
    projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs;
    projectWorkbenchSession.clips_revision = snapshot.component_revisions.clips;
    const clip = (snapshot.clips || []).find(
      (item) => item.clip_id === projectWorkbenchSession.clip_id,
    );
    const activeStatuses = new Set(["queued", "preparing", "running", "validating"]);
    if (!clip?.job_id) return false;
    const trajectoryJobCompatible = !clip.job_type || clip.job_type === "trajectory";
    if (
      !trajectoryJobCompatible
      || !activeStatuses.has(clip.status)
    ) return false;

    projectWorkbenchTrajectoryJobId = clip.job_id;
    projectWorkbenchTrajectoryStatus = clip.status;
    runningStage = isPureRotationWorkflow() ? "pure_rotation" : "sfm";
    renderProjectTrajectorySnapshot(clip);
    document.querySelector("#workflowCancel").hidden = false;
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = true;
    });

    let managedWatcher = null;
    managedWatcher = waitForProjectWorkbenchTrajectory(clip.job_id)
      .catch((error) => {
        projectWorkbenchTrajectoryJobId = null;
        projectWorkbenchTrajectoryStatus = null;
        runningStage = null;
        stateLabel.textContent = "轨迹任务异常";
        message.textContent = error.message;
      })
      .finally(() => {
        if (projectWorkbenchTrajectoryStartPromise === managedWatcher) {
          projectWorkbenchTrajectoryStartPromise = null;
        }
        if (!projectWorkbenchTrajectoryJobId) {
          refreshSupplementalWorkflowActionAvailability(false);
        }
      });
    projectWorkbenchTrajectoryStartPromise = managedWatcher;
    return true;
  }

  async function waitForProjectWorkbenchTrajectory(jobId) {
    const terminal = new Set(["success", "failed", "interrupted", "cancelled", "stale_input", "superseded"]);
    let lastHeartbeatAt = 0;
    while (true) {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
        { cache: "no-store" },
      );
      const snapshot = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
      projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs;
      projectWorkbenchSession.clips_revision = snapshot.component_revisions.clips;
      const clip = (snapshot.clips || []).find((item) => item.clip_id === projectWorkbenchSession.clip_id);
      if (!clip || clip.job_id !== jobId) throw new Error("无法读取当前片段的轨迹任务状态");
      projectWorkbenchTrajectoryStatus = String(clip.status || clip.stage || "queued");
      renderProjectTrajectorySnapshot(clip);
      if (terminal.has(clip.status)) {
        projectWorkbenchTrajectoryJobId = null;
        document.querySelector("#workflowCancel").hidden = true;
        refreshSupplementalWorkflowActionAvailability(false);
        if (clip.status === "cancelled") {
          projectWorkbenchTrajectoryStatus = null;
          runningStage = null;
          progress.value = 0;
          stateLabel.textContent = "待处理";
          message.textContent = "任务已取消，片段已返回待处理";
          return null;
        }
        if (clip.status !== "success") {
          throw new Error(projectTrajectoryStatusCopy(clip.status, clip.stage));
        }
        const renewed = await renewProjectWorkbenchSession(snapshot.component_revisions.clips);
        const attached = await projectWorkbenchRequest(
          `/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/trajectory-ready`,
          { expected_revision: renewed.clips_revision },
        );
        projectWorkbenchSession = { ...projectWorkbenchSession, ...attached };
        progress.value = 1;
        stateLabel.textContent = "已完成";
        message.textContent = isPureRotationWorkflow()
          ? "旋转轨迹已验证，正在进入调试环节"
          : "轨迹结果已验证，正在载入关键帧标定工作台";
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set("workflowStage", "keyframes");
        projectWorkbenchInternalNavigation = true;
        window.location.replace(nextUrl.toString());
        return attached;
      }
      const now = Date.now();
      if (now - lastHeartbeatAt >= PROJECT_WORKBENCH_HEARTBEAT_MS) {
        await renewProjectWorkbenchSession(snapshot.component_revisions.clips);
        lastHeartbeatAt = now;
      }
      try {
        const runtimeResponse = await fetch(
          `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/jobs/${encodeURIComponent(jobId)}/runtime`,
          { cache: "no-store" },
        );
        if (runtimeResponse.ok) {
          const runtime = await runtimeResponse.json();
          const content = document.querySelector("#workflowLogContent");
          if (content) content.textContent = runtime.lines.join("\n") || "暂无日志";
        }
      } catch (error) {
        // 实时详情读取失败时继续使用项目 snapshot，不能中断任务状态跟踪。
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }

  async function runProjectWorkbenchTrajectory() {
    if (projectWorkbenchTrajectoryStartPromise) {
      return projectWorkbenchTrajectoryStartPromise;
    }
    projectWorkbenchTrajectoryStartPromise = runProjectWorkbenchTrajectoryOnce();
    try {
      return await projectWorkbenchTrajectoryStartPromise;
    } finally {
      projectWorkbenchTrajectoryStartPromise = null;
      if (!projectWorkbenchTrajectoryJobId) {
        refreshSupplementalWorkflowActionAvailability(false);
      }
    }
  }

  async function runProjectWorkbenchTrajectoryOnce() {
    if (!projectWorkbenchSession) throw new Error("项目工作台会话尚未就绪");
    if (projectWorkbenchSession.launch_mode === "trajectory_ready") {
      throw new Error("当前片段已有可用轨迹；如需重算，请返回片段管理页面重试");
    }
    const clipId = projectWorkbenchSession.clip_id;
    projectWorkbenchTrajectoryStatus = "checking";
    stateLabel.textContent = "正在检查任务";
    message.textContent = "正在确认当前片段的轨迹任务状态";
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = true;
    });
    const snapshotResponse = await fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
      { cache: "no-store" },
    );
    const snapshot = await snapshotResponse.json().catch(() => ({}));
    if (!snapshotResponse.ok) throw new Error(snapshot.error || `HTTP ${snapshotResponse.status}`);
    projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs;
    const currentClip = (snapshot.clips || []).find((item) => item.clip_id === clipId);
    if (!currentClip) throw new Error("项目中找不到当前片段");

    const activeStatuses = new Set(["queued", "preparing", "running", "validating", "cancel_requested"]);
    const retryStatuses = new Set(["failed", "interrupted", "cancelled", "stale_input", "superseded"]);
    let jobId = null;
    if (currentClip.job_id && activeStatuses.has(currentClip.status)) {
      jobId = currentClip.job_id;
    } else if (currentClip.job_id && retryStatuses.has(currentClip.status)) {
      const retried = await projectWorkbenchRequest(
        `/jobs/${encodeURIComponent(currentClip.job_id)}/retry`,
        { expected_revision: snapshot.component_revisions.jobs },
      );
      jobId = retried.job_id;
      projectWorkbenchSession.jobs_revision = retried.jobs_revision;
    } else {
      const preflight = await projectWorkbenchRequest("/trajectory-jobs", {
        expected_revision: projectWorkbenchSession.jobs_revision,
        clip_ids: [clipId],
        enqueue: false,
      });
      const confirmed = (preflight.needs_confirmation || []).includes(clipId) ? [clipId] : [];
      const queued = await projectWorkbenchRequest("/trajectory-jobs", {
        expected_revision: projectWorkbenchSession.jobs_revision,
        clip_ids: [clipId],
        confirmed_clip_ids: confirmed,
        enqueue: true,
      });
      jobId = queued.job_ids?.[0];
      projectWorkbenchSession.jobs_revision = queued.jobs_revision;
    }
    if (!jobId) throw new Error("轨迹任务未能进入队列");
    projectWorkbenchTrajectoryJobId = jobId;
    projectWorkbenchTrajectoryStatus = "queued";
    runningStage = isPureRotationWorkflow() ? "pure_rotation" : "sfm";
    progress.value = 0;
    stateLabel.textContent = "排队中";
    message.textContent = "已进入资源队列，算法将在获得资源后启动";
    document.querySelector("#workflowCancel").hidden = false;
    document.querySelectorAll("[data-job-action]").forEach((button) => {
      button.disabled = true;
    });
    return waitForProjectWorkbenchTrajectory(jobId);
  }

  async function finalizeProjectWorkbenchSave(result, { navigate = true } = {}) {
    if (!projectWorkbenchToken) return null;
    if (!projectWorkbenchSession) throw new Error("项目工作台会话尚未就绪");
    if (projectWorkbenchSaveInFlight) return null;
    if (projectWorkbenchSession.state !== "editing" && projectWorkbenchSession.state !== "pending_save") {
      throw new Error("项目工作台会话已失效，请返回项目管理页面重新进入");
    }
    stopProjectWorkbenchEditingHeartbeat();
    if (projectWorkbenchEditingHeartbeatPromise) {
      await projectWorkbenchEditingHeartbeatPromise;
    }
    projectWorkbenchSaveInFlight = true;
    try {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectWorkbenchSession.project_id)}/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/save`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: projectWorkbenchSession.clips_revision,
            existing_save: result,
          }),
        },
      );
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.message || payload.error || `HTTP ${response.status}`);
      projectWorkbenchSession = { ...projectWorkbenchSession, ...payload };
      await persistProjectWorkbenchResumeNow();
      if (navigate) window.location.assign(projectWorkbenchSession.return_to);
      return payload;
    } finally {
      projectWorkbenchSaveInFlight = false;
      if (projectWorkbenchSession?.state === "editing") {
        startProjectWorkbenchEditingHeartbeat();
      }
    }
  }

  async function finishQualityStage() {
    setWorkflowStage("render");
    message.textContent = "已进入渲染导出；可以添加标签后渲染视频。";
    await persistQualityCompletion();
  }

  async function persistQualityCompletion() {
    await ensureProjectWorkbenchSession();
    const result = await saveCurrentCameraTrack();
    await finalizeProjectWorkbenchSave(result, { navigate: false });
  }

  async function persistWorkbenchDraftForReturn() {
    if (!projectWorkbenchToken || !projectWorkbenchSession) return null;
    if (projectWorkbenchTrajectoryIsPending()) return null;
    if (isPureRotationWorkflow()) {
      if (
        window.pureRotationViewer?.pose
        && (pureRotationEditMode === "placement" || !pureRotationHasPlacement)
      ) {
        await savePureRotationPlacement();
      }
      if (pureRotationCorrections.length) {
        await apiPost("/api/pure-rotation/corrections", {
          dataset,
          runId,
          corrections: pureRotationCorrections,
        });
      }
      return { ok: true, kind: "pure_rotation_draft" };
    }
    if (typeof window.cadsceneGetCameraTrack === "function") {
      return saveCurrentCameraTrack();
    }
    return null;
  }

  function projectWorkbenchFallbackReturnTo() {
    const target = new URL("/apps/project_workspace/", window.location.origin);
    target.searchParams.set("projectId", projectWorkbenchProjectId);
    if (runId) target.searchParams.set("focusClip", runId);
    return `${target.pathname}${target.search}`;
  }

  async function returnToProjectWorkspace() {
    if (projectWorkbenchBootstrapFailed || !projectWorkbenchSession) {
      projectWorkbenchInternalNavigation = true;
      window.location.assign(projectWorkbenchFallbackReturnTo());
      return;
    }
    await ensureProjectWorkbenchSession();
    await persistWorkbenchDraftForReturn();
    await persistProjectWorkbenchResumeNow();
    if (
      projectWorkbenchSession.state === "editing"
      || projectWorkbenchSession.state === "pending_save"
    ) {
      const closed = await projectWorkbenchRequest(
        `/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/close`,
        { expected_revision: projectWorkbenchSession.clips_revision },
      );
      projectWorkbenchSession = { ...projectWorkbenchSession, ...closed };
    }
    projectWorkbenchInternalNavigation = true;
    window.location.assign(projectWorkbenchSession.return_to);
  }

  window.addEventListener("pagehide", () => {
    stopProjectWorkbenchEditingHeartbeat();
    if (
      projectWorkbenchInternalNavigation
      || projectWorkbenchSaveInFlight
      || !projectWorkbenchSession
      || projectWorkbenchSession.state !== "editing"
    ) return;
    fetch(
      `/api/projects/${encodeURIComponent(projectWorkbenchSession.project_id)}/workbench-sessions/${encodeURIComponent(projectWorkbenchToken)}/close`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: projectWorkbenchSession.clips_revision }),
        keepalive: true,
      },
    ).catch(() => {});
  });

  window.addEventListener("beforeunload", (event) => {
    if (projectWorkbenchInternalNavigation) return;
    const hasUnsavedCameraDraft = Boolean(
      window.cadsceneHasUnsavedCameraDraft?.()
      || pureRotationCorrectionDraftDirty,
    );
    if (!hasUnsavedCameraDraft) return;
    event.preventDefault();
    event.returnValue = "";
  });

  function updateKeyframePlanUi() {
    const alignmentButton = document.querySelector("#workflowRunAlignment");
    const continueButton = document.querySelector("#workflowContinueKeyframes");
    const finishButton = document.querySelector("#workflowFinishKeyframes");
    const planStatus = document.querySelector("#workflowKeyframePlanStatus");
    const pending = Number(keyframePlan?.pending_count || 0);
    const completed = Number(keyframePlan?.completed_count || 0);
    const total = Array.isArray(keyframePlan?.frames) ? keyframePlan.frames.length : 0;
    if (alignmentButton) alignmentButton.textContent = keyframePlan ? "重新路线拟合" : "路线拟合";
    if (continueButton) continueButton.disabled = trajectoryWorkflowActionsAreBlocked() || keyframeSaveInFlight || !keyframePlan || pending === 0;
    if (finishButton) finishButton.disabled = trajectoryWorkflowActionsAreBlocked() || keyframeSaveInFlight || !keyframePlan || pending > 0;
    if (planStatus) {
      planStatus.textContent = keyframePlan
        ? `计划：已完成 ${completed}/${total}，待标定 ${pending}。待标定帧不会计入人工关键帧。`
        : "尚未生成关键帧计划；初步路线拟合后可按间隔生成。";
    }
    if (typeof window.cadsceneSetKeyframePlan === "function") {
      window.cadsceneSetKeyframePlan(keyframePlan);
    }
  }

  async function loadKeyframePlan({ suppressErrors = true } = {}) {
    if (!dataset || !runId) return null;
    try {
      const response = await fetch(
        `/api/workflow/keyframe-plan?dataset=${encodeURIComponent(dataset)}&runId=${encodeURIComponent(runId)}`,
        { cache: "no-store" },
      );
      if (response.status === 404) {
        keyframePlan = null;
      } else {
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.error || `HTTP ${response.status}`);
        keyframePlan = payload;
      }
    } catch (error) {
      keyframePlan = null;
      if (!suppressErrors) throw error;
    }
    updateKeyframePlanUi();
    return keyframePlan;
  }

  function jumpToNextPendingKeyframe() {
    const pending = (keyframePlan?.frames || []).find((item) => item.status !== "completed");
    if (!pending) {
      message.textContent = keyframePlan ? "关键帧计划已全部完成，请重新路线拟合。" : "请先完成路线拟合并生成关键帧计划。";
      return;
    }
    const frameInput = document.querySelector("#frameInput");
    if (frameInput) frameInput.value = String(pending.frame_index);
    document.querySelector("#goToFrame")?.click();
    message.textContent = `已跳转到待标定关键帧 ${pending.frame_index}。`;
  }

  async function generateKeyframePlan() {
    const planStatus = document.querySelector("#workflowKeyframePlanStatus");
    try {
      // 路线拟合可能刚在另一次任务轮询中完成，不能用页面加载时的旧状态阻断用户。
      await refreshAlignmentArtifactState();
      if (!alignmentArtifactReady) {
        throw new Error("请先完成首帧/初始关键帧的路线拟合，再生成关键帧计划。");
      }
      await saveCurrentCameraTrack();
      const intervalFrames = Number(document.querySelector("#workflowKeyframeStep")?.value || 180);
      await apiPost("/api/workflow/generate-keyframe-plan", {
        dataset,
        runId,
        intervalFrames,
      });
      // 以服务端实际落盘的计划为准；该回读也会立刻刷新质量时间轴中的紫色计划标记。
      await loadKeyframePlan({ suppressErrors: false });
      if (!keyframePlan) throw new Error("关键帧计划已提交，但未能读取保存结果，请刷新页面后重试。");
      const pending = Number(keyframePlan.pending_count || 0);
      message.textContent = `已生成 ${intervalFrames} 帧间隔的关键帧计划：${pending} 个待标定帧。`;
      jumpToNextPendingKeyframe();
    } catch (error) {
      if (planStatus) planStatus.textContent = `关键帧计划生成失败：${error.message}`;
      throw error;
    }
  }

  async function finishKeyframePlan() {
    await saveCurrentCameraTrack();
    if (!keyframePlan) throw new Error("请先在初步路线拟合后生成关键帧计划。");
    const pending = Number(keyframePlan.pending_count || 0);
    if (pending > 0) throw new Error(`关键帧计划还有 ${pending} 帧待标定，请继续完成后再拟合。`);
    sessionStorage.setItem(postAlignmentStageKey(), "quality");
    message.textContent = "关键帧计划已完成，正在进行最终路线拟合；成功后将进入质量检测。";
    try {
      return await startAlignmentStage();
    } catch (error) {
      sessionStorage.removeItem(postAlignmentStageKey());
      throw error;
    }
  }

  async function startQualityStage() {
    await saveCurrentCameraTrack();
    if (!keyframePlan) {
      throw new Error("请先完成初步路线拟合并生成关键帧计划。");
    }
    if (Number(keyframePlan.pending_count || 0) > 0) {
      throw new Error(`关键帧计划还有 ${keyframePlan.pending_count} 帧待标定，请先完成并重新路线拟合。`);
    }
    return runStage("quality");
  }

  async function startAlignmentStage() {
    // 路线拟合 = 先保存当前已标定关键帧，再用它们运行 alignment（生成初步路线/相机计划）。
    const cameraTrack = window.cadsceneGetCameraTrack();
    const manualAnchorCount = window.CadsceneKeyframes.confirmedManualKeyframes(
      cameraTrack.keyframes || [],
    ).length;
    if (manualAnchorCount < 2) {
      throw new Error(`路线拟合至少需要 2 个人工关键帧；当前为 ${manualAnchorCount} 个。请在另一帧完成“添加/更新关键帧”后再运行。`);
    }
    await saveCurrentCameraTrack(cameraTrack);
    return runStage("alignment");
  }

  async function persistEditedCameraTrack({ advancePlan = false } = {}) {
    if (!dataset || !runId || typeof window.cadsceneGetCameraTrack !== "function") return;
    if (keyframeSaveInFlight) return;
    keyframeSaveInFlight = true;
    const editedFrame = currentFrame();
    document.querySelector("#addKeyframe")?.setAttribute("disabled", "");
    updateKeyframePlanUi();
    // 旧版按钮先同步更新内存中的 track；下一事件循环再读取可取得更新后的相机值。
    await new Promise((resolve) => window.setTimeout(resolve, 0));
    try {
      await saveCurrentCameraTrack();
      window.cadsceneClearUnsavedCameraDraft?.();
      const plannedFrame = (keyframePlan?.frames || []).find(
        (item) => Number(item.frame_index) === editedFrame,
      );
      if (advancePlan && selectedWorkflowStage === "keyframes" && plannedFrame?.status === "completed") {
        const pending = (keyframePlan?.frames || []).find((item) => item.status !== "completed");
        if (pending) {
          message.textContent = `计划关键帧 ${editedFrame} 已保存，正在进入下一帧。`;
          jumpToNextPendingKeyframe();
        } else {
          message.textContent = "计划关键帧已全部完成，请点击“完成关键帧标定”进行最终路线拟合。";
        }
      }
    } catch (error) {
      console.warn("[cadscene workflow] keyframe auto-save failed", error);
      message.textContent = `关键帧自动保存失败：${error.message}`;
    } finally {
      keyframeSaveInFlight = false;
      document.querySelector("#addKeyframe")?.removeAttribute("disabled");
      updateKeyframePlanUi();
    }
  }

  function projectRenderStatusCopy(status, stage) {
    const labels = {
      queued: "已进入渲染队列",
      preparing: "正在准备渲染输入",
      running: "正在渲染片段",
      validating: "正在验证渲染结果",
      success: "片段渲染完成",
      failed: "片段渲染失败",
      interrupted: "片段渲染已中断",
      cancelled: "片段渲染已取消",
      stale_input: "输入已变化，本次渲染未发布",
      superseded: "本次渲染已被新任务取代",
    };
    return labels[status] || stage || "等待渲染任务";
  }

  function setProjectRenderVisibleProgress(candidate, { complete = false } = {}) {
    const numeric = Number(candidate);
    if (!Number.isFinite(numeric)) return projectRenderVisibleProgress;
    const bounded = complete ? 1 : Math.min(0.99, Math.max(0, numeric));
    projectRenderVisibleProgress = Math.max(projectRenderVisibleProgress, bounded);
    progress.value = projectRenderVisibleProgress;
    return projectRenderVisibleProgress;
  }

  function projectRenderPreflightCopy(reason) {
    const value = String(reason || "");
    if (value.includes("media specification")) return "项目视频规格尚未准备完成，请重新分析后再试";
    if (value.includes("render adapter is unavailable")) return "当前工作流的渲染能力尚不可用";
    if (value.includes("saved workbench output")) return "请先保存当前工作台调试结果";
    if (value.includes("trajectory")) return "当前轨迹结果无效或已过期，请重新解算";
    return value || "当前片段尚不满足渲染条件";
  }

  async function waitForProjectWorkbenchRender(jobId) {
    const terminal = new Set(["success", "failed", "interrupted", "cancelled", "stale_input", "superseded"]);
    while (true) {
      const response = await fetch(
        `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
        { cache: "no-store" },
      );
      const snapshot = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(snapshot.error || `HTTP ${response.status}`);
      projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs;
      projectWorkbenchSession.clips_revision = snapshot.component_revisions.clips;
      const clip = (snapshot.clips || []).find(
        (item) => item.clip_id === projectWorkbenchSession.clip_id,
      );
      if (!clip || clip.render?.job_id !== jobId) {
        throw new Error("无法读取当前片段的渲染任务状态");
      }
      const render = clip.render;
      projectWorkbenchRenderStatus = String(render.status || render.stage || "queued");
      if (terminal.has(render.status)) {
        if (render.status === "success") {
          setProjectRenderVisibleProgress(1, { complete: true });
        }
        projectWorkbenchRenderJobId = null;
        runningStage = null;
        if (render.status !== "success") {
          projectWorkbenchRenderStatus = null;
          throw new Error(projectRenderStatusCopy(render.status, render.stage));
        }
        projectWorkbenchRenderStatus = "success";
        projectRenderOutputUrl = render.preview_url || null;
        stateLabel.textContent = "已完成";
        message.textContent = "片段渲染完成；可以预览视频，或返回项目管理继续处理其他片段。";
        await refreshRenderOutputState();
        return render;
      }
      const fraction = render.progress?.fraction;
      if (typeof fraction === "number") setProjectRenderVisibleProgress(fraction);
      stateLabel.textContent = projectRenderStatusCopy(render.status, render.stage);
      if (render.status === "validating") {
        message.textContent = "正在封装并验证渲染结果";
      } else {
        message.textContent = projectRenderStatusCopy(render.status, render.stage);
      }
      try {
        const runtimeResponse = await fetch(
          `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/jobs/${encodeURIComponent(jobId)}/runtime`,
          { cache: "no-store" },
        );
        if (runtimeResponse.ok) {
          const runtime = await runtimeResponse.json();
          const content = document.querySelector("#workflowLogContent");
          if (content) content.textContent = runtime.lines.join("\n") || "暂无日志";
          updateRenderProgressFromLog("render", runtime.lines || []);
        }
      } catch (error) {
        // 实时日志暂不可用时继续依赖项目 snapshot 跟踪后台任务。
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }

  async function startRenderStage() {
    if (projectWorkbenchToken) {
      await ensureProjectWorkbenchSession();
      if (
        projectWorkbenchSession.state === "editing"
        || projectWorkbenchSession.state === "pending_save"
      ) {
        const result = await saveCurrentCameraTrack();
        await finalizeProjectWorkbenchSave(result, { navigate: false });
      } else if (projectWorkbenchSession.state !== "saved") {
        throw new Error("项目工作台会话已失效，请返回项目管理页面重新进入");
      }
      const clipId = projectWorkbenchSession.clip_id;
      const snapshotResponse = await fetch(
        `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
        { cache: "no-store" },
      );
      const snapshot = await snapshotResponse.json().catch(() => ({}));
      if (!snapshotResponse.ok) throw new Error(snapshot.error || `HTTP ${snapshotResponse.status}`);
      projectWorkbenchSession.jobs_revision = snapshot.component_revisions.jobs;
      projectWorkbenchSession.clips_revision = snapshot.component_revisions.clips;
      const currentClip = (snapshot.clips || []).find((item) => item.clip_id === clipId);
      const activeStatuses = new Set(["queued", "preparing", "running", "validating", "cancel_requested"]);
      if (currentClip?.render?.job_id && activeStatuses.has(currentClip.render.status)) {
        renderProgressState = null;
        projectRenderVisibleProgress = 0;
        projectWorkbenchRenderJobId = currentClip.render.job_id;
        projectWorkbenchRenderStatus = currentClip.render.status;
        runningStage = "project_render";
        message.textContent = "已恢复当前片段的后台渲染任务。";
        return waitForProjectWorkbenchRender(currentClip.render.job_id);
      }
      const preflight = await projectWorkbenchRequest("/render-jobs", {
        expected_revision: projectWorkbenchSession.jobs_revision,
        clip_ids: [clipId],
        enqueue: false,
      });
      const confirmationRequired = preflight.needs_confirmation || preflight.confirmation_required || [];
      const confirmedClipIds = confirmationRequired.includes(clipId) ? [clipId] : [];
      if (!(preflight.eligible || []).includes(clipId) && !confirmedClipIds.length) {
        runningStage = null;
        progress.value = 0;
        stateLabel.textContent = "无法开始渲染";
        throw new Error(projectRenderPreflightCopy(preflight.reasons?.[clipId]));
      }
      const queued = await projectWorkbenchRequest("/render-jobs", {
        expected_revision: projectWorkbenchSession.jobs_revision,
        clip_ids: [clipId],
        confirmed_clip_ids: confirmedClipIds,
        enqueue: true,
      });
      const jobId = queued.job_ids?.[0];
      if (!jobId) throw new Error("渲染任务未能进入项目队列");
      projectWorkbenchSession.jobs_revision = queued.jobs_revision;
      renderProgressState = null;
      projectRenderVisibleProgress = 0;
      projectWorkbenchRenderJobId = jobId;
      projectWorkbenchRenderStatus = "queued";
      runningStage = "project_render";
      progress.value = 0;
      stateLabel.textContent = "排队中";
      message.textContent = "渲染任务已进入后台队列；返回项目管理不会中断任务。";
      document.querySelector("#workflowCancel").hidden = true;
      return waitForProjectWorkbenchRender(jobId);
    }
    await saveCurrentCameraTrack();
    message.textContent = "正在用最新人工关键帧重新拟合路线并渲染。";
    return runStage("render");
  }

  async function cancelRunningJob() {
    if (projectWorkbenchToken) {
      if (!projectWorkbenchTrajectoryJobId) return null;
      const jobId = projectWorkbenchTrajectoryJobId;
      const previousStatus = projectWorkbenchTrajectoryStatus;
      projectWorkbenchTrajectoryJobId = null;
      projectWorkbenchTrajectoryStatus = "cancelled";
      document.querySelector("#workflowCancel").hidden = true;
      try {
        const cancelled = await projectWorkbenchRequest(
          `/jobs/${encodeURIComponent(jobId)}/cancel`,
          { expected_revision: projectWorkbenchSession.jobs_revision },
        );
        projectWorkbenchSession.jobs_revision = cancelled.jobs_revision;
        message.textContent = "已请求取消轨迹任务";
        return cancelled;
      } catch (error) {
        projectWorkbenchTrajectoryJobId = jobId;
        projectWorkbenchTrajectoryStatus = previousStatus;
        document.querySelector("#workflowCancel").hidden = false;
        throw error;
      }
    }
    await apiPost("/api/workflow/cancel", { dataset, runId });
    runningStage = null;
    sessionStorage.removeItem(`cadsceneJobReload:${dataset}:${runId}`);
    await pollJobStatus();
  }

  function updateRenderProgressFromLog(stage, lines) {
    if (stage !== "render" || !Array.isArray(lines)) return;
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      const match = lines[index].match(/\[render\]\s+frame\s+(\d+)\/(\d+)/);
      if (!match) continue;
      const completed = Number(match[1]);
      const total = Math.max(1, Number(match[2]));
      renderProgressState = { completed, total };
      if (projectWorkbenchRenderJobId) {
        setProjectRenderVisibleProgress(completed / total);
        message.textContent = completed >= total
          ? "正在封装并验证渲染结果"
          : `正在渲染 ${completed}/${total}`;
      } else {
        progress.value = Math.min(1, completed / total);
        message.textContent = "正在渲染";
      }
      return;
    }
  }

  async function pollJobLog() {
    if (projectWorkbenchTrajectoryOwnsStatus()) return;
    if (projectWorkbenchTrajectoryIsPending()) return;
    const stage = runningStage || latestJobStatus?.current_stage || selectedWorkflowStage;
    if (!dataset || !runId || !["sfm", "alignment", "quality", "render"].includes(stage)) return;
    try {
      const response = await fetch(
        `/api/workflow/job-log?dataset=${encodeURIComponent(dataset)}&runId=${encodeURIComponent(runId)}&stage=${stage}&tail=30`,
        { cache: "no-store" },
      );
      if (!response.ok) return;
      const payload = await response.json();
      const content = document.querySelector("#workflowLogContent");
      if (content) content.textContent = (payload.lines || []).join("\n") || "暂无日志";
      updateRenderProgressFromLog(stage, payload.lines || []);
    } catch (error) {
      // 日志轮询失败不影响主 viewer。
    }
  }

  function runWithMessage(action) {
    action().catch((error) => {
      message.textContent = error.message;
    });
  }

  async function applySfmCameraInitializationOnce() {
    if (sfmCameraInitializationComplete) return false;
    if (sfmCameraInitializationPromise) return sfmCameraInitializationPromise;
    sfmCameraInitializationPromise = (async () => {
      // 已保存的人工/拟合轨迹是权威结果，不能被 SfM 初值覆盖。
      if (window.cadsceneHasLoadedCameraTrack?.()) {
        sfmCameraInitializationComplete = true;
        return false;
      }
      const response = await fetch(
        `/api/workflow/sfm-camera-init?dataset=${encodeURIComponent(dataset)}&runId=${encodeURIComponent(runId)}`,
        { cache: "no-store" },
      );
      const params = await response.json();
      if (!response.ok) throw new Error(params.error || `HTTP ${response.status}`);
      if (typeof window.cadsceneApplyCameraParameters !== "function") {
        throw new Error("viewer 相机尚未初始化");
      }
      const appliedFields = window.cadsceneApplyCameraParameters(params);
      if (!Array.isArray(appliedFields) || appliedFields.length === 0) {
        return false;
      }
      sfmCameraInitializationComplete = true;
      message.textContent = params.orientation_safe_to_apply
        ? `已使用 SfM 第 ${params.frame_index} 帧初始化 ${appliedFields.join("/")}；位置坐标保持不变。`
        : `已使用 SfM 初始化 FOV=${params.fov.toFixed(1)}°；yaw/pitch/roll 将在首个人工锚点后确定。`;
      return true;
    })();
    try {
      return await sfmCameraInitializationPromise;
    } finally {
      sfmCameraInitializationPromise = null;
    }
  }

  function currentFrame() {
    const inputValue = Number(document.querySelector("#frameInput")?.value);
    if (Number.isFinite(inputValue) && inputValue >= 0) return Math.round(inputValue);
    const match = document.querySelector("#frameStatus")?.textContent?.match(/\d+/);
    return match ? Number(match[0]) : 0;
  }

  async function ignoreSuggestion(frameIndex) {
    const response = await fetch("/api/workflow/ignore-suggestion", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset,
        run_id: runId,
        frame_index: frameIndex,
        reason: "用户确认无需补帧",
      }),
    });
    if (!response.ok) throw new Error(`ignored_suggestions.json HTTP ${response.status}`);
    localStorage.setItem(`cadsceneIgnored:${dataset}:${runId}:${frameIndex}`, "1");
    ignoredSuggestionFrames.add(frameIndex);
    window.cadsceneSetIgnoredSuggestions?.([...ignoredSuggestionFrames]);
    message.textContent = `建议帧 ${frameIndex} 已标记为忽略。`;
    selectedSuggestionFrame = null;
    updateSuggestionActions();
  }

  function availableSuggestions() {
    return workflowSuggestions.filter((item) => !ignoredSuggestionFrames.has(Number(item.frame_index)));
  }

  function updateSuggestionActions() {
    const viewButton = document.querySelector("#viewCurrentSuggestion");
    const ignoreButton = document.querySelector("#ignoreCurrentSuggestion");
    const available = availableSuggestions();
    const hasSuggestion = available.length > 0;
    if (viewButton) {
      viewButton.disabled = !hasSuggestion;
      viewButton.textContent = hasSuggestion ? "查看当前建议帧" : "暂无建议";
    }
    if (ignoreButton) ignoreButton.disabled = !hasSuggestion;
  }

  async function loadWorkflowSuggestions() {
    const viewerPaths = window.resolveViewerPaths ? window.resolveViewerPaths() : {};
    const suggestionsPath = viewerPaths.suggestions || runPath("04_quality/keyframe_suggestions.json");
    if (!suggestionsPath) return updateSuggestionActions();
    try {
      const response = await fetch(suggestionsPath, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      const list = Array.isArray(payload) ? payload : payload.suggestions || [];
      const priorityRank = { high: 3, medium: 2, low: 1 };
      workflowSuggestions = list.slice().sort((a, b) => {
        const scoreA = Number(a.risk_score ?? priorityRank[a.priority] ?? 0);
        const scoreB = Number(b.risk_score ?? priorityRank[b.priority] ?? 0);
        return scoreB - scoreA || Number(a.frame_index) - Number(b.frame_index);
      });
    } catch (error) {
      workflowSuggestions = [];
    }
    const ignoredPath = runPath("04_quality/ignored_suggestions.json");
    if (ignoredPath) {
      try {
        const response = await fetch(ignoredPath, { cache: "no-store" });
        if (response.ok) {
          const payload = await response.json();
          ignoredSuggestionFrames = new Set((payload.ignored || []).map((item) => Number(item.frame_index)));
        }
      } catch (error) {
        // ignored_suggestions.json 尚未生成时按空列表处理。
      }
    }
    window.cadsceneSetIgnoredSuggestions?.([...ignoredSuggestionFrames]);
    updateSuggestionActions();
  }

  function jumpToCurrentSuggestion() {
    const available = availableSuggestions();
    if (!available.length) return updateSuggestionActions();
    const suggestion = available.find((item) => Number(item.frame_index) === selectedSuggestionFrame) || available[0];
    selectedSuggestionFrame = Number(suggestion.frame_index);
    const frameInput = document.querySelector("#frameInput");
    if (frameInput) frameInput.value = String(selectedSuggestionFrame);
    document.querySelector("#goToFrame")?.click();
    message.textContent = `已跳转到建议帧 ${selectedSuggestionFrame}。`;
  }

  document.querySelectorAll("#workflowSteps li").forEach((node) => {
    node.addEventListener("click", () => setWorkflowStage(node.dataset.stage));
  });
  document.querySelector("#workflowStartSfm")?.addEventListener("click", () => runWithMessage(() => runStage("sfm")));
  document.querySelector("#workflowStartPureRotation")?.addEventListener("click", () => runWithMessage(() => runPureRotationStage()));
  document.querySelector("#workflowRerunPureRotation")?.addEventListener("click", () => runWithMessage(() => runPureRotationStage({ force: true })));
  // 路线拟合放在关键帧标定阶段：先保存当前关键帧，再启动 alignment job。
  document.querySelector("#workflowRunAlignment")?.addEventListener("click", () => runWithMessage(startAlignmentStage));
  // 质量检测只启动 quality job（不再包含路线拟合）。
  document.querySelector("#workflowRunQuality")?.addEventListener("click", () => runWithMessage(startQualityStage));
  document.querySelector("#workflowRerunQuality")?.addEventListener("click", () => runWithMessage(startQualityStage));
  document.querySelector("#workflowFinishQuality")?.addEventListener("click", () => runWithMessage(finishQualityStage));
  document.querySelector("#workflowReturnKeyframes")?.addEventListener("click", () => setWorkflowStage("keyframes"));
  document.querySelector("#workflowRender")?.addEventListener("click", () => runWithMessage(startRenderStage));
  document.querySelector("#workflowCancel")?.addEventListener("click", () => runWithMessage(cancelRunningJob));
  document.querySelector("#workflowFinishKeyframes")?.addEventListener("click", () => {
    if (isPureRotationWorkflow()) setWorkflowStage("render");
    else runWithMessage(finishKeyframePlan);
  });
  document.querySelector("#workflowPureFinishKeyframes")?.addEventListener("click", () => runWithMessage(finishPureRotationCalibration));
  document.querySelector("#workbenchThemeToggle")?.addEventListener("click", () => {
    const theme = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    applyWorkbenchTheme(theme);
  });
  const workbenchReturnButton = document.querySelector("#workbenchReturnButton");
  if (workbenchReturnButton) workbenchReturnButton.hidden = !projectWorkbenchToken;
  workbenchReturnButton?.addEventListener("click", () => {
    document.querySelector("#workbenchReturnDialog")?.showModal();
  });
  document.querySelector("#workbenchConfirmReturn")?.addEventListener("click", () => {
    runWithMessage(returnToProjectWorkspace);
  });
  document.querySelector("#workflowReturnPureCalibration")?.addEventListener("click", () => setWorkflowStage("keyframes"));
  document.querySelector("#pureRotationPlacementSection")?.addEventListener("focusin", () => setPureRotationEditMode("placement"));
  document.querySelector("#pureRotationSavePlacement")?.addEventListener("click", () => runWithMessage(savePureRotationPlacement));
  document.querySelector("#pureRotationRestorePlacement")?.addEventListener("click", () => runWithMessage(restorePureRotationPlacement));
  document.querySelector("#pureRotationWorldYaw")?.addEventListener("input", (event) => {
    previewPureRotationWorldYaw(event.target.value);
  });
  document.querySelector("#pureRotationWorldYawNumber")?.addEventListener("input", (event) => {
    previewPureRotationWorldYaw(event.target.value);
  });
  document.querySelector("#pureRotationResetWorldYaw")?.addEventListener("click", () => {
    previewPureRotationWorldYaw(0);
  });
  for (const key of ["yaw", "pitch", "roll"]) {
    document.querySelector(`#pureRotationLocal${key[0].toUpperCase()}${key.slice(1)}`)?.addEventListener("input", (event) => {
      previewPureRotationLocalDelta(key, event.target.value);
    });
  }
  document.querySelector("#addKeyframe")?.addEventListener("click", () => persistEditedCameraTrack({ advancePlan: true }));
  document.querySelector("#deleteKeyframe")?.addEventListener("click", () => persistEditedCameraTrack());
  document.querySelector("#workflowGenerateKeyframes")?.addEventListener("click", () => runWithMessage(generateKeyframePlan));
  document.querySelector("#workflowContinueKeyframes")?.addEventListener("click", jumpToNextPendingKeyframe);
  document.querySelector("#viewCurrentSuggestion")?.addEventListener("click", jumpToCurrentSuggestion);
  document.querySelector("#ignoreCurrentSuggestion")?.addEventListener("click", () => {
    const frameIndex = selectedSuggestionFrame ?? currentFrame();
    ignoreSuggestion(frameIndex).catch((error) => {
      message.textContent = `忽略建议失败：${error.message}`;
    });
  });
  document.querySelector("#workflowVideoInput")?.addEventListener("change", (event) => {
    runWithMessage(() => importSelectedFile("video", event.target.files?.[0]));
  });
  document.querySelector("#workflowHoveringDeclared")?.addEventListener("change", (event) => {
    const hint = document.querySelector("#workflowMotionModeHint");
    if (hint) hint.textContent = event.target.checked
      ? "将使用 OpenGV 悬停旋转流程；相机位置固定，不恢复平移。"
      : "将使用通用 SfM 三维重建流程。";
  });
  document.querySelector("#workflowCadInput")?.addEventListener("change", (event) => {
    runWithMessage(() => importSelectedFile("cad", event.target.files?.[0]));
  });
  document.querySelector("#workflowAdvancedCadInput")?.addEventListener("change", (event) => {
    runWithMessage(() => importSelectedFile("cad", event.target.files?.[0]));
  });

  const renderPath = runPath("08_render/sfm_align_overlay.mp4");
  const preview = document.querySelector("#workflowPreviewRender");
  const download = document.querySelector("#workflowDownloadRender");
  const previewDialog = document.querySelector("#workflowRenderPreviewDialog");
  const previewVideo = document.querySelector("#workflowRenderPreviewVideo");

  function activeRenderPath() {
    return projectRenderOutputUrl || renderPath;
  }

  async function refreshRenderOutputState() {
    if (projectWorkbenchToken && projectWorkbenchProjectId && projectWorkbenchSession?.clip_id) {
      const snapshotResponse = await fetch(
        `/api/projects/${encodeURIComponent(projectWorkbenchProjectId)}/snapshot`,
        { cache: "no-store" },
      );
      if (snapshotResponse.ok) {
        const snapshot = await snapshotResponse.json();
        const clip = (snapshot.clips || []).find(
          (item) => item.clip_id === projectWorkbenchSession.clip_id,
        );
        if (clip?.render?.preview_url) {
          projectRenderOutputUrl = clip.render.preview_url;
          if (clip.render.status === "success") {
            projectWorkbenchRenderStatus = "success";
            setProjectRenderVisibleProgress(1, { complete: true });
            if (selectedWorkflowStage === "render") {
              stateLabel.textContent = "已完成";
              message.textContent = "渲染视频已生成，可预览或下载。";
            }
          } else if (selectedWorkflowStage === "render") {
            stateLabel.textContent = projectRenderStatusCopy(
              clip.render.status,
              clip.render.stage,
            );
            message.textContent = "当前输入已变化；仍可预览或下载上一次已验证的渲染结果。";
          }
        }
      }
    }
    const candidate = activeRenderPath();
    if (!candidate) return false;
    try {
      const response = await fetch(candidate, { method: "HEAD", cache: "no-store" });
      const ready = response.ok;
      if (preview) preview.disabled = !ready;
      if (download) {
        download.href = ready ? candidate : "#";
        download.toggleAttribute("aria-disabled", !ready);
      }
      return ready;
    } catch (error) {
      if (preview) preview.disabled = true;
      if (download) download.toggleAttribute("aria-disabled", true);
      return false;
    }
  }

  if (renderPath) {
    preview?.addEventListener("click", () => {
      if (!previewDialog || !previewVideo) return;
      const candidate = activeRenderPath();
      if (!candidate) return;
      previewVideo.src = `${candidate}${candidate.includes("?") ? "&" : "?"}t=${Date.now()}`;
      if (typeof previewDialog.showModal === "function") previewDialog.showModal();
      else previewDialog.setAttribute("open", "");
    });
    refreshRenderOutputState();
  }
  document.querySelector("#workflowCloseRenderPreview")?.addEventListener("click", () => {
    if (previewVideo) {
      previewVideo.pause();
      previewVideo.removeAttribute("src");
      previewVideo.load();
    }
    previewDialog?.close();
  });

  if (debugEnabled) {
    document.querySelectorAll(".dev-only-control").forEach((node) => node.classList.add("is-debug-visible"));
  }
  initializeWorkbenchTheme();
  loadWorkflowSuggestions();
  function selectInitialWorkflowStage() {
    if (projectWorkbenchBootstrapFailed) return;
    const restoredWorkflowStage = sessionStorage.getItem(restoredWorkflowStageKey());
    const durableWorkflowStage = projectWorkbenchSession?.resume_state?.workflow_stage;
    if (stageOrder.includes(durableWorkflowStage)) {
      sessionStorage.removeItem(restoredWorkflowStageKey());
      setWorkflowStage(durableWorkflowStage);
    } else if (stageOrder.includes(restoredWorkflowStage)) {
      sessionStorage.removeItem(restoredWorkflowStageKey());
      setWorkflowStage(restoredWorkflowStage);
    } else if (stageOrder.includes(requestedWorkflowStage)) {
      setWorkflowStage(requestedWorkflowStage);
    } else {
      detectWorkflowStageFromArtifacts().then((stage) => {
        if (!selectedWorkflowStage) setWorkflowStage(stage);
      });
    }
  }
  if (projectWorkbenchToken) {
    projectWorkbenchBootstrapPromise = bootstrapProjectWorkbenchSession();
    projectWorkbenchBootstrapPromise
      .then(() => {
        selectInitialWorkflowStage();
        applyProjectWorkbenchResumePosition();
      })
      .catch((error) => {
        projectWorkbenchBootstrapFailed = true;
        stateLabel.textContent = "会话不可用";
        message.textContent = `项目会话不可用：${error.message}。请返回项目管理页面后重新进入工作台。`;
      });
  } else {
    selectInitialWorkflowStage();
  }
  loadDatasetManifestForUpload();
  blockTrajectoryWorkflowActionsUntilResolved();
  loadManifestBackedTrajectoryWorkflow();
  loadKeyframePlan();
  const resumeVideo = document.querySelector("#sourceVideo");
  resumeVideo?.addEventListener("pause", scheduleProjectWorkbenchResume);
  resumeVideo?.addEventListener("seeked", scheduleProjectWorkbenchResume);
  window.addEventListener("cadscenePtsAuthorityReady", () => {
    applyProjectWorkbenchResumePosition();
    scheduleProjectWorkbenchResume();
  });
  window.addEventListener("cadsceneViewerReady", () => {
    viewerReadyForSfmCameraInit = true;
    maybeAutoApplySfmCameraInit();
  });
  pollJobStatus();
  setInterval(pollJobStatus, 1000);
  setInterval(pollJobLog, 2000);
})();
