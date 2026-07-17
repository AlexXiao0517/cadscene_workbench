(function () {
  "use strict";

  const debugEnabled = new URLSearchParams(window.location.search).get("debug") === "1"; // debug=1
  const modeCopy = {
    sfm_only: ["仅 SfM", "将使用已可用的 SfM 轨迹流程。"],
    srt_sfm_fused: ["SRT + SfM（功能待启用）", "已识别到定位遥测；融合算法尚未启用，不会自动开始。"],
    srt_full_pose: ["SRT 完整姿态（功能待启用）", "已识别到完整姿态字段；直接姿态驱动尚未启用，不会自动开始。"],
  };
  const state = { dataset: "", runId: "", manifest: null, mode: "sfm_only" };
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
    if (!video || !cad) { setMessage("请先选择视频和 CAD 文件。"); return; }
    $("#portalSubmit").disabled = true;
    state.dataset = generatedId("dataset");
    state.runId = generatedId("run");
    try {
      setMessage("正在创建项目…");
      await postJson("/api/workflow/create-dataset", { dataset: state.dataset, runId: state.runId, cadScale: 0.06, originX: 567747.5756295, originY: 3330464.2234675 });
      await upload(`/api/workflow/upload-video?dataset=${encodeURIComponent(state.dataset)}&runId=${encodeURIComponent(state.runId)}`, video, "video");
      setFileStatus("video", "视频已上传", 1);
      await upload(`/api/workflow/upload-cad?dataset=${encodeURIComponent(state.dataset)}&runId=${encodeURIComponent(state.runId)}`, cad, "cad");
      setFileStatus("cad", "CAD 已上传并解析", 1);
      if (srt) {
        const srtResult = await upload(`/api/workflow/upload-srt?dataset=${encodeURIComponent(state.dataset)}&runId=${encodeURIComponent(state.runId)}`, srt, "srt");
        state.mode = srtResult.trajectory_mode || state.mode;
        setFileStatus("srt", "SRT 已上传并完成检测", 1);
      }
      await loadManifest();
      showResult();
      setMessage("上传完成，可进入项目。");
    } catch (error) {
      setMessage(`上传失败：${error.message}`);
    } finally { $("#portalSubmit").disabled = false; }
  }

  $("#portalForm").addEventListener("submit", submit);
  $("#portalEnter").addEventListener("click", () => {
    const mode = debugEnabled && $("#portalModeOverride").value ? $("#portalModeOverride").value : state.mode;
    const target = new URL("/apps/web_camera_viewer/", window.location.origin);
    target.searchParams.set("dataset", state.dataset);
    target.searchParams.set("runId", state.runId);
    target.searchParams.set("trajectoryMode", mode);
    if (debugEnabled) target.searchParams.set("debug", "1");
    window.location.assign(target.toString());
  });
  if (debugEnabled) document.documentElement.classList.add("debug-enabled");
})();
