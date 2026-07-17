(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const dataset = params.get("dataset") || "";
  const runId = params.get("runId") || "";
  const debugEnabled = params.get("debug") === "1" || window.VIEWER_DEBUG === true;
  const nativeFetch = window.fetch.bind(window);
  let runningObservedAt = null;

  function sfmOptions() {
    return {
      backend: document.querySelector("#workflowSfmBackendSelect")?.value || "pycolmap",
      device: document.querySelector("#workflowSfmDeviceSelect")?.value || "cpu",
      gpu_index: document.querySelector("#workflowSfmGpuIndex")?.value || "0",
      no_cpu_fallback: Boolean(document.querySelector("#workflowSfmNoCpuFallback")?.checked),
    };
  }

  // 保留旧 workflow.js，只在 SfM run-stage 请求发出前补充受控参数。
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : input?.url || "";
    if (url.includes("/api/workflow/run-stage") && init?.method === "POST" && init.body) {
      try {
        const payload = JSON.parse(init.body);
        if (payload.stage === "sfm") {
          payload.options = { ...(payload.options || {}), ...sfmOptions() };
          init = { ...init, body: JSON.stringify(payload) };
        }
      } catch (error) {
        console.warn("[cadscene sfm backend] 无法补充 SfM 后端参数", error);
      }
    }
    return nativeFetch(input, init);
  };

  function updateRuntime(payload) {
    const sfm = payload?.stages?.sfm || {};
    const backend = document.querySelector("#workflowSfmBackend");
    const device = document.querySelector("#workflowSfmDevice");
    const substage = document.querySelector("#workflowSfmSubstage");
    const elapsed = document.querySelector("#workflowSfmElapsed");
    if (sfm.status === "running") {
      if (!runningObservedAt) runningObservedAt = Date.now();
      const selection = String(sfm.message || "").match(/后端=([^，]+)，设备=([^，]+)/);
      if (selection && backend) backend.textContent = selection[1];
      if (selection && device) device.textContent = selection[2];
      if (substage) substage.textContent = sfm.message || "运行中";
      if (elapsed) elapsed.textContent = `${Math.round((Date.now() - runningObservedAt) / 1000)} 秒`;
    } else {
      runningObservedAt = null;
    }
  }

  async function updateCompletedStats() {
    if (!dataset || !runId) return;
    try {
      const response = await nativeFetch(
        `/runs/${encodeURIComponent(dataset)}/${encodeURIComponent(runId)}/02_sfm/sfm_stats.json?t=${Date.now()}`,
        { cache: "no-store" },
      );
      if (!response.ok) return;
      const stats = await response.json();
      const backend = document.querySelector("#workflowSfmBackend");
      const device = document.querySelector("#workflowSfmDevice");
      const substage = document.querySelector("#workflowSfmSubstage");
      const elapsed = document.querySelector("#workflowSfmElapsed");
      if (backend) backend.textContent = stats.backend || "未知";
      if (device) device.textContent = stats.effective_device === "cuda"
        ? `GPU ${stats.gpu_index ?? 0} ${stats.gpu_name || ""}`.trim()
        : (stats.effective_device || "未验证");
      if (substage) substage.textContent = "已完成";
      if (elapsed) elapsed.textContent = `${Number(stats.elapsed_sec || 0).toFixed(1)} 秒`;
    } catch (error) {
      // SfM 尚未完成时 stats 不存在，不影响旧工作流。
    }
  }

  async function pollRuntime() {
    if (!dataset || !runId) return;
    try {
      const response = await nativeFetch(
        `/runs/${encodeURIComponent(dataset)}/${encodeURIComponent(runId)}/job_status.json?t=${Date.now()}`,
        { cache: "no-store" },
      );
      if (response.ok) updateRuntime(await response.json());
    } catch (error) {
      // job_status 可选。
    }
    updateCompletedStats();
  }

  if (debugEnabled) {
    document.querySelectorAll(".dev-only-control").forEach((node) => node.classList.add("is-debug-visible"));
  }
  pollRuntime();
  window.setInterval(pollRuntime, 1000);
})();
