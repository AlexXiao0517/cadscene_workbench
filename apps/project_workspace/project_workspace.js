(function () {
  "use strict";

  const POLL_INTERVAL_MS = 1500;
  const THEME_STORAGE_KEY = "mediaflow-theme";
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
    projectNameEditing: false,
    reanalysisSubmitting: false,
    activatingAnalysis: false,
    dismissedCandidateRevision: null,
    cadReplacementUploading: false,
  };
  const dirtyEdits = state.dirtyEdits;
  const selectedClipIds = state.selectedClipIds;
  const $ = (selector, root = document) => root.querySelector(selector);
  const STATUS_LABELS = {
    ready: "待处理",
    queued: "排队中",
    preparing: "准备输入",
    running: "处理中",
    validating: "验证结果",
    success: "已完成",
    failed: "失败",
    interrupted: "已中断",
    cancelled: "已取消",
    stale_input: "输入已过期",
    superseded: "结果已失效",
  };
  const WORKFLOW_LABELS = {
    sfm_only: "三维重建",
    srt_sfm_fused: "SRT + 三维重建",
    srt_full_pose: "SRT 全姿态",
    pure_rotation: "旋转估计",
  };
  const MOTION_LABELS = {
    general_motion: "一般运动",
    rotation_dominant: "旋转为主",
    static: "静止",
    unknown: "待确认",
  };

  function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    const toggle = $("#sidebarThemeToggle");
    if (!toggle) return;
    const isLight = theme === "light";
    const help = isLight ? "切换为深色模式" : "切换为浅色模式";
    toggle.setAttribute("aria-pressed", String(isLight));
    toggle.setAttribute("aria-label", help);
    toggle.setAttribute("title", help);
    $("#sidebarThemeLabel").textContent = isLight ? "深色模式" : "浅色模式";
  }

  function initializeTheme() {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    const preferred = window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
    applyTheme(saved === "light" || saved === "dark" ? saved : preferred);
  }

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
    const open = $(".open-workbench", row);
    open.disabled = !(
      capabilities.can_open_workbench || capabilities.can_prepare_workbench
    );
    open.title = capabilities.can_open_workbench
      ? "进入片段工作台"
      : (capabilities.can_prepare_workbench ? "准备片段视频后进入工作台" : "片段视频或 CAD 尚未就绪");
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
    $(".status-pill", row).textContent = STATUS_LABELS[clip.status] || clip.status || STATUS_LABELS.ready;
    const thumbnail = $(".clip-thumbnail img", row);
    thumbnail.src = clip.thumbnail_url || "";
    thumbnail.hidden = !clip.thumbnail_url;
    const progressTrack = $(".progress-track", row);
    const progressFill = $(".progress-fill", row);
    const progressPercent = $(".progress-percent", row);
    const fraction = clip.progress?.fraction;
    const active = ["queued", "preparing", "running", "validating"].includes(clip.status);
    const hasPercentage = typeof fraction === "number";
    const measuredPercent = hasPercentage
      ? Math.max(0, Math.round(fraction * 100))
      : null;
    const percent = clip.status === "success"
      ? 100
      : (measuredPercent == null ? null : Math.min(99, measuredPercent));
    progressTrack.hidden = !active && !hasPercentage;
    progressTrack.classList.toggle("progress-indeterminate", active && !hasPercentage);
    progressFill.style.width = hasPercentage ? `${percent}%` : "";
    if (hasPercentage) progressPercent.textContent = `${percent}%`;
    else progressPercent.textContent = "—";
    applyCapabilities(clip, row);
    $(".open-workbench", row).addEventListener("click", () => openWorkbench(clip, row));
    $(".retry-job", row).addEventListener("click", () => runJobAction(clip, "retry"));
    $(".cancel-job", row).addEventListener("click", () => runJobAction(clip, "cancel"));
    return row;
  }

  function renderSnapshot(snapshot) {
    state.snapshot = snapshot;
    if (!state.projectNameEditing) {
      $("#sidebarProjectName").value = snapshot.display_name || snapshot.project_id;
    }
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
    renderCadReplacement(snapshot);
    $("#clipCount").textContent = snapshot.clips.length;
    $("#pendingCount").textContent = snapshot.clips.filter((clip) => ["ready", "queued"].includes(clip.status)).length;
    $("#runningCount").textContent = snapshot.clips.filter((clip) => ["preparing", "running", "validating"].includes(clip.status)).length;
    const reanalyzeButton = $("#reanalyzeButton");
    const analysisActive = ["queued", "preparing", "running", "validating"]
      .includes(snapshot.analysis?.status);
    if (snapshot.candidate_analysis_revision) {
      reanalyzeButton.textContent = "应用新分析结果";
      reanalyzeButton.disabled = state.activatingAnalysis;
    } else if (analysisActive || state.reanalysisSubmitting) {
      reanalyzeButton.textContent = "重新分析中…";
      reanalyzeButton.disabled = true;
    } else {
      reanalyzeButton.textContent = "重新分析";
      reanalyzeButton.disabled = !snapshot.capabilities.can_reanalyze;
    }
    $("#batchTrajectoryButton").disabled = !snapshot.capabilities.can_start_trajectory;
    $("#batchRenderButton").disabled = !snapshot.capabilities.can_render;
    const mergeButton = $("#mergeProjectButton");
    const mergeStatus = snapshot.merge?.status || "not_started";
    const mergeActive = ["queued", "preparing", "running", "validating"].includes(mergeStatus);
    const mergeStatusMessage = $("#mergeStatusMessage");
    const mergeFraction = snapshot.merge?.progress?.fraction;
    const mergePercent = typeof mergeFraction === "number"
      ? `${Math.max(0, Math.min(100, Math.round(mergeFraction * 100)))}%`
      : "";
    mergeButton.disabled = mergeActive || !snapshot.capabilities.can_merge;
    mergeButton.textContent = snapshot.merge?.download_url
      ? "查看合并视频"
      : (mergeActive ? "合并输出中…" : "合并并输出");
    mergeStatusMessage.classList.remove("is-error");
    if (snapshot.merge?.download_url) {
      mergeStatusMessage.textContent = "合并完成，可预览或下载";
    } else if (mergeActive) {
      const stage = STATUS_LABELS[snapshot.merge?.stage] || "合并处理中";
      mergeStatusMessage.textContent = mergePercent ? `${stage} ${mergePercent}` : stage;
    } else if (["failed", "interrupted", "cancelled", "stale_input", "superseded"].includes(mergeStatus)) {
      mergeStatusMessage.textContent = `合并失败：${STATUS_LABELS[mergeStatus] || mergeStatus}`;
      mergeStatusMessage.classList.add("is-error");
    } else {
      mergeStatusMessage.textContent = "";
    }
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
    offerAnalysisCandidate(snapshot);
  }

  function renderCadReplacement(snapshot) {
    const cad_replacement = snapshot.cad_replacement || {};
    const card = $("#cadAssetCard");
    const trigger = $("#cadReplacementTrigger");
    const active = ["queued", "preparing", "running", "validating"]
      .includes(cad_replacement.status) || state.cadReplacementUploading;
    card.classList.toggle(
      "is-replace-eligible",
      cad_replacement.eligible === true && !active,
    );
    card.classList.toggle("is-replacing", active);
    trigger.disabled = cad_replacement.eligible !== true || active;
    trigger.title = cad_replacement.eligible
      ? "替换 CAD 图纸"
      : (cad_replacement.reason || "项目坐标系尚未打通");
    const progress = $("#cadReplacementProgress");
    const reported = cad_replacement.progress?.fraction;
    const hasProgress = typeof reported === "number";
    const percent = cad_replacement.status === "success"
      ? 100
      : (hasProgress ? Math.min(99, Math.max(0, Math.round(reported * 100))) : null);
    progress.hidden = !active && !["failed", "success"].includes(cad_replacement.status);
    $("#cadReplacementFill").style.width = percent == null ? "0%" : `${percent}%`;
    $("#cadReplacementPercent").textContent = percent == null ? "—" : `${percent}%`;
    if (state.cadReplacementUploading) {
      $("#cadMeta").textContent = "正在上传新版 CAD…";
    } else if (active) {
      $("#cadMeta").textContent = cad_replacement.progress?.message || "CAD 图纸替换中";
    } else if (cad_replacement.status === "failed") {
      $("#cadMeta").textContent = cad_replacement.error || "CAD 图纸替换失败，旧图仍在使用";
    } else if (cad_replacement.status === "success") {
      const count = cad_replacement.version_count || 1;
      $("#cadMeta").textContent = `替换完成 · 已保留 ${count} 个版本`;
    }
  }

  function updateCadReplacementConfirmation() {
    const hasFile = $("#cadReplacementFile").files.length === 1;
    const confirmed = $("#cadCoordinateConfirmation").checked;
    $("#confirmCadReplacement").disabled = !hasFile || !confirmed || state.cadReplacementUploading;
  }

  function openCadReplacementDialog() {
    if (state.snapshot?.cad_replacement?.eligible !== true) return;
    const dialog = $("#cadReplacementDialog");
    $("#cadReplacementFile").value = "";
    $("#cadCoordinateConfirmation").checked = false;
    updateCadReplacementConfirmation();
    dialog.showModal();
  }

  async function submitCadReplacement(event) {
    event.preventDefault();
    if (state.cadReplacementUploading) return;
    const file = $("#cadReplacementFile").files[0];
    if (!file || !$("#cadCoordinateConfirmation").checked) return;
    state.cadReplacementUploading = true;
    updateCadReplacementConfirmation();
    if (state.snapshot) renderCadReplacement(state.snapshot);
    try {
      const body = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const form = new FormData();
        form.append("file", file, file.name);
        const revision = state.snapshot.component_revisions.project;
        xhr.open(
          "POST",
          `/api/projects/${encodeURIComponent(projectId)}/uploads/cad-replacement?expectedRevision=${revision}&sameCoordinateSystem=1`,
        );
        xhr.responseType = "json";
        xhr.addEventListener("load", () => {
          const result = xhr.response || {};
          if (xhr.status >= 200 && xhr.status < 300) resolve(result);
          else reject(new Error(result.error || `CAD 替换提交失败（HTTP ${xhr.status}）`));
        });
        xhr.addEventListener("error", () => reject(new Error("CAD 上传连接中断，请检查服务是否运行")));
        xhr.send(form);
      });
      state.snapshot.component_revisions.project = body.project_revision;
      state.etag = null;
      $("#cadReplacementDialog").close();
      setMessage("CAD 图纸替换任务已提交；完成前继续使用当前图纸。", false);
      await pollSnapshot();
    } catch (error) {
      setMessage(error.message, true);
    } finally {
      state.cadReplacementUploading = false;
      updateCadReplacementConfirmation();
      if (state.snapshot) renderCadReplacement(state.snapshot);
    }
  }

  function offerAnalysisCandidate(snapshot) {
    const revision = snapshot.candidate_analysis_revision;
    const preview = snapshot.candidate_analysis_preview;
    const dialog = $("#analysisCandidateDialog");
    if (
      !revision
      || state.dismissedCandidateRevision === revision
      || state.activatingAnalysis
      || dialog.open
    ) return;
    const currentCount = snapshot.clips?.length || 0;
    const candidateCount = preview?.clip_count ?? 0;
    $("#analysisCandidateSummary").textContent =
      `当前 ${currentCount} 段 → 新分析 ${candidateCount} 段`;
    const clipList = $("#analysisCandidateClips");
    const candidateClips = preview?.clips || [];
    clipList.replaceChildren(...(
      candidateClips.length
        ? preview.clips.map((clip) => {
            const row = document.createElement("article");
            row.className = "candidate-clip";
            const name = document.createElement("strong");
            name.textContent = clip.display_name;
            const time = document.createElement("span");
            time.className = "candidate-clip-time";
            time.textContent = `${clip.time_range} · ${clip.duration}`;
            const tags = document.createElement("span");
            tags.className = "candidate-clip-tags";
            const motion = document.createElement("span");
            motion.className = "candidate-tag";
            motion.textContent = MOTION_LABELS[clip.detected_motion_mode]
              || clip.detected_motion_mode;
            const workflow = document.createElement("span");
            workflow.className = "candidate-tag";
            workflow.textContent = WORKFLOW_LABELS[clip.recommended_workflow]
              || "需人工确认";
            tags.append(motion, workflow);
            if (clip.needs_review) {
              const review = document.createElement("span");
              review.className = "candidate-tag review";
              review.textContent = "需确认";
              tags.append(review);
            }
            row.append(name, time, tags);
            return row;
          })
        : [Object.assign(document.createElement("span"), {
            className: "empty-state",
            textContent: "候选片段预览暂不可用，请稍后刷新。",
          })]
    ));
    dialog.showModal();
  }

  function cancelProjectRename() {
    state.projectNameEditing = false;
    const $holder = $("#sidebarProjectName");
    $holder.setAttribute("readonly", "");
    $holder.classList.remove("is-editing");
    $("#projectRenameButton").hidden = false;
    if (state.snapshot) {
      $holder.value = state.snapshot.display_name || state.snapshot.project_id;
    }
  }

  function startProjectRename() {
    if (!state.snapshot || state.projectNameEditing) return;
    state.projectNameEditing = true;
    const $holder = $("#sidebarProjectName");
    $holder.value = state.snapshot.display_name || state.snapshot.project_id;
    $holder.removeAttribute("readonly");
    $holder.classList.add("is-editing");
    $("#projectRenameButton").hidden = true;
    $holder.focus();
    $holder.select();
  }

  async function saveProjectRename() {
    if (!state.projectNameEditing || !state.snapshot) return;
    const $holder = $("#sidebarProjectName");
    const displayName = $holder.value.trim();
    if (!displayName) {
      cancelProjectRename();
      return;
    }
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_revision: state.snapshot.component_revisions.project,
          display_name: displayName,
        }),
      });
      state.snapshot.component_revisions.project = body.project_revision;
      state.snapshot.display_name = body.display_name;
      state.etag = null;
      cancelProjectRename();
      $("#projectBreadcrumb").textContent = `${body.display_name} · ${state.snapshot.project_state}`;
      setMessage("项目名称已保存");
    } catch (error) {
      cancelProjectRename();
      state.etag = null;
      setMessage(`项目重命名失败：${error.message}`, true);
      await pollSnapshot();
    }
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
    if (state.snapshot?.candidate_analysis_revision) {
      state.dismissedCandidateRevision = null;
      offerAnalysisCandidate(state.snapshot);
      return;
    }
    const reanalyzeButton = $("#reanalyzeButton");
    state.reanalysisSubmitting = true;
    reanalyzeButton.textContent = "重新分析中…";
    reanalyzeButton.disabled = true;
    try {
      const { body } = await request(`/api/projects/${encodeURIComponent(projectId)}/analysis/start`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_revision: state.snapshot.component_revisions.project }),
      });
      state.snapshot.component_revisions.project = body.project_revision;
      state.etag = null;
      setMessage("已提交视频分段重新分析，完成后将提示是否应用新结果");
      await pollSnapshot();
    } catch (error) {
      setMessage(error.message, true);
    } finally {
      state.reanalysisSubmitting = false;
      if (state.snapshot) renderSnapshot(state.snapshot);
    }
  }

  async function activateCandidateAnalysis(event) {
    event.preventDefault();
    const revision = state.snapshot?.candidate_analysis_revision;
    if (!revision || state.activatingAnalysis) return;
    state.activatingAnalysis = true;
    $("#confirmAnalysisCandidate").disabled = true;
    try {
      const { body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/analysis/activate`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.project,
            expected_clips_revision: state.snapshot.component_revisions.clips,
            candidate_analysis_revision: revision,
          }),
        },
      );
      state.snapshot.component_revisions.project = body.project_revision;
      state.snapshot.component_revisions.clips = body.clips_revision;
      state.dismissedCandidateRevision = null;
      state.etag = null;
      $("#analysisCandidateDialog").close();
      setMessage("已应用新的视频分段分析结果");
      await pollSnapshot();
    } catch (error) {
      setMessage(error.message, true);
    } finally {
      state.activatingAnalysis = false;
      $("#confirmAnalysisCandidate").disabled = false;
      if (state.snapshot) renderSnapshot(state.snapshot);
    }
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

  function mergeDownloadUrl(previewUrl) {
    const target = new URL(previewUrl, window.location.origin);
    target.searchParams.set("download", "1");
    return `${target.pathname}${target.search}${target.hash}`;
  }

  function openMergeResult(previewUrl) {
    const dialog = $("#mergeResultDialog");
    const video = $("#mergeResultVideo");
    const download = $("#mergeResultDownload");
    video.src = previewUrl;
    download.href = mergeDownloadUrl(previewUrl);
    download.download = `${projectId || "project"}-merged.mp4`;
    if (!dialog.open) dialog.showModal();
  }

  function resetMergeResult() {
    const video = $("#mergeResultVideo");
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  async function mergeProject() {
    if (state.snapshot?.merge?.download_url) {
      openMergeResult(state.snapshot.merge.download_url);
      return;
    }
    const mergeButton = $("#mergeProjectButton");
    const mergeStatusMessage = $("#mergeStatusMessage");
    mergeButton.disabled = true;
    mergeButton.textContent = "正在提交…";
    mergeStatusMessage.classList.remove("is-error");
    mergeStatusMessage.textContent = "正在提交合并任务…";
    try {
      const { body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/merge-jobs`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.jobs,
          }),
        },
      );
      state.snapshot.component_revisions.jobs = body.jobs_revision;
      state.etag = null;
      setMessage("已加入合并输出队列，完成后可直接下载。");
      await pollSnapshot();
    } catch (error) {
      mergeButton.disabled = !state.snapshot?.capabilities?.can_merge;
      mergeButton.textContent = "合并并输出";
      mergeStatusMessage.textContent = `合并失败：${error.message}`;
      mergeStatusMessage.classList.add("is-error");
      setMessage(`合并失败：${error.message}`, true);
    }
  }

  async function openWorkbench(clip, row) {
    const returnParams = new URLSearchParams({
      projectId,
      focusClip: clip.clip_id,
    });
    try {
      const { response, body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/workbench-sessions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.clips,
            expected_jobs_revision: state.snapshot.component_revisions.jobs,
            return_to: `/apps/project_workspace/?${returnParams.toString()}`,
          }),
        },
      );
      if (response.status === 202) {
        state.snapshot.component_revisions.jobs = body.jobs_revision;
        await waitForWorkbenchPreparation(clip.clip_id);
        return;
      }
      state.snapshot.component_revisions.clips = body.clips_revision;
      window.location.assign(body.workbench_url);
    } catch (error) {
      $(".row-error", row).textContent = error.message;
    }
  }

  async function waitForWorkbenchPreparation(clipId) {
    const dialog = $("#workbenchPreparationDialog");
    if (!dialog.open) dialog.showModal();
    while (true) {
      state.etag = null;
      await pollSnapshot();
      const clip = state.snapshot?.clips.find((item) => item.clip_id === clipId);
      if (!clip) throw new Error("片段已不存在，无法进入工作台");
      const preparation = clip.workbench?.preparation;
      const fraction = preparation?.progress?.fraction;
      const percent = typeof fraction === "number"
        ? Math.max(0, Math.min(100, Math.round(fraction * 100)))
        : null;
      $("#workbenchPreparationMessage").textContent = preparation?.stage
        ? (STATUS_LABELS[preparation.stage] || preparation.stage)
        : "正在按原视频时间范围准备片段视频…";
      $("#workbenchPreparationFill").style.width = percent == null ? "0%" : `${percent}%`;
      $("#workbenchPreparationPercent").textContent = percent == null ? "—" : `${percent}%`;
      if (["failed", "interrupted", "cancelled", "stale_input", "superseded"].includes(preparation?.status)) {
        dialog.close();
        throw new Error(preparation?.error || "片段视频准备失败，请重试");
      }
      if (clip.capabilities?.can_open_workbench) {
        dialog.close();
        await openWorkbench(clip, document.querySelector(`[data-clip-id="${CSS.escape(clipId)}"]`));
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  $("#sidebarToggle").addEventListener("click", (event) => {
    const collapsed = $("#appShell").classList.toggle("sidebar-collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
    event.currentTarget.setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
  });
  $("#sidebarThemeToggle").addEventListener("click", () => {
    const theme = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    applyTheme(theme);
  });
  document.querySelector('[data-nav="files"]').addEventListener("click", () => {
    window.location.assign("/apps/project_library/");
  });
  $("#projectRenameButton").addEventListener("click", startProjectRename);
  $("#sidebarProjectName").addEventListener("blur", saveProjectRename);
  $("#sidebarProjectName").addEventListener("keydown", (event) => {
    if (event.key === "Enter") event.currentTarget.blur();
    if (event.key === "Escape") {
      event.preventDefault();
      cancelProjectRename();
    }
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
  $("#mergeProjectButton").addEventListener("click", mergeProject);
  $("#closeMergeResult").addEventListener("click", () => $("#mergeResultDialog").close());
  $("#mergeResultDialog").addEventListener("close", resetMergeResult);
  $("#reanalyzeButton").addEventListener("click", reanalyzeProject);
  $("#cadReplacementTrigger").addEventListener("click", openCadReplacementDialog);
  $("#cadReplacementForm").addEventListener("submit", submitCadReplacement);
  $("#cadReplacementFile").addEventListener("change", updateCadReplacementConfirmation);
  $("#cadCoordinateConfirmation").addEventListener("change", updateCadReplacementConfirmation);
  $("#cancelCadReplacement").addEventListener("click", () => $("#cadReplacementDialog").close());
  $("#confirmAnalysisCandidate").addEventListener("click", activateCandidateAnalysis);
  $("#dismissAnalysisCandidate").addEventListener("click", () => {
    state.dismissedCandidateRevision = state.snapshot?.candidate_analysis_revision || null;
  });
  $("#confirmPreflight").addEventListener("click", enqueuePreflight);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollSnapshot(); });
  initializeTheme();
  if (!projectId) setMessage("缺少项目标识，无法载入工作区。", true);
  else pollSnapshot();
  window.setInterval(pollSnapshot, POLL_INTERVAL_MS);
})();
