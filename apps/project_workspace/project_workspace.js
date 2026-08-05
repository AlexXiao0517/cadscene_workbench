(function () {
  "use strict";

  const POLL_INTERVAL_MS = 1500;
  const params = new URLSearchParams(window.location.search);
  const projectId = params.get("projectId") || "";
  const focusClipId = params.get("focusClip") || "";
  const state = {
    snapshot: null,
    etag: null,
    polling: false,
    dirtyEdits: new Map(),
    selectedClipIds: new Set(),
    pendingPreflight: null,
  };
  const dirtyEdits = state.dirtyEdits;
  const selectedClipIds = state.selectedClipIds;
  const $ = (selector, root = document) => root.querySelector(selector);

  function setMessage(message, isError = false) {
    const element = $("#liveMessage");
    element.textContent = message || "";
    element.style.color = isError ? "var(--danger)" : "var(--green)";
  }

  async function request(path, options = {}) {
    const response = await fetch(path, options);
    if (response.status === 304) return { response, body: null };
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return { response, body };
  }

  function effectiveEdit(clip) {
    return dirtyEdits.get(clip.clip_id) || {
      workflow: clip.workflow_override,
      name: clip.custom_display_name,
    };
  }

  function effectiveDisplayName(clip) {
    if (!dirtyEdits.has(clip.clip_id)) return clip.display_name;
    const edit = dirtyEdits.get(clip.clip_id);
    return edit.name === null ? clip.generated_display_name : edit.name;
  }

  function visibleWorkflowChoice(clip, edit) {
    const workflow = edit.workflow || clip.resolved_workflow;
    return workflow === "pure_rotation" ? "pure_rotation" : "sfm_only";
  }

  function applyCapabilities(clip, row) {
    const capabilities = clip.capabilities || {};
    $(".open-workbench", row).disabled = !capabilities.can_open_workbench;
    $(".retry-job", row).disabled = !capabilities.can_retry;
    $(".cancel-job", row).disabled = !capabilities.can_cancel;
    $(".workflow-select", row).title = capabilities.reason || "";
  }

  function renderRow(clip) {
    const row = $("#clipRowTemplate").content.firstElementChild.cloneNode(true);
    row.dataset.clipId = clip.clip_id;
    const edit = effectiveEdit(clip);
    const checkbox = $(".clip-select", row);
    checkbox.checked = selectedClipIds.has(clip.clip_id);
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedClipIds.add(clip.clip_id);
      else selectedClipIds.delete(clip.clip_id);
    });
    const nameButton = $(".clip-name", row);
    nameButton.textContent = effectiveDisplayName(clip);
    nameButton.addEventListener("click", () => renameClip(clip, row));
    $(".review-badge", row).hidden = !clip.needs_review;
    $(".time-range", row).textContent = clip.time_range;
    $(".duration", row).textContent = clip.duration;
    $(".motion-mode", row).textContent = clip.detected_motion_mode;
    $(".confidence", row).textContent = clip.confidence == null ? "" : `置信度 ${Math.round(clip.confidence * 100)}%`;
    $(".workflow-recommendation", row).textContent = clip.recommended_workflow || "需人工确认";
    const workflow = $(".workflow-select", row);
    workflow.value = visibleWorkflowChoice(clip, edit);
    workflow.classList.toggle("local-dirty", dirtyEdits.has(clip.clip_id));
    workflow.addEventListener("change", () => saveWorkflow(clip, workflow, row));
    $(".status-pill", row).textContent = clip.status || "ready";
    const thumbnail = $(".clip-thumbnail img", row);
    thumbnail.src = clip.thumbnail_url || "";
    thumbnail.hidden = !clip.thumbnail_url;
    const progressTrack = $(".progress-track", row);
    const progressFill = $(".progress-fill", row);
    const fraction = clip.progress?.fraction;
    const active = ["preparing", "running", "validating"].includes(clip.status);
    progressTrack.hidden = !active;
    progressTrack.classList.toggle("progress-indeterminate", active && typeof fraction !== "number");
    progressFill.style.width = typeof fraction === "number" ? `${Math.round(fraction * 100)}%` : "42%";
    applyCapabilities(clip, row);
    $(".open-workbench", row).addEventListener("click", () => openWorkbench(clip, row));
    $(".retry-job", row).addEventListener("click", () => runJobAction(clip, "retry"));
    $(".cancel-job", row).addEventListener("click", () => runJobAction(clip, "cancel"));
    return row;
  }

  function renderSnapshot(snapshot) {
    state.snapshot = snapshot;
    $("#sidebarProjectName").textContent = snapshot.display_name || snapshot.project_id;
    $("#projectBreadcrumb").textContent = `${snapshot.display_name || snapshot.project_id} · ${snapshot.project_state}`;
    const assets = snapshot.assets || {};
    for (const [kind, nameId, metaId] of [["video", "sourceVideoName", "sourceVideoMeta"], ["cad", "cadName", "cadMeta"], ["srt", "srtName", "srtMeta"]]) {
      const asset = assets[kind];
      if (!asset) continue;
      $(`#${nameId}`).textContent = asset.original_filename || kind;
      $(`#${metaId}`).textContent = asset.size_bytes ? `${(asset.size_bytes / 1048576).toFixed(1)} MB` : "已验证";
    }
    const sourceThumb = $("#sourceVideoThumbnail");
    sourceThumb.src = assets.video?.thumbnail_url || "";
    sourceThumb.hidden = !assets.video?.thumbnail_url;
    $("#clipCount").textContent = snapshot.clips.length;
    $("#pendingCount").textContent = snapshot.clips.filter((clip) => ["ready", "queued"].includes(clip.status)).length;
    $("#runningCount").textContent = snapshot.clips.filter((clip) => ["preparing", "running", "validating"].includes(clip.status)).length;
    $("#reanalyzeButton").disabled = !snapshot.capabilities.can_reanalyze;
    $("#batchTrajectoryButton").disabled = !snapshot.capabilities.can_start_trajectory;
    $("#batchRenderButton").disabled = !snapshot.capabilities.can_render;
    $("#mergeProjectButton").disabled = !snapshot.capabilities.can_merge;
    const rows = $("#clipRows");
    rows.replaceChildren(...snapshot.clips.map(renderRow));
    if (focusClipId) {
      const focused = rows.querySelector(`[data-clip-id="${CSS.escape(focusClipId)}"]`);
      if (focused) {
        focused.classList.add("is-focused-return");
        focused.scrollIntoView({ block: "center", behavior: "smooth" });
      }
    }
    const timeline = $("#mergeTimeline");
    timeline.replaceChildren(...snapshot.clips.map((clip) => {
      const item = document.createElement("span");
      item.className = "timeline-clip";
      item.textContent = `${effectiveDisplayName(clip)} · ${clip.duration}`;
      return item;
    }));
  }

  async function pollSnapshot() {
    if (!projectId || state.polling || document.hidden) return;
    state.polling = true;
    try {
      const headers = {};
      if (state.etag) headers["If-None-Match"] = state.etag;
      const { response, body } = await request(`/api/projects/${encodeURIComponent(projectId)}/snapshot`, { headers, cache: "no-store" });
      if (response.status === 304) return;
      state.etag = response.headers.get("ETag");
      renderSnapshot(body);
      $("#pollStatus").textContent = "刚刚同步";
    } catch (error) {
      $("#pollStatus").textContent = "同步中断";
      setMessage(error.message, true);
    } finally {
      state.polling = false;
    }
  }

  async function saveWorkflow(clip, select, row) {
    const workflow = select.value || null;
    dirtyEdits.set(clip.clip_id, { ...effectiveEdit(clip), workflow });
    select.classList.add("local-dirty");
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/workflow`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: state.snapshot.component_revisions.clips,
          workflow_override: workflow,
        }),
      });
      // This literal documents the reset payload used by the recommendation option.
      const resetToRecommendation = { workflow_override: null };
      void resetToRecommendation;
      dirtyEdits.delete(clip.clip_id);
      state.snapshot.component_revisions.clips = body.clips_revision;
      state.etag = null;
      await pollSnapshot();
    } catch (error) {
      $(".row-error", row).textContent = error.message;
    }
  }

  async function renameClip(clip, row) {
    const current = effectiveEdit(clip).name || clip.display_name;
    const entered = window.prompt("修改片段名称；留空可恢复系统名称", current);
    if (entered === null) return;
    const customName = entered.trim() || null;
    dirtyEdits.set(clip.clip_id, { ...effectiveEdit(clip), name: customName });
    $(".clip-name", row).textContent = customName || clip.generated_display_name;
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/name`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: state.snapshot.component_revisions.clips,
          custom_display_name: customName,
        }),
      });
      dirtyEdits.delete(clip.clip_id);
      state.snapshot.component_revisions.clips = body.clips_revision;
      state.etag = null;
      await pollSnapshot();
    } catch (error) {
      $(".row-error", row).textContent = error.message;
    }
  }

  async function preflightBatch(kind = "trajectory") {
    const clipIds = selectedClipIds.size ? [...selectedClipIds] : state.snapshot.clips.map((clip) => clip.clip_id);
    const endpoint = kind === "render" ? "render-jobs" : "trajectory-jobs";
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: state.snapshot.component_revisions.jobs, clip_ids: clipIds, enqueue: false }),
      });
      const needsConfirmation = body.needs_confirmation || body.confirmation_required || [];
      state.pendingPreflight = { clipIds, result: body, endpoint, needsConfirmation };
      $("#preflightResult").innerHTML = `<p>可入队：${body.eligible.length} 个</p><p>需确认：${needsConfirmation.length} 个</p><p>已跳过：${body.skipped.length} 个</p>`;
      const items = $("#preflightItems");
      items.replaceChildren(...clipIds.map((clipId) => {
        const label = document.createElement("label");
        const category = body.eligible.includes(clipId) ? "可入队" : (needsConfirmation.includes(clipId) ? "需确认" : "已跳过");
        const reason = body.reasons[clipId] || "检查通过";
        if (needsConfirmation.includes(clipId)) {
          const checkbox = document.createElement("input");
          checkbox.type = "checkbox";
          checkbox.dataset.confirmClipId = clipId;
          label.append(checkbox);
        }
        label.append(document.createTextNode(`${clipId} · ${category} · ${reason}`));
        return label;
      }));
      $("#preflightDialog").showModal();
    } catch (error) { setMessage(error.message, true); }
  }

  async function enqueuePreflight(event) {
    event.preventDefault();
    const pending = state.pendingPreflight;
    if (!pending) return;
    const confirmedClipIds = [...document.querySelectorAll("[data-confirm-clip-id]:checked")]
      .map((item) => item.dataset.confirmClipId);
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/${pending.endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: state.snapshot.component_revisions.jobs,
          clip_ids: pending.clipIds,
          confirmed_clip_ids: confirmedClipIds,
          enqueue: true,
        }),
      });
      state.pendingPreflight = null;
      $("#preflightDialog").close();
      state.snapshot.component_revisions.jobs = body.jobs_revision;
      state.etag = null;
      setMessage(`已将 ${body.enqueued_clip_ids.length} 个片段加入资源队列`);
      await pollSnapshot();
    } catch (error) {
      setMessage(error.message, true);
    }
  }

  async function reanalyzeProject() {
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/analysis/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: state.snapshot.component_revisions.project }),
      });
      state.snapshot.component_revisions.project = body.project_revision;
      state.etag = null;
      setMessage("已提交重新分析");
      await pollSnapshot();
    } catch (error) { setMessage(error.message, true); }
  }

  async function runJobAction(clip, action) {
    if (!clip.job_id) return;
    const path = action === "retry"
      ? `/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/retry`
      : `/api/projects/${encodeURIComponent(projectId)}/jobs/${encodeURIComponent(clip.job_id)}/cancel`;
    try {
      const { body } = await request(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: state.snapshot.component_revisions.jobs }),
      });
      state.snapshot.component_revisions.jobs = body.jobs_revision;
      state.etag = null;
      await pollSnapshot();
    } catch (error) { setMessage(error.message, true); }
  }

  async function openWorkbench(clip, row) {
    const returnParams = new URLSearchParams({
      projectId,
      focusClip: clip.clip_id,
    });
    try {
      const { body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/workbench-sessions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.clips,
            return_to: `/apps/project_workspace/?${returnParams.toString()}`,
          }),
        },
      );
      state.snapshot.component_revisions.clips = body.clips_revision;
      window.location.assign(body.workbench_url);
    } catch (error) {
      $(".row-error", row).textContent = error.message;
    }
  }

  $("#sidebarToggle").addEventListener("click", (event) => {
    const collapsed = $("#appShell").classList.toggle("sidebar-collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
    event.currentTarget.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
  });
  $("#selectAll").addEventListener("change", (event) => {
    for (const clip of state.snapshot?.clips || []) {
      if (event.currentTarget.checked) selectedClipIds.add(clip.clip_id);
      else selectedClipIds.delete(clip.clip_id);
    }
    if (state.snapshot) renderSnapshot(state.snapshot);
  });
  $("#batchTrajectoryButton").addEventListener("click", () => preflightBatch("trajectory"));
  $("#batchRenderButton").addEventListener("click", () => preflightBatch("render"));
  $("#reanalyzeButton").addEventListener("click", reanalyzeProject);
  $("#confirmPreflight").addEventListener("click", enqueuePreflight);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSnapshot(); });
  if (!projectId) setMessage("缺少项目标识，无法载入工作区。", true);
  else pollSnapshot();
  window.setInterval(pollSnapshot, POLL_INTERVAL_MS);
})();
