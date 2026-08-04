(function () {
  "use strict";

  const debugEnabled = new URLSearchParams(window.location.search).get("debug") === "1"; // debug=1
  const modeCopy = {
    sfm_only: ["仅 SfM", "将使用已可用的 SfM 轨迹流程。"],
    pure_rotation: ["悬停旋转（实验）", "将使用 OpenGV 恢复相对旋转；相机中心固定，不恢复平移。"],
    srt_sfm_fused: ["SRT + SfM（功能待启用）", "已识别到定位遥测；融合算法尚未启用，不会自动开始。"],
    srt_full_pose: ["SRT 完整姿态（功能待启用）", "已识别到完整姿态字段；直接姿态驱动尚未启用，不会自动开始。"],
  };
  // Kept as compatibility documentation for existing single-video links.
  const legacyRouteCompatibility = [
    "/api/workflow/create-dataset",
    "/api/workflow/upload-video",
    "/api/workflow/upload-cad",
    "/api/workflow/upload-srt",
  ];
  void legacyRouteCompatibility;
  const state = { dataset: "", runId: "", manifest: null, mode: "sfm_only", projectRevision: 0 };
  const $ = (selector) => document.querySelector(selector);

  function generatedId(prefix) {
    const suffix = (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/[^a-z0-9-]/gi, "");
    return `${prefix}-${suffix.toLowerCase()}`;
  }

  function setFileStatus(kind, text, value) {
    $(`#${kind}Status`).textContent = text;
    if (value !== undefined) $(`#${kind}Progress`).value = Math.max(0, Math.min(1, value));
  }

  function setMessage(text) { $("#portalMessage").textContent = text; }

  function updateSelectedFileStatus(kind, required) {
    const file = $(`#portal${kind[0].toUpperCase()}${kind.slice(1)}`).files[0];
    setFileStatus(
      kind,
      file ? "已选择，等待上传" + `：${file.name}` : (required ? "尚未选择" : "不上传也可使用 SfM 工作流"),
      0,
    );
  }

  function updateMotionModeAvailability() {
    const srt = $("#portalSrt").files[0];
    const motionSection = $("#portalMotionMode");
    const hoveringInput = $("#portalHoveringDeclared");
    motionSection.hidden = Boolean(srt);
    hoveringInput.disabled = Boolean(srt);
    if (srt) hoveringInput.checked = false;
  }

  async function postJson(path, payload) {
    const response = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }

  function upload(path, file, kind) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const body = new FormData();
      body.append("file", file, file.name);
      xhr.open("POST", path);
      xhr.responseType = "json";
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) setFileStatus(kind, `正在上传 ${file.name}`, event.loaded / event.total);
      });
      xhr.addEventListener("load", () => {
        const result = xhr.response || {};
        if (xhr.status >= 200 && xhr.status < 300) resolve(result);
        else reject(new Error(result.error || `HTTP ${xhr.status}`));
      });
      xhr.addEventListener("error", () => reject(new Error("上传连接失败")));
      xhr.send(body);
    });
  }

  async function loadManifest() {
    const response = await fetch(`/api/workflow/dataset-manifest?dataset=${encodeURIComponent(state.dataset)}`, { cache: "no-store" });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "无法读取项目状态");
    state.manifest = result.manifest;
    state.mode = state.manifest?.workflow?.trajectory_mode || "sfm_only";
  }

  function showResult() {
    const copy = modeCopy[state.mode] || modeCopy.sfm_only;
    $("#portalResult").hidden = false;
    $("#portalMode").textContent = `检测模式：${copy[0]}`;
    $("#portalSummary").textContent = copy[1];
    $("#portalIdentifiers").textContent = `dataset=${state.dataset}; runId=${state.runId}`;
    $("#portalEnter").disabled = !(state.manifest?.video?.url && state.manifest?.cad?.status === "ready");
  }

  async function submit(event) {
    event.preventDefault();
    const video = $("#portalVideo").files[0];
    const cad = $("#portalCad").files[0];
    const srt = $("#portalSrt").files[0];
    const hoveringDeclared = Boolean(!srt && $("#portalHoveringDeclared").checked);
    if (!video || !cad) { setMessage("请先选择视频和 CAD 文件。"); return; }
    $("#portalSubmit").disabled = true;
    state.dataset = generatedId("dataset");
    state.runId = generatedId("run");
    state.projectRevision = 0;
    state.manifest = null;
    try {
      setMessage("正在创建项目…");
      await postJson("/api/projects", { project_id: state.dataset, hoveringDeclared });
      const videoResult = await upload(`/api/projects/${encodeURIComponent(state.dataset)}/uploads/video?expectedRevision=${state.projectRevision}`, video, "video");
      state.projectRevision = videoResult.project_revision;
      setFileStatus("video", "视频已上传", 1);
      if (srt) {
        const srtResult = await upload(`/api/projects/${encodeURIComponent(state.dataset)}/uploads/srt?expectedRevision=${state.projectRevision}`, srt, "srt");
        state.projectRevision = srtResult.project_revision;
        state.mode = srtResult.trajectory_mode || state.mode;
        setFileStatus("srt", "SRT 已上传并完成检测", 1);
      }
      const cadResult = await upload(`/api/projects/${encodeURIComponent(state.dataset)}/uploads/cad?expectedRevision=${state.projectRevision}`, cad, "cad");
      state.projectRevision = cadResult.project_revision;
      setFileStatus("cad", "CAD 已上传并进入分析队列", 1);
      setMessage("上传完成，正在进入项目片段管理。");
      const target = new URL("/apps/project_workspace/", window.location.origin);
      target.searchParams.set("projectId", state.dataset);
      window.location.assign(target.toString());
    } catch (error) {
      setMessage(`上传失败：${error.message}`);
    } finally { $("#portalSubmit").disabled = false; }
  }

  $("#portalForm").addEventListener("submit", submit);
  ["video", "cad", "srt"].forEach((kind) => {
    $(`#portal${kind[0].toUpperCase()}${kind.slice(1)}`).addEventListener("change", () => {
      updateSelectedFileStatus(kind, kind !== "srt");
      if (kind === "srt") updateMotionModeAvailability();
    });
  });
  updateMotionModeAvailability();
  $("#portalEnter").addEventListener("click", () => {
    const mode = debugEnabled && $("#portalModeOverride").value ? $("#portalModeOverride").value : state.mode;
    const target = new URL("/apps/web_camera_viewer/", window.location.origin);
    target.searchParams.set("dataset", state.dataset);
    target.searchParams.set("runId", state.runId);
    target.searchParams.set("trajectoryMode", mode);
    target.searchParams.set("video", state.manifest.video.url);
    target.searchParams.set("cad", state.manifest.cad.url);
    target.searchParams.set("cadScale", String(state.manifest.defaults.cad_scale));
    target.searchParams.set("originXY", state.manifest.defaults.origin_xy.join(","));
    if (debugEnabled) target.searchParams.set("debug", "1");
    window.location.assign(target.toString());
  });
  if (debugEnabled) document.documentElement.classList.add("debug-enabled");
})();
