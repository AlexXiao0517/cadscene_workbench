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
    fullPoseClipId: null,
    srtConfigWorkflow: null,
    georeferenceCandidates: [],
    georeferenceOperation: null,
    georeferencePollGeneration: 0,
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
    srt_fixed_track_visual_pose: "SRT 轨迹 + 视觉姿态",
    srt_full_pose: "SRT 全姿态（跳过三维重建）",
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
    return Object.hasOwn(WORKFLOW_LABELS, workflow) ? workflow : "sfm_only";
  }

  function trajectoryDisplayStatus(clip) {
    return clip.status === "cancelled" ? "ready" : clip.status;
  }

  function formatSrtCoverage(coverage) {
    if (!coverage || Number(coverage.overlapping_record_count || 0) <= 0) return "未检测到片段内 SRT 记录";
    const percent = (value) => `${Math.round(Math.max(0, Math.min(1, Number(value) || 0)) * 100)}%`;
    return `定位/高度覆盖 ${percent(coverage.trajectory_coverage)} · 完整姿态覆盖 ${percent(coverage.full_pose_coverage)} · ${coverage.overlapping_record_count} 条记录`;
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
    const bridgeUp = $(".bridge-up", row);
    bridgeUp.hidden = capabilities.can_bridge_up !== true;
    bridgeUp.disabled = capabilities.can_bridge_up !== true;
    bridgeUp.title = capabilities.can_bridge_up
      ? `用重叠帧打通 ${capabilities.bridge_up_target_clip_id} 的路线，完成后进入微调`
      : (capabilities.bridge_up_reason || "没有可安全打通的同场景上一片段");
    const bridgeDown = $(".bridge-down", row);
    bridgeDown.hidden = capabilities.can_bridge_down !== true;
    bridgeDown.disabled = capabilities.can_bridge_down !== true;
    bridgeDown.title = capabilities.can_bridge_down
      ? `用重叠帧打通 ${capabilities.bridge_down_target_clip_id} 的路线，完成后进入微调`
      : (capabilities.bridge_down_reason || "没有可安全打通的同场景下一片段");
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
    const recommendation = $(".workflow-recommendation", row);
    recommendation.textContent = WORKFLOW_LABELS[clip.recommended_workflow]
      || "需人工确认";
    if (clip.srt_coverage) recommendation.title = formatSrtCoverage(clip.srt_coverage);
    const workflow = $(".workflow-select", row);
    workflow.value = visibleWorkflowChoice(clip, edit);
    workflow.classList.toggle("local-dirty", dirtyEdits.has(clip.clip_id));
    workflow.addEventListener("change", () => saveWorkflow(clip, workflow, row));
    const displayStatus = trajectoryDisplayStatus(clip);
    $(".status-pill", row).textContent = STATUS_LABELS[trajectoryDisplayStatus(clip)]
      || displayStatus
      || STATUS_LABELS.ready;
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
    const configureFullPose = $(".configure-full-pose", row);
    const configurableSrt = workflow.value === "srt_full_pose"
      || workflow.value === "srt_fixed_track_visual_pose";
    configureFullPose.hidden = !configurableSrt;
    configureFullPose.addEventListener("click", () => openFullPoseDialog(clip));
    const bridgeStatus = clip.scene_bridge?.status;
    const hasSavedWorkbench = clip.workbench?.state === "saved";
    const bridgeReason = clip.capabilities?.bridge_up_reason
      || clip.capabilities?.bridge_down_reason;
    if (!hasSavedWorkbench && ["stale_input", "superseded"].includes(bridgeStatus)) {
      $(".row-error", row).textContent = "旧打通结果已失效，可重新打通";
    } else if (bridgeReason) {
      $(".row-error", row).textContent = bridgeReason;
    } else if (["srt_full_pose", "srt_fixed_track_visual_pose"].includes(workflow.value) && clip.capabilities?.reason) {
      $(".row-error", row).textContent = clip.capabilities.reason;
    }
    $(".open-workbench", row).addEventListener("click", () => openWorkbench(clip, row));
    $(".bridge-up", row).addEventListener("click", () => bridgeAdjacent(clip, "up", row));
    $(".bridge-down", row).addEventListener("click", () => bridgeAdjacent(clip, "down", row));
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
    $("#pendingCount").textContent = snapshot.clips.filter((clip) =>
      ["ready", "queued"].includes(trajectoryDisplayStatus(clip))).length;
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

  function axisMappingLabel(value) {
    return value === "cad_x_northing_cad_y_easting"
      ? "CAD X=北坐标，Y=东坐标"
      : "CAD X=东坐标，Y=北坐标";
  }

  function parseCentralMeridianInput(raw) {
    const text = String(raw ?? "").trim();
    if (!text) return null;
    const decimal = text.match(/^([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*(?:°|度)?$/u);
    let value;
    if (decimal) {
      value = Number(decimal[1]);
    } else {
      const degreeMinute = text.match(/^([+-]?\d{1,3})(?:\s*(?:°|度)\s*|\s+)(\d+(?:\.\d+)?)\s*(?:′|'|分)?$/u);
      if (!degreeMinute) {
        throw new Error("请输入十进制度或度分，例如 120 或 118°50′");
      }
      const minutes = Number(degreeMinute[2]);
      if (!Number.isFinite(minutes) || minutes < 0 || minutes >= 60) {
        throw new Error("中央经线的分必须大于等于 0 且小于 60");
      }
      const sign = degreeMinute[1].startsWith("-") ? -1 : 1;
      value = sign * (Math.abs(Number(degreeMinute[1])) + minutes / 60);
    }
    if (!Number.isFinite(value) || value < -180 || value > 180) {
      throw new Error("中央经线必须位于 -180° 到 180°");
    }
    return value;
  }

  function formatCentralMeridian(degrees) {
    const value = Number(degrees);
    if (!Number.isFinite(value)) return "";
    const sign = value < 0 ? "-" : "";
    let wholeDegrees = Math.floor(Math.abs(value));
    const rawMinutes = (Math.abs(value) - wholeDegrees) * 60;
    let minutes = Math.round(rawMinutes);
    if (Math.abs(rawMinutes - minutes) < 1e-8) {
      if (minutes === 60) {
        wholeDegrees += 1;
        minutes = 0;
      }
      return minutes === 0
        ? `${sign}${wholeDegrees}°`
        : `${sign}${wholeDegrees}°${minutes}′`;
    }
    return String(Number(value.toFixed(10)));
  }

  function cadGeoreferenceLabel(config) {
    return config?.crs_source === "custom"
      ? "自定义 CGCS2000 高斯—克吕格"
      : `EPSG:${config?.epsg}`;
  }

  function renderCadGeoreferenceStatus() {
    const config = state.snapshot?.cad_georeference || {};
    const status = $("#cadGeoreferenceStatus");
    if (config.confirmed === true) {
      status.textContent = `已确认 ${cadGeoreferenceLabel(config)} · 中央经线 ${formatCentralMeridian(config.central_meridian_deg)} · ${axisMappingLabel(config.cad_axis_mapping)}`;
      status.classList.add("is-confirmed");
    } else if (config.stale_reason === "cad_asset_changed") {
      status.textContent = "CAD 已更换，请为当前图纸重新确认坐标系";
      status.classList.remove("is-confirmed");
    } else {
      status.textContent = "尚未确认；轨迹任务会保持禁用";
      status.classList.remove("is-confirmed");
    }
  }

  function svgElement(name, attributes = {}) {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) {
      element.setAttribute(key, String(value));
    }
    return element;
  }

  function renderCadGeoreferencePreview(candidate) {
    const svg = $("#cadGeoreferencePreview");
    const evidence = candidate?.evidence || {};
    const bbox = evidence.cad_bbox_raw;
    const trajectory = evidence.trajectory_polyline_raw;
    svg.replaceChildren();
    if (!Array.isArray(bbox) || bbox.length !== 4 || !Array.isArray(trajectory) || !trajectory.length) {
      const label = svgElement("text", { x: 320, y: 118, "text-anchor": "middle", class: "preview-empty" });
      label.textContent = "此候选暂无可视化证据";
      svg.append(label);
      return;
    }
    const validPoints = trajectory
      .filter((point) => Array.isArray(point) && point.length === 2)
      .map((point) => [Number(point[0]), Number(point[1])])
      .filter((point) => point.every(Number.isFinite));
    if (!validPoints.length) return;
    const xValues = [Number(bbox[0]), Number(bbox[2]), ...validPoints.map((point) => point[0])];
    const yValues = [Number(bbox[1]), Number(bbox[3]), ...validPoints.map((point) => point[1])];
    const minX = Math.min(...xValues);
    const maxX = Math.max(...xValues);
    const minY = Math.min(...yValues);
    const maxY = Math.max(...yValues);
    const spanX = Math.max(maxX - minX, 1);
    const spanY = Math.max(maxY - minY, 1);
    const project = ([x, y]) => [
      24 + ((x - minX) / spanX) * 592,
      206 - ((y - minY) / spanY) * 182,
    ];
    const cadMin = project([Number(bbox[0]), Number(bbox[1])]);
    const cadMax = project([Number(bbox[2]), Number(bbox[3])]);
    svg.append(svgElement("rect", {
      x: Math.min(cadMin[0], cadMax[0]),
      y: Math.min(cadMin[1], cadMax[1]),
      width: Math.abs(cadMax[0] - cadMin[0]),
      height: Math.abs(cadMax[1] - cadMin[1]),
      class: "cad-preview-bounds",
    }));
    const screenPoints = validPoints.map(project);
    svg.append(svgElement("path", {
      d: screenPoints.map((point, index) => `${index ? "L" : "M"}${point[0].toFixed(2)},${point[1].toFixed(2)}`).join(" "),
      class: "trajectory-preview-line",
    }));
    svg.append(svgElement("circle", { cx: screenPoints[0][0], cy: screenPoints[0][1], r: 5, class: "trajectory-start" }));
    const end = screenPoints.at(-1);
    svg.append(svgElement("circle", { cx: end[0], cy: end[1], r: 5, class: "trajectory-end" }));
  }

  function renderCadGeoreferenceCandidates() {
    const container = $("#cadGeoreferenceCandidates");
    const candidates = state.georeferenceCandidates;
    if (!candidates.length) {
      container.replaceChildren(Object.assign(document.createElement("span"), {
        className: "empty-state",
        textContent: "没有找到可用的 CGCS2000 高斯-克吕格候选",
      }));
      renderCadGeoreferencePreview(null);
      return;
    }
    container.replaceChildren(...candidates.map((candidate, index) => {
      const card = document.createElement("article");
      card.className = "crs-candidate";
      if (index === 0) card.classList.add("is-recommended");
      const title = document.createElement("div");
      title.className = "crs-candidate-title";
      const code = document.createElement("strong");
      code.textContent = candidate.crs_source === "custom"
        ? "自定义 CGCS2000 高斯—克吕格"
        : `EPSG:${candidate.epsg}`;
      const rank = document.createElement("span");
      rank.textContent = index === 0 ? "推荐" : "备选";
      title.append(code, rank);
      const projection = document.createElement("p");
      projection.textContent = `${candidate.crs_name} · 中央经线 ${formatCentralMeridian(candidate.central_meridian_deg)}`;
      const mapping = document.createElement("p");
      mapping.textContent = axisMappingLabel(candidate.cad_axis_mapping);
      const evidence = document.createElement("p");
      const inside = Number(candidate.evidence?.trajectory_inside_cad_ratio || 0) * 100;
      evidence.className = "crs-evidence";
      evidence.textContent = `匹配分 ${Number(candidate.score).toFixed(1)} · 轨迹落入 CAD ${inside.toFixed(0)}%`;
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.className = "button compact";
      confirm.textContent = "确认此坐标系";
      confirm.addEventListener("click", () => confirmCadGeoreference(candidate));
      card.addEventListener("mouseenter", () => renderCadGeoreferencePreview(candidate));
      card.addEventListener("focusin", () => renderCadGeoreferencePreview(candidate));
      card.append(title, projection, mapping, evidence, confirm);
      return card;
    }));
    const top = candidates[0];
    const notice = $("#centralMeridianNotice");
    notice.hidden = Number(top.epsg) !== 4549;
    notice.textContent = Number(top.epsg) === 4549
      ? "120°是本项目推荐中央经线，不会应用到其他 CAD"
      : "";
    renderCadGeoreferencePreview(top);
  }

  function renderCadGeoreferenceOperation(operation) {
    state.georeferenceOperation = operation || null;
    const status = operation?.status || "not_started";
    const active = ["queued", "running", "preparing", "validating"].includes(status);
    const progress = operation?.progress || {};
    const fraction = Number(progress.fraction);
    const hasFraction = Number.isFinite(fraction);
    const percent = status === "success"
      ? 100
      : (hasFraction ? Math.min(99, Math.max(0, Math.round(fraction * 100))) : null);
    const container = $("#cadGeoreferenceProgress");
    const track = $("#cadGeoreferenceProgress .progress-track");
    container.hidden = status === "not_started";
    track.classList.toggle("progress-indeterminate", active && percent == null);
    $("#cadGeoreferenceProgressFill").style.width = percent == null ? "" : `${percent}%`;
    $("#cadGeoreferenceProgressPercent").textContent = percent == null ? "—" : `${percent}%`;
    $("#cadGeoreferenceProgressText").textContent = progress.message
      || operation?.error
      || ({
        queued: "候选任务已排队",
        running: "正在生成坐标系候选",
        preparing: "正在准备坐标输入",
        validating: "正在验证候选结果",
        success: "坐标系候选生成完成",
        failed: "坐标系候选生成失败",
        stale_input: "候选输入已变化，请重新生成",
      }[status] || "等待生成候选");
    const currentCandidates = status === "success" && operation?.stale !== true
      ? (operation.candidates || [])
      : [];
    state.georeferenceCandidates = currentCandidates;
    renderCadGeoreferenceCandidates();
    if (!currentCandidates.length) {
      const empty = $("#cadGeoreferenceCandidates .empty-state");
      if (empty) {
        empty.textContent = active
          ? "候选计算完成后将在这里显示"
          : (status === "failed" ? "生成失败；请检查参数后重试" : "填写中央经线后点击“生成候选”");
      }
    }
    const button = $("#loadCadGeoreferenceCandidates");
    button.disabled = active;
    button.textContent = active ? "正在生成…" : (status === "not_started" ? "生成候选" : "重新生成候选");
    $("#centralMeridianInput").disabled = active;
  }

  async function pollCadGeoreferenceCandidates() {
    const generation = ++state.georeferencePollGeneration;
    while ($("#fullPoseDialog").open && generation === state.georeferencePollGeneration) {
      try {
        const { body } = await request(
          `/api/projects/${encodeURIComponent(projectId)}/cad-georeference/candidates`,
          { method: "GET" },
        );
        if (generation !== state.georeferencePollGeneration) return;
        const operation = body.operation || { status: "not_started", candidates: [] };
        const input = $("#centralMeridianInput");
        if (input.value === "" && operation.requested_central_meridian_deg != null) {
          input.value = formatCentralMeridian(operation.requested_central_meridian_deg);
        }
        renderCadGeoreferenceOperation(operation);
        if (!["queued", "running", "preparing", "validating"].includes(operation.status)) return;
      } catch (error) {
        if (generation === state.georeferencePollGeneration) {
          setMessage(`坐标系候选状态读取失败：${error.message}`, true);
        }
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  async function loadCadGeoreferenceCandidates() {
    const button = $("#loadCadGeoreferenceCandidates");
    const input = $("#centralMeridianInput");
    let centralMeridian;
    try {
      centralMeridian = parseCentralMeridianInput(input.value);
    } catch (error) {
      input.setCustomValidity(error.message);
      input.reportValidity();
      input.setCustomValidity("");
      return;
    }
    state.georeferencePollGeneration += 1;
    button.disabled = true;
    button.textContent = "正在提交…";
    try {
      const { body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/cad-georeference/candidates`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.project,
            central_meridian_deg: centralMeridian,
          }),
        },
      );
      renderCadGeoreferenceOperation(body.operation);
      void pollCadGeoreferenceCandidates();
    } catch (error) {
      setMessage(`坐标系候选生成失败：${error.message}`, true);
      button.disabled = false;
      button.textContent = "重新生成候选";
    }
  }

  async function confirmCadGeoreference(candidate) {
    try {
      const { body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/cad-georeference/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.project,
            candidate_job_id: state.georeferenceOperation?.job_id,
            candidate_input_fingerprint: state.georeferenceOperation?.input_fingerprint,
            candidate: {
              ...candidate,
              cad_axis_mapping: candidate.cad_axis_mapping,
              central_meridian_deg: candidate.central_meridian_deg,
            },
          }),
        },
      );
      state.snapshot.component_revisions.project = body.project_revision;
      state.snapshot.cad_georeference = body.cad_georeference;
      state.etag = null;
      renderCadGeoreferenceStatus();
      setMessage(`已确认 ${cadGeoreferenceLabel(body.cad_georeference)}，仅绑定当前 CAD`);
    } catch (error) {
      setMessage(`坐标系确认失败：${error.message}`, true);
    }
  }

  function openFullPoseDialog(clip) {
    if (!clip) return;
    state.fullPoseClipId = clip.clip_id;
    state.srtConfigWorkflow = clip.resolved_workflow;
    state.georeferenceCandidates = [];
    state.georeferenceOperation = null;
    const fixedTrack = state.srtConfigWorkflow === "srt_fixed_track_visual_pose";
    const settings = fixedTrack
      ? (clip.srt_fixed_track_visual_pose_settings || {})
      : (clip.srt_full_pose_settings || {});
    $("#srtConfigEyebrow").textContent = fixedTrack ? "SRT 固定轨迹" : "SRT 直接轨迹";
    $("#srtConfigTitle").textContent = fixedTrack
      ? "配置固定轨迹与视觉姿态"
      : "配置无人机与 CAD 坐标";
    $("#srtConfigDescription").textContent = fixedTrack
      ? "SRT 提供严格位置和相对高度，视觉算法只估计姿态，不执行三维重建。"
      : "大疆 SRT 已提供位置和云台姿态，不再执行三维重建。镜头按水平视场角建模，姿态使用大疆绝对 NED 约定。";
    $("#cadZOffsetField").hidden = fixedTrack;
    $("#routeOffsetFields").hidden = !fixedTrack;
    $("#srtCoverageSummary").textContent = formatSrtCoverage(clip.srt_coverage);
    $("#horizontalFovInput").value = settings.horizontal_fov_deg ?? "";
    const confirmedGeoreference = state.snapshot?.cad_georeference || {};
    $("#centralMeridianInput").value = confirmedGeoreference.confirmed
      ? formatCentralMeridian(confirmedGeoreference.central_meridian_deg)
      : "";
    $("#cadZOffsetInput").value = settings.cad_z_offset_m ?? 0;
    const routeOffset = Array.isArray(settings.route_offset_xyz_m)
      ? settings.route_offset_xyz_m
      : [0, 0, 0];
    $("#routeOffsetXInput").value = routeOffset[0] ?? 0;
    $("#routeOffsetYInput").value = routeOffset[1] ?? 0;
    $("#routeOffsetZInput").value = routeOffset[2] ?? 0;
    $("#cadGeoreferenceCandidates").replaceChildren(Object.assign(document.createElement("span"), {
      className: "empty-state",
      textContent: "填写中央经线后点击“生成候选”",
    }));
    renderCadGeoreferenceOperation({ status: "not_started", candidates: [] });
    renderCadGeoreferenceStatus();
    renderCadGeoreferencePreview(null);
    $("#fullPoseDialog").showModal();
    void pollCadGeoreferenceCandidates();
  }

  function closeFullPoseDialog() {
    state.georeferencePollGeneration += 1;
    state.fullPoseClipId = null;
    state.srtConfigWorkflow = null;
    $("#fullPoseDialog").close();
  }

  async function saveFullPoseSettings(event) {
    event.preventDefault();
    const clipId = state.fullPoseClipId;
    const fovInput = $("#horizontalFovInput");
    const horizontalFov = Number(fovInput.value);
    const cadZOffset = Number($("#cadZOffsetInput").value || 0);
    const fixedTrack = state.srtConfigWorkflow === "srt_fixed_track_visual_pose";
    const routeOffset = [
      Number($("#routeOffsetXInput").value || 0),
      Number($("#routeOffsetYInput").value || 0),
      Number($("#routeOffsetZInput").value || 0),
    ];
    if (!clipId || !Number.isFinite(horizontalFov) || horizontalFov <= 1 || horizontalFov >= 179) {
      fovInput.setCustomValidity("请输入 1° 到 179° 之间的水平视场角");
      fovInput.reportValidity();
      fovInput.setCustomValidity("");
      return;
    }
    if (fixedTrack && routeOffset.some((value) => !Number.isFinite(value))) {
      setMessage("整条路线的 XYZ 偏移必须是有效数值", true);
      return;
    }
    try {
      const settingsEndpoint = fixedTrack
        ? `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clipId)}/srt-fixed-track-visual-pose`
        : `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clipId)}/srt-full-pose`;
      const { body } = await request(
        settingsEndpoint,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(fixedTrack
            ? {
                expected_revision: state.snapshot.component_revisions.clips,
                horizontal_fov_deg: horizontalFov,
                route_offset_xyz_m: routeOffset,
              }
            : {
                expected_revision: state.snapshot.component_revisions.clips,
                horizontal_fov_deg: horizontalFov,
                cad_z_offset_m: cadZOffset,
                attitude_profile: "dji_absolute_ned",
              }),
        },
      );
      state.snapshot.component_revisions.clips = body.clips_revision;
      const selected = state.snapshot.clips.find((item) => item.clip_id === clipId);
      if (selected) {
        if (fixedTrack) selected.srt_fixed_track_visual_pose_settings = body.settings;
        else selected.srt_full_pose_settings = body.settings;
      }
      state.etag = null;
      closeFullPoseDialog();
      setMessage(fixedTrack ? "SRT 固定轨迹配置已保存" : "SRT 全姿态配置已保存");
      await pollSnapshot();
    } catch (error) {
      setMessage(`SRT 配置保存失败：${error.message}`, true);
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
      if (["srt_full_pose", "srt_fixed_track_visual_pose"].includes(workflow)) {
        const refreshed = state.snapshot?.clips.find((item) => item.clip_id === clip.clip_id);
        openFullPoseDialog(refreshed || clip);
      }
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

  async function bridgeAdjacent(clip, direction, row) {
    const capability = clip.capabilities || {};
    const targetClipId = direction === "up"
      ? capability.bridge_up_target_clip_id
      : capability.bridge_down_target_clip_id;
    if (!targetClipId) return;
    const dialog = $("#workbenchPreparationDialog");
    const directionLabel = direction === "up" ? "向上" : "向下";
    $("#workbenchPreparationTitle").textContent = "正在提交路线打通任务";
    $("#workbenchPreparationMessage").textContent = `正在提交${directionLabel}打通任务…`;
    $("#workbenchPreparationFill").style.width = "0%";
    $("#workbenchPreparationPercent").textContent = "—";
    if (!dialog.open) dialog.showModal();
    setMessage(`正在提交${directionLabel}打通任务…`);
    try {
      const { response, body } = await request(
        `/api/projects/${encodeURIComponent(projectId)}/clips/${encodeURIComponent(clip.clip_id)}/scene-bridges`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: state.snapshot.component_revisions.clips,
            expected_jobs_revision: state.snapshot.component_revisions.jobs,
            direction: direction,
          }),
        },
      );
      if (response.status !== 202) throw new Error("场景路线打通任务未成功入队");
      state.snapshot.component_revisions.clips = body.clips_revision;
      state.snapshot.component_revisions.jobs = body.jobs_revision;
      await waitForSceneBridge(body.job_id, targetClipId);
    } catch (error) {
      if (dialog.open) dialog.close();
      $(".row-error", row).textContent = error.message;
      setMessage(`路线打通失败：${error.message}`, true);
      state.etag = null;
      await pollSnapshot();
    }
  }

  async function waitForSceneBridge(jobId, targetClipId) {
    const dialog = $("#workbenchPreparationDialog");
    $("#workbenchPreparationTitle").textContent = "正在打通相邻片段路线";
    if (!dialog.open) dialog.showModal();
    while (true) {
      state.etag = null;
      await pollSnapshot();
      const target = state.snapshot?.clips.find((item) => item.clip_id === targetClipId);
      if (!target) throw new Error("目标片段已不存在，无法继续打通路线");
      const bridge = target.scene_bridge;
      if (bridge?.job_id !== jobId) {
        dialog.close();
        throw new Error("目标片段的场景路线任务已变化，请重新操作");
      }
      const fraction = bridge?.progress?.fraction;
      const percent = typeof fraction === "number"
        ? Math.max(0, Math.min(100, Math.round(fraction * 100)))
        : null;
      $("#workbenchPreparationMessage").textContent = bridge?.progress?.message
        || (bridge?.stage ? (STATUS_LABELS[bridge.stage] || bridge.stage) : "正在使用共同 PTS 打通相邻片段路线…");
      $("#workbenchPreparationFill").style.width = percent == null ? "0%" : `${percent}%`;
      $("#workbenchPreparationPercent").textContent = percent == null ? "—" : `${percent}%`;
      if (["failed", "interrupted", "cancelled", "stale_input", "superseded"].includes(bridge?.status)) {
        dialog.close();
        throw new Error(bridge?.error || "相邻片段路线打通失败，请重试");
      }
      if (bridge?.status === "success") {
        dialog.close();
        const targetRow = document.querySelector(`[data-clip-id="${CSS.escape(targetClipId)}"]`);
        await openWorkbench(target, targetRow);
        return;
      }
      await new Promise((resolve) => window.setTimeout(resolve, POLL_INTERVAL_MS));
    }
  }

  async function waitForWorkbenchPreparation(clipId) {
    const dialog = $("#workbenchPreparationDialog");
    $("#workbenchPreparationTitle").textContent = "正在准备片段工作台";
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
  $("#fullPoseForm").addEventListener("submit", saveFullPoseSettings);
  $("#loadCadGeoreferenceCandidates").addEventListener("click", loadCadGeoreferenceCandidates);
  $("#centralMeridianInput").addEventListener("input", () => {
    state.georeferencePollGeneration += 1;
    renderCadGeoreferenceOperation({ status: "not_started", candidates: [] });
  });
  $("#closeFullPoseDialog").addEventListener("click", closeFullPoseDialog);
  $("#cancelFullPoseSettings").addEventListener("click", closeFullPoseDialog);
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
