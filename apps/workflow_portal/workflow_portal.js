import { createProject, getSnapshot, retryAnalysis, uploadAsset } from "./portal_api.js?v=20260806-upload-v2";

const debugEnabled = new URLSearchParams(window.location.search).get("debug") === "1"; // debug=1
const THEME_STORAGE_KEY = "mediaflow-theme";
const legacyRouteCompatibility = [
  "/api/workflow/create-dataset", "/api/workflow/upload-video",
  "/api/workflow/upload-cad", "/api/workflow/upload-srt", "/snapshot",
];
void legacyRouteCompatibility;

const state = {
  projectId: "", dataset: "", projectRevision: 0, manifest: null,
  projectPromise: null, files: {}, uploads: {},
  completed: { video: false, cad: false, srt: false }, etag: "", snapshot: null,
  overlayDismissed: false, analysisComplete: false,
};
const $ = (selector) => document.querySelector(selector);

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const toggle = $("#themeToggle");
  if (!toggle) return;
  const isLight = theme === "light";
  toggle.setAttribute("aria-pressed", String(isLight));
  toggle.setAttribute("aria-label", isLight ? "切换为黑夜模式" : "切换为白天模式");
  toggle.querySelector("span").textContent = isLight ? "黑夜模式" : "白天模式";
}

function initializeTheme() {
  const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
  const preferred = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
  applyTheme(saved === "light" || saved === "dark" ? saved : preferred);
}

