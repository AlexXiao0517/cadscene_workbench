(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const dataset = params.get("dataset") || "";
  const runId = params.get("runId") || "";
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
  const trajectoryModeLabel = document.querySelector("#workflowTrajectoryMode");
  let trajectoryWorkflow = null;
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
  function uploadTimestamp() {
    const now = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    return `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;
  }

  function renderTrajectoryWorkflow(workflow) {
    if (!trajectoryModeLabel) return;
    const mode = String(workflow?.trajectory_mode || "sfm_only");
    const implementation = String(workflow?.implementation_status || "ready");
    const copy = {
      sfm_only: "轨迹模式：仅 SfM（可用）",
      srt_sfm_fused: "轨迹模式：SRT + SfM（功能待启用）",
      srt_full_pose: "轨迹模式：SRT 完整姿态（功能待启用）",
    };
    trajectoryModeLabel.hidden = false;
    trajectoryModeLabel.textContent = copy[mode] || copy.sfm_only;
    trajectoryModeLabel.classList.toggle("interface-only", implementation === "interface_only");
    if (implementation !== "interface_only") return;
    const explanation = "当前 SRT 轨迹能力仅提供界面提示；融合或直接姿态驱动尚未实现，因此不会启动相关流程。";
    trajectoryModeLabel.title = explanation;
    document.querySelectorAll("[data-job-action], #workflowGenerateKeyframes, #workflowContinueKeyframes, #workflowFinishKeyframes, #workflowFinishQuality, #workflowReturnKeyframes").forEach((button) => {
      button.disabled = true;
      button.title = explanation;
    });
    if (message) message.textContent = explanation;
  }

  function isInterfaceOnlyTrajectoryWorkflow() {
    return trajectoryWorkflow?.implementation_status === "interface_only";
  }

  async function loadManifestBackedTrajectoryWorkflow() {
    if (!dataset) return;
    try {
      const response = await fetch(`/api/workflow/dataset-manifest?dataset=${encodeURIComponent(dataset)}`, { cache: "no-store" });
      if (!response.ok) return;
      const payload = await response.json();
      trajectoryWorkflow = payload?.manifest?.workflow || null;
      renderTrajectoryWorkflow(trajectoryWorkflow);
    } catch (error) {
      // Preserve legacy compatibility when a manifest is unavailable.
    }
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
    const taskTitle = latestJobStatus?.status === "running" && operation === "alignment"
      ? "路线拟合"
      : stageTitles[stage];
    title.textContent = `当前任务：${taskTitle}`;
    stateLabel.textContent = stageStatus?.status || "pending";
    progress.value = Number(stageStatus?.progress || 0);
    message.textContent = stageStatus?.error || stageStatus?.message || "等待任务";
    if (stage === "render" && operation === "render" && latestJobStatus?.status === "running") {
      if (renderProgressState) progress.value = renderProgressState.completed / renderProgressState.total;
      message.textContent = "正在渲染";
    } else if (stage === "render" && latestJobStatus?.status !== "running") {
      renderProgressState = null;
    }
    if (
      stage === "keyframes" &&
      !stageStatus?.message &&
      latestJobStatus?.stages?.sfm?.status === "success"
    ) {
      message.textContent = alignmentArtifactReady
        ? "路线已拟合。继续补关键帧，完成后再次「路线拟合」刷新点云/轨迹。"
        : "SfM 已完成，已完成首帧标定后点击「路线拟合」，之后才会显示 CAD-aligned 点云。";
    }
    if (stage === "sfm") {
      const sfmStatus = document.querySelector("#workflowSfmStatus");
      if (sfmStatus && stageStatus?.status === "running") sfmStatus.textContent = stageStatus.message || "SfM 正在运行";
      if (sfmStatus && stageStatus?.status === "failed") sfmStatus.textContent = stageStatus.error || "SfM 运行失败";
      if (sfmStatus && stageStatus?.status === "success") updateSfmSummary();
    }
  }

  function setWorkflowStage(stage) {
    selectedWorkflowStage = stageOrder.includes(stage) ? stage : "upload";
    updateWorkflowStepActive(selectedWorkflowStage);
    renderWorkflowPanel(selectedWorkflowStage);
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
      const backendStage = payload.current_stage || "upload";
      const detectedStage = latestJobStatus?.status !== "running"
        ? await detectWorkflowStageFromArtifacts()
        : "";
      setWorkflowStage(detectedStage || (payload.status === "success" ? nextStageAfterSuccess(backendStage) : backendStage));
    } else {
      renderWorkflowPanel(selectedWorkflowStage);
    }
    // SfM 重建成功后，自动读取 SfM 的 FOV 作为虚拟相机初值，无需用户手动触发。
    // applySfmCameraInitializationOnce 内部用 sessionStorage 去重，重复调用安全。
    if (stages.sfm?.status === "success") {
      const sfmSuitable = await updateSfmSummary();
      if (sfmSuitable !== false) maybeAutoApplySfmCameraInit();
    }
    const isRunning = payload.status === "running";
    const operation = payload.operation || payload.current_stage;
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
      button.disabled = isRunning || isInterfaceOnlyTrajectoryWorkflow();
    });
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
        }
        sessionStorage.removeItem(postAlignmentStageKey());
      }
      window.location.reload();
    } else if (pendingReload && ["failed", "cancelled"].includes(payload.status)) {
      sessionStorage.removeItem(reloadKey);
      sessionStorage.removeItem(postAlignmentStageKey());
    }
  }

  function maybeAutoApplySfmCameraInit() {
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
    if (!(cadScale > 0) || !Number.isFinite(originX) || !Number.isFinite(originY)) {
      throw new Error("CAD scale / origin XY 参数无效");
    }
    const result = await apiPost("/api/workflow/create-dataset", {
      dataset: requestedDataset,
      runId: uploadRunId,
      cadScale,
      originX,
      originY,
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
    if (isInterfaceOnlyTrajectoryWorkflow()) {
      throw new Error("轨迹功能尚未启用，当前模式不能启动处理流程。");
    }
    if (!dataset || !runId) {
      throw new Error("请先通过 URL 指定 dataset 和 runId。");
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

  function updateKeyframePlanUi() {
    const alignmentButton = document.querySelector("#workflowRunAlignment");
    const continueButton = document.querySelector("#workflowContinueKeyframes");
    const finishButton = document.querySelector("#workflowFinishKeyframes");
    const planStatus = document.querySelector("#workflowKeyframePlanStatus");
    const pending = Number(keyframePlan?.pending_count || 0);
    const completed = Number(keyframePlan?.completed_count || 0);
    const total = Array.isArray(keyframePlan?.frames) ? keyframePlan.frames.length : 0;
    if (alignmentButton) alignmentButton.textContent = keyframePlan ? "重新路线拟合" : "路线拟合";
    if (continueButton) continueButton.disabled = isInterfaceOnlyTrajectoryWorkflow() || keyframeSaveInFlight || !keyframePlan || pending === 0;
    if (finishButton) finishButton.disabled = isInterfaceOnlyTrajectoryWorkflow() || keyframeSaveInFlight || !keyframePlan || pending > 0;
    if (planStatus) {
      planStatus.textContent = keyframePlan
        ? `计划：已完成 ${completed}/${total}，待标定 ${pending}。待标定帧不会计入人工关键帧。`
        : "尚未生成关键帧计划；初步路线拟合后可按间隔生成。";
    }
    if (typeof window.cadsceneSetKeyframePlan === "function") {
      window.cadsceneSetKeyframePlan(keyframePlan);
    }
  }

  async function loadKeyframePlan() {
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
    if (!alignmentArtifactReady) {
      throw new Error("请先完成首帧/初始关键帧的路线拟合，再生成关键帧计划。");
    }
    await saveCurrentCameraTrack();
    const intervalFrames = Number(document.querySelector("#workflowKeyframeStep")?.value || 180);
    const result = await apiPost("/api/workflow/generate-keyframe-plan", {
      dataset,
      runId,
      intervalFrames,
    });
    keyframePlan = result.plan;
    updateKeyframePlanUi();
    const pending = Number(keyframePlan.pending_count || 0);
    message.textContent = `已生成 ${intervalFrames} 帧间隔的关键帧计划：${pending} 个待标定帧。`;
    jumpToNextPendingKeyframe();
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
    const manualAnchorCount = (cameraTrack.keyframes || []).filter(
      (keyframe) => keyframe?.camera && keyframe.source !== "algorithm_prediction",
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

  async function startRenderStage() {
    await saveCurrentCameraTrack();
    message.textContent = "正在用最新人工关键帧重新拟合路线并渲染。";
    return runStage("render");
  }

  async function cancelRunningJob() {
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
      progress.value = Math.min(1, completed / total);
      message.textContent = "正在渲染";
      return;
    }
  }

  async function pollJobLog() {
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
    const key = `cadsceneSfmCameraInit:v2:${dataset}:${runId}`;
    if (sessionStorage.getItem(key) === "1") return;
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
    sessionStorage.setItem(key, "1");
    message.textContent = params.orientation_safe_to_apply
      ? `已使用 SfM 第 ${params.frame_index} 帧初始化 ${appliedFields.join("/")}；位置坐标保持不变。`
      : `已使用 SfM 初始化 FOV=${params.fov.toFixed(1)}°；yaw/pitch/roll 将在首个人工锚点后确定。`;
    return true;
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
  // 路线拟合放在关键帧标定阶段：先保存当前关键帧，再启动 alignment job。
  document.querySelector("#workflowRunAlignment")?.addEventListener("click", () => runWithMessage(startAlignmentStage));
  // 质量检测只启动 quality job（不再包含路线拟合）。
  document.querySelector("#workflowRunQuality")?.addEventListener("click", () => runWithMessage(startQualityStage));
  document.querySelector("#workflowRerunQuality")?.addEventListener("click", () => runWithMessage(startQualityStage));
  document.querySelector("#workflowFinishQuality")?.addEventListener("click", () => setWorkflowStage("render"));
  document.querySelector("#workflowReturnKeyframes")?.addEventListener("click", () => setWorkflowStage("keyframes"));
  document.querySelector("#workflowRender")?.addEventListener("click", () => runWithMessage(startRenderStage));
  document.querySelector("#workflowCancel")?.addEventListener("click", () => runWithMessage(cancelRunningJob));
  document.querySelector("#workflowFinishKeyframes")?.addEventListener("click", () => runWithMessage(finishKeyframePlan));
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

  async function refreshRenderOutputState() {
    if (!renderPath) return false;
    try {
      const response = await fetch(renderPath, { method: "HEAD", cache: "no-store" });
      const ready = response.ok;
      if (preview) preview.disabled = !ready;
      if (download) {
        download.href = ready ? renderPath : "#";
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
      previewVideo.src = `${renderPath}${renderPath.includes("?") ? "&" : "?"}t=${Date.now()}`;
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
  loadWorkflowSuggestions();
  loadDatasetManifestForUpload();
  loadManifestBackedTrajectoryWorkflow();
  loadKeyframePlan();
  const restoredWorkflowStage = sessionStorage.getItem(restoredWorkflowStageKey());
  if (stageOrder.includes(restoredWorkflowStage)) {
    sessionStorage.removeItem(restoredWorkflowStageKey());
    setWorkflowStage(restoredWorkflowStage);
  } else {
    detectWorkflowStageFromArtifacts().then((stage) => {
      if (!selectedWorkflowStage) setWorkflowStage(stage);
    });
  }
  window.addEventListener("cadsceneViewerReady", maybeAutoApplySfmCameraInit);
  pollJobStatus();
  setInterval(pollJobStatus, 1000);
  setInterval(pollJobLog, 2000);
})();