function generatedId() {
  const suffix = (window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`).replace(/[^a-z0-9-]/gi, "");
  return `dataset-${suffix.toLowerCase()}`;
}
function fileSize(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
function setMessage(message, error = false) {
  $("#portalMessage").textContent = message;
  $("#portalMessage").classList.toggle("is-error", error);
}
function ensureProject(file) {
  if (state.projectPromise) return state.projectPromise;
  state.projectId = generatedId();
  state.dataset = state.projectId;
  state.projectRevision = 0;
  state.manifest = null;
  const displayName = file.name.replace(/\.[^.]+$/, "") || "新建视频项目";
  state.projectPromise = createProject(state.projectId, displayName);
  return state.projectPromise;
}
function validateFile(kind, file) {
  const extension = file.name.toLowerCase().split(".").pop();
  if (kind === "video" && extension !== "mp4") throw new Error("视频仅支持 MP4 格式");
  if (kind === "cad" && extension !== "dxf") throw new Error("CAD 图纸仅支持 DXF 格式");
  if (kind === "srt" && extension !== "srt") throw new Error("遥测文件仅支持 SRT 格式");
}
function setUploadVisual(kind, loaded, total, acknowledged) {
  const exactPercent = total > 0 ? Math.floor((loaded / total) * 100) : 0;
  const percent = acknowledged ? 100 : Math.min(99, exactPercent);
  $(`#${kind}Progress`).style.width = `${percent}%`;
  $(`#${kind}Percent`).textContent = `${percent}%`;
  if (kind !== "srt") {
    $(`#${kind}Status`).textContent = acknowledged ? "上传完成" : (exactPercent >= 100 ? "文件已传输，正在确认" : `正在上传 ${fileSize(loaded)} / ${fileSize(total)}`);
  }
}
function updateCreateAvailability() {
  $("#portalSubmit").disabled = !(state.completed.video && state.completed.cad);
}
function showSelectedFile(kind, file) {
  if (kind === "srt") {
    $("#srtStatus").textContent = `${file.name} · 准备上传`;
    $("#srtProgressWrap").hidden = false;
    return;
  }
  const preview = $(`#${kind}Preview`);
  preview.hidden = false;
  preview.querySelector(".file-name").textContent = file.name;
  preview.querySelector(".file-meta").textContent = fileSize(file.size);
  const pane = $(`[data-upload-kind="${kind}"]`);
  pane.classList.add("has-file");
  pane.classList.remove("is-complete", "is-error");
  pane.querySelector(".drop-copy").hidden = true;
  pane.querySelector(".upload-glyph").hidden = true;
  $(`#${kind}ProgressWrap`).hidden = false;
  if (kind === "video") {
    const video = preview.querySelector("video");
    if (video.src) URL.revokeObjectURL(video.src);
    video.src = URL.createObjectURL(file);
  }
}
async function startAssetUpload(kind, file) {
  validateFile(kind, file);
  state.files[kind] = file;
  state.completed[kind] = false;
  updateCreateAvailability();
  showSelectedFile(kind, file);
  const pane = kind === "srt" ? null : $(`[data-upload-kind="${kind}"]`);
  if (pane) pane.classList.add("is-uploading");
  if (kind !== "srt") $(`[data-reselect="${kind}"]`).hidden = true;
  try {
    await ensureProject(file);
    const result = await uploadAsset(state.projectId, kind, file, ({ loaded, total, acknowledged }) => {
      setUploadVisual(kind, loaded, total, acknowledged);
    });
    state.completed[kind] = true;
    const progressWrap = kind === "srt" ? $("#srtProgressWrap") : $(`#${kind}ProgressWrap`);
    progressWrap.hidden = true;
    if (kind === "srt") $("#srtStatus").textContent = `${file.name} · 上传完成`;
    else {
      pane.classList.remove("is-uploading");
      pane.classList.add("is-complete");
      $(`[data-reselect="${kind}"]`).hidden = false;
    }
    updateCreateAvailability();
    if (state.completed.video && state.completed.cad) setMessage("文件已上传，可以新建项目。解析进度将在弹窗中显示。");
    return result;
  } catch (error) {
    state.completed[kind] = false;
    if (pane) {
      pane.classList.remove("is-uploading");
      pane.classList.add("is-error");
    }
    updateCreateAvailability();
    setMessage(`${kind === "video" ? "视频" : kind === "cad" ? "CAD" : "SRT"}上传失败：${error.message}`, true);
    throw error;
  }
}
function rememberUpload(kind, file) {
  const task = startAssetUpload(kind, file);
  state.uploads[kind] = task;
  task.catch(() => {});
}

function bindUpload(kind) {
  const input = $(`#portal${kind[0].toUpperCase()}${kind.slice(1)}`);
  input.addEventListener("change", () => {
    const file = input.files[0];
    if (file) rememberUpload(kind, file);
  });
  if (kind === "srt") return;
  const card = $(`[data-upload-kind="${kind}"]`);
  card.addEventListener("dragover", (event) => { event.preventDefault(); card.classList.add("is-dragging"); });
  card.addEventListener("dragleave", () => card.classList.remove("is-dragging"));
  card.addEventListener("drop", (event) => {
    event.preventDefault(); card.classList.remove("is-dragging");
    const file = event.dataTransfer?.files?.[0];
    if (file) rememberUpload(kind, file);
  });
}

function progressOf(job) {
  if (!job) return 0;
  if (job.status === "success") return 100;
  return Math.round(Math.max(0, Math.min(1, Number(job.progress?.fraction || 0))) * 100);
}
function stageMessage(job, fallback) {
  return job?.progress?.message || job?.error || fallback;
}
function renderTaskStage(name, percent, message, active) {
  const element = $(`#taskStage${name}`);
  element.classList.toggle("is-active", active && percent < 100);
  element.classList.toggle("is-complete", percent >= 100);
  element.querySelector("small").textContent = message;
  element.querySelector("b").textContent = `${percent}%`;
  const meter = element.querySelector(".meter span");
  if (meter) meter.style.width = `${percent}%`;
}
function renderAnalysis(snapshot) {
  const jobs = Object.fromEntries((snapshot.analysis?.jobs || []).map((job) => [job.job_type, job]));
  const cadJob = jobs.cad_analysis;
  const videoJob = jobs.video_analysis;
  const cad = progressOf(cadJob);
  const video = progressOf(videoJob);
  const workspace = snapshot.project_state === "ready" && snapshot.clips?.length ? 100 : 0;
  renderTaskStage("Upload", 100, "视频与 CAD 已原子发布", false);
  renderTaskStage("Cad", cad, stageMessage(cadJob, "等待任务启动"), cadJob?.status !== "success");
  renderTaskStage("Video", video, stageMessage(videoJob, cad >= 100 ? "等待任务启动" : "等待 CAD 解析完成"), cad >= 100 && videoJob?.status !== "success");
  renderTaskStage("Workspace", workspace, workspace ? "项目片段已生成" : "等待生成逻辑片段", video >= 100);
  const overall = Math.round(15 + cad * .15 + video * .65 + workspace * .05);
  $("#taskOverallPercent").textContent = `${overall}%`;
  $("#taskCirclePercent").textContent = `${overall}%`;
  $("#taskCircleProgress").style.strokeDashoffset = String(100 - overall);
  $("#taskOverallProgress").style.width = `${overall}%`;
  $("#analysisDetail").textContent = workspace ? "分析完成，正在进入项目片段管理…" : stageMessage(videoJob, stageMessage(cadJob, "任务已进入队列"));
}
function analysisFailure(snapshot) {
  const failed = (snapshot.analysis?.jobs || []).find((job) => ["failed", "interrupted", "cancelled"].includes(job.status));
  if (failed) return failed.error || `${failed.job_type === "cad_analysis" ? "CAD 解析" : "视频分析"}未完成`;
  if (["analysis_failed", "analysis_interrupted", "analysis_cancelled"].includes(snapshot.project_state)) return `项目分析未完成：${snapshot.project_state}`;
  return "";
}
function delay(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }
async function waitForAnalysisCompletion() {
  while (true) {
    const result = await getSnapshot(state.projectId, state.etag);
    state.etag = result.etag;
    if (result.snapshot) state.snapshot = result.snapshot;
    if (!state.snapshot) { await delay(500); continue; }
    renderAnalysis(state.snapshot);
    const failure = analysisFailure(state.snapshot);
    if (failure) throw new Error(failure);
    if (state.snapshot.project_state === "ready" && state.snapshot.clips?.length) return state.snapshot;
    await delay(500);
  }
}
function openWorkspace() {
  const target = new URL("/apps/project_workspace/", window.location.origin);
  target.searchParams.set("projectId", state.dataset);
  window.location.assign(target.toString());
}
async function submit(event) {
  event.preventDefault();
  if (!(state.completed.video && state.completed.cad)) return;
  if (state.analysisComplete) { openWorkspace(); return; }
  $("#portalSubmit").disabled = true;
  $("#analysisOverlay").hidden = false;
  state.overlayDismissed = false;
  $("#analysisError").hidden = true;
  $("#analysisRetry").hidden = true;
  try {
    await Promise.all([state.uploads.video, state.uploads.cad]);
    await waitForAnalysisCompletion();
    state.analysisComplete = true;
    $("#portalSubmit").disabled = false;
    $("#portalSubmit span").textContent = "进入片段管理";
    if (state.overlayDismissed) {
      setMessage("分析完成，可以进入片段管理。");
      return;
    }
    await delay(450);
    openWorkspace();
  } catch (error) {
    $("#analysisError").textContent = `任务停止：${error.message}`;
    $("#analysisError").hidden = false;
    $("#analysisRetry").hidden = false;
    $("#analysisDetail").textContent = "请查看失败原因后重试";
    $("#portalSubmit").disabled = false;
    if (state.overlayDismissed) setMessage(`分析失败：${error.message}`, true);
  }
}

bindUpload("video"); bindUpload("cad"); bindUpload("srt");
initializeTheme();
$("#themeToggle").addEventListener("click", () => {
  const theme = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
  window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  applyTheme(theme);
});
document.querySelectorAll("[data-reselect]").forEach((button) => button.addEventListener("click", () => $(`#portal${button.dataset.reselect[0].toUpperCase()}${button.dataset.reselect.slice(1)}`).click()));
$("#portalForm").addEventListener("submit", submit);
$("#analysisClose").addEventListener("click", () => {
  state.overlayDismissed = true;
  $("#analysisOverlay").hidden = true;
  setMessage("分析仍在后台运行，可继续停留在此页面。完成后可进入片段管理。");
});
$("#analysisRetry").addEventListener("click", async () => {
  $("#analysisRetry").hidden = true; $("#analysisError").hidden = true;
  try {
    await retryAnalysis(state.projectId, state.snapshot.component_revisions.project);
    state.etag = "";
    await waitForAnalysisCompletion();
    state.analysisComplete = true;
    $("#portalSubmit").disabled = false;
    $("#portalSubmit span").textContent = "进入片段管理";
    if (state.overlayDismissed) { setMessage("分析完成，可以进入片段管理。"); return; }
    openWorkspace();
  } catch (error) { $("#analysisError").textContent = `重试失败：${error.message}`; $("#analysisError").hidden = false; $("#analysisRetry").hidden = false; }
});
if (debugEnabled) { document.documentElement.classList.add("debug-enabled"); $("#portalIdentifiers").textContent = "debug=1"; }
