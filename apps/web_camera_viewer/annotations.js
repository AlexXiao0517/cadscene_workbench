(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const token = params.get("projectWorkbenchToken") || "";
  const projectId = params.get("projectId") || params.get("dataset") || "";
  const video = document.querySelector("#sourceVideo");
  const videoLayer = video?.closest(".video-layer");
  const sceneContainer = document.querySelector("#sceneContainer");
  const overlay = document.querySelector("#annotationOverlay");
  const cadOverlay = document.querySelector("#cadAnnotationOverlay");
  const roiSelection = document.querySelector("#annotationRoiSelection");
  const status = document.querySelector("#annotationToolStatus");
  const trackingState = document.querySelector("#annotationTrackingState");
  const editor = {
    title: document.querySelector("#annotationTitle"),
    body: document.querySelector("#annotationBody"),
    panelWidth: document.querySelector("#annotationPanelWidth"),
    backgroundOpacity: document.querySelector("#annotationBackgroundOpacity"),
    titleColor: document.querySelector("#annotationTitleColor"),
    fontSize: document.querySelector("#annotationFontSize"),
    color: document.querySelector("#annotationTextColor"),
    startPts: document.querySelector("#annotationStartPts"),
    endPts: document.querySelector("#annotationEndPts"),
    visible: document.querySelector("#annotationVisible"),
    save: document.querySelector("#annotationSave"),
    remove: document.querySelector("#annotationDelete"),
    reanchor: document.querySelector("#annotationReanchor"),
  };
  if (!video || !videoLayer || !sceneContainer || !overlay || !cadOverlay) return;

  const state = {
    clipId: "",
    clip: null,
    snapshot: null,
    annotations: [],
    annotationsRevision: 0,
    selectedId: null,
    mode: null,
    selectionStart: null,
    nodes: new Map(),
    cadNodes: new Map(),
    drag: null,
    renderQueued: false,
    pendingInitialTrackingId: null,
    suppressNextVideoClick: false,
  };
  const MAX_DOM_LABELS = 250;
  const DEFAULT_VIDEO_ROI_SIZE = 64;

  function setStatus(message) {
    if (status) status.textContent = message;
  }

  function colorWithOpacity(value, opacity) {
    const text = String(value || "#000000").replace("#", "");
    const normalized = text.length === 8 ? text : `${text.slice(0, 6)}FF`;
    const red = Number.parseInt(normalized.slice(0, 2), 16) || 0;
    const green = Number.parseInt(normalized.slice(2, 4), 16) || 0;
    const blue = Number.parseInt(normalized.slice(4, 6), 16) || 0;
    const encodedAlpha = (Number.parseInt(normalized.slice(6, 8), 16) || 0) / 255;
    return `rgba(${red}, ${green}, ${blue}, ${encodedAlpha * Number(opacity ?? 1)})`;
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {
      cache: "no-store",
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const body = response.status === 204 ? {} : await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || `标签请求失败（HTTP ${response.status}）`);
    return body;
  }

  function annotationPath(annotationId = "") {
    const root = `/api/projects/${encodeURIComponent(projectId)}/annotations`;
    return annotationId ? `${root}/${encodeURIComponent(annotationId)}` : root;
  }

  function selectedAnnotation() {
    return state.annotations.find((item) => item.annotation_id === state.selectedId) || null;
  }

  function clipPtsRange() {
    const interval = state.clip?.source_interval;
    if (!interval?.time_base) return null;
    return {
      start_pts: Number(interval.start_pts),
      end_pts_exclusive: Number(interval.end_pts_exclusive),
      time_base: {
        numerator: Number(interval.time_base.numerator),
        denominator: Number(interval.time_base.denominator),
      },
      semantics: "half_open",
    };
  }

  function currentSourcePts() {
    return window.CadsceneAnnotationPts?.currentSourcePts?.() ?? null;
  }

  function syncEditor() {
    const annotation = selectedAnnotation();
    const enabled = Boolean(annotation);
    for (const control of [editor.title, editor.body, editor.panelWidth, editor.backgroundOpacity, editor.titleColor, editor.fontSize, editor.color, editor.startPts, editor.endPts, editor.visible]) {
      if (control) control.disabled = !enabled;
    }
    if (editor.save) editor.save.disabled = !enabled;
    if (editor.remove) editor.remove.disabled = !enabled;
    if (editor.reanchor) editor.reanchor.disabled = !annotation || annotation.anchor_type !== "video_track";
    if (!annotation) return;
    editor.title.value = annotation.content?.title || "";
    editor.body.value = annotation.content?.body ?? annotation.text ?? "";
    editor.panelWidth.value = String(annotation.panel?.width_px || 320);
    editor.backgroundOpacity.value = String(annotation.style?.background_opacity ?? 0.7);
    editor.titleColor.value = String(annotation.style?.title_color || "#69D2FF").slice(0, 7);
    editor.fontSize.value = String(annotation.style?.font_size_px || 28);
    editor.color.value = String(annotation.style?.text_color || "#FFFFFF").slice(0, 7);
    editor.startPts.value = String(annotation.source_pts_range.start_pts);
    editor.endPts.value = String(annotation.source_pts_range.end_pts_exclusive);
    editor.visible.checked = annotation.user_visible !== false;
  }

  function selectAnnotation(annotationId) {
    state.selectedId = annotationId;
    for (const nodes of [state.nodes, state.cadNodes]) {
      for (const [identity, entry] of nodes) {
        entry.label.classList.toggle("is-selected", identity === annotationId);
      }
    }
    syncEditor();
  }

  function focusSelectedAnnotationEditor() {
    if (!editor.title || !selectedAnnotation()) return;
    editor.title.focus();
    editor.title.select();
  }

  function removeNode(annotationId, nodes = state.nodes) {
    const entry = nodes.get(annotationId);
    if (!entry) return;
    entry.label.remove();
    entry.line.remove();
    entry.dot.remove();
    nodes.delete(annotationId);
  }

  function ensureNode(annotation, nodes = state.nodes, container = overlay) {
    let entry = nodes.get(annotation.annotation_id);
    if (entry) return entry;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    line.classList.add("engineering-callout-leader");
    const polyline = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
    line.append(polyline);
    const dot = document.createElement("div");
    dot.className = "annotation-anchor-dot";
    const label = document.createElement("div");
    label.className = "annotation-label engineering-callout";
    const title = document.createElement("div");
    title.className = "engineering-callout-title";
    const body = document.createElement("div");
    body.className = "engineering-callout-body";
    label.append(title, body);
    label.dataset.annotationId = annotation.annotation_id;
    label.addEventListener("pointerdown", beginLabelDrag);
    label.addEventListener("click", (event) => {
      event.stopPropagation();
      selectAnnotation(annotation.annotation_id);
    });
    container.append(line, dot, label);
    entry = { line, polyline, dot, label, title, body };
    nodes.set(annotation.annotation_id, entry);
    return entry;
  }

  function ensureCadNode(annotation) {
    return ensureNode(annotation, state.cadNodes, cadOverlay);
  }

  function reconcileNodes() {
    const visibleSet = new Set(
      state.annotations.slice(0, MAX_DOM_LABELS).map((item) => item.annotation_id),
    );
    for (const identity of state.nodes.keys()) {
      if (!visibleSet.has(identity)) removeNode(identity);
    }
    for (const annotation of state.annotations.slice(0, MAX_DOM_LABELS)) ensureNode(annotation);
    const cadVisibleSet = new Set(
      state.annotations
        .slice(0, MAX_DOM_LABELS)
        .filter((item) => item.anchor_type === "cad_anchor")
        .map((item) => item.annotation_id),
    );
    for (const identity of state.cadNodes.keys()) {
      if (!cadVisibleSet.has(identity)) removeNode(identity, state.cadNodes);
    }
    for (const annotation of state.annotations.slice(0, MAX_DOM_LABELS)) {
      if (annotation.anchor_type === "cad_anchor") ensureCadNode(annotation);
    }
    syncEditor();
  }

  async function refreshSnapshot() {
    if (!projectId) return;
    const snapshot = await request(`/api/projects/${encodeURIComponent(projectId)}/snapshot`, { headers: {} });
    state.snapshot = snapshot;
    state.annotationsRevision = Number(snapshot.component_revisions?.annotations || 0);
    state.clip = (snapshot.clips || []).find((item) => item.clip_id === state.clipId) || null;
    state.annotations = (snapshot.annotations || []).filter((item) => item.clip_id === state.clipId);
    if (state.selectedId && !selectedAnnotation()) state.selectedId = null;
    const range = clipPtsRange();
    if (range && !selectedAnnotation()) {
      editor.startPts.value = String(range.start_pts);
      editor.endPts.value = String(range.end_pts_exclusive);
    }
    await window.CadsceneAnnotationPts?.loadTrackingRevisions?.(state.annotations);
    reconcileNodes();
  }

  function makeStyle() {
    return {
      font_size_px: Number(editor.fontSize.value || 28),
      text_color: String(editor.color.value || "#ffffff").toUpperCase(),
      title_color: String(editor.titleColor.value || "#69d2ff").toUpperCase(),
      background_color: "#000000B3",
      background_opacity: Number(editor.backgroundOpacity.value || 0.7),
      border_color: "#FFFFFFCC",
      font_family: "sans-serif",
      font_weight: 600,
    };
  }

  async function createAnnotation(anchorType, anchor) {
    const range = clipPtsRange();
    if (!range) throw new Error("片段的权威 PTS 区间尚未加载");
    const body = await request(annotationPath(), {
      method: "POST",
      body: JSON.stringify({
        expected_revision: state.annotationsRevision,
        clip_id: state.clipId,
        anchor_type: anchorType,
        text: "点击编辑工程说明",
        content: { title: "工程标牌", body: "点击编辑工程说明" },
        panel: { width_px: 320, padding_px: 16, safe_margin_px: 20, border_radius_px: 6, shadow: true },
        leader: { line_width_px: 2, anchor_radius_px: 6, elbow_length_px: 24, anchor_shape: "circle" },
        anchor,
        source_pts_range: range,
        screen_offset: [190, -100],
        style: makeStyle(),
        user_visible: true,
      }),
    });
    state.annotationsRevision = Number(body.annotations_revision);
    state.selectedId = body.annotation.annotation_id;
    await refreshSnapshot();
    return body.annotation;
  }

  async function updateAnnotation(annotation, changes) {
    const body = await request(annotationPath(annotation.annotation_id), {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: state.annotationsRevision,
        expected_annotation_revision: annotation.annotation_revision,
        changes,
      }),
    });
    state.annotationsRevision = Number(body.annotations_revision);
    state.annotations = state.annotations.map((item) => (
      item.annotation_id === annotation.annotation_id ? body.annotation : item
    ));
    reconcileNodes();
    return body.annotation;
  }

  async function runTracking(annotation, correction = null) {
    setStatus(correction ? "正在从修正帧继续跟踪…" : "正在跟踪视频目标…");
    const body = await request(`${annotationPath(annotation.annotation_id)}/track`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: state.annotationsRevision,
        expected_annotation_revision: annotation.annotation_revision,
        ...(correction ? { correction } : {}),
      }),
    });
    state.annotationsRevision = Number(body.annotations_revision);
    await refreshSnapshot();
    setStatus(correction ? "修正跟踪已保存" : "目标跟踪已保存");
  }

  function activateMode(mode) {
    if (!video.paused) video.pause();
    state.mode = mode;
    document.querySelectorAll("[data-annotation-mode]").forEach((button) => {
      button.classList.toggle("is-active", button.dataset.annotationMode === mode);
    });
    setStatus(mode === "cad_anchor"
      ? "请在右侧 CAD 三维场景点击锚点"
      : "请在左侧视频拖框；单击会创建小范围 ROI");
  }

  function suppressVideoPlaybackWhileSelecting(event) {
    if (!new Set(["video_track", "video_track_reanchor"]).has(state.mode) && !state.suppressNextVideoClick) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    state.suppressNextVideoClick = false;
  }

  video.addEventListener("click", suppressVideoPlaybackWhileSelecting, true);

  sceneContainer.addEventListener("click", async (event) => {
    if (state.mode !== "cad_anchor") return;
    if (event.target.closest?.(".annotation-label")) return;
    event.preventDefault();
    event.stopPropagation();
    const pts = currentSourcePts();
    if (!Number.isInteger(pts)) return setStatus("当前帧还没有权威 source PTS");
    const picked = window.cadscenePickCadWorld?.(event);
    if (!picked?.cad_world_xyz) return setStatus("未选中 CAD 几何或地面");
    const projected = window.cadsceneProjectCadWorldPoint?.(picked.cad_world_xyz);
    const decision = window.CadsceneAnnotationPts?.cadAnchorCreationDecision?.(projected)
      || { ok: false, reason: "projection_unavailable" };
    if (!decision.ok) {
      setStatus(`所选 CAD 点当前不在视频画面内（${decision.reason}）；创建后右侧 CAD 中仍可编辑`);
    }
    try {
      await createAnnotation("cad_anchor", picked);
      state.mode = null;
      document.querySelectorAll("[data-annotation-mode]").forEach((button) => button.classList.remove("is-active"));
      focusSelectedAnnotationEditor();
      setStatus(decision.ok
        ? "CAD 标签已创建：输入文字后按 Enter 保存，也可拖动标牌位置"
        : `CAD 标签已创建；当前视频投影隐藏（${decision.reason}），右侧 CAD 中仍可编辑`);
    } catch (error) {
      setStatus(error.message);
    }
  }, true);

  videoLayer.addEventListener("pointerdown", (event) => {
    if (!new Set(["video_track", "video_track_reanchor"]).has(state.mode)) return;
    const rect = videoLayer.getBoundingClientRect();
    state.suppressNextVideoClick = true;
    state.selectionStart = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    updateRoiSelection(state.selectionStart, state.selectionStart);
    event.preventDefault();
  });

  function updateRoiSelection(start, finish) {
    if (!roiSelection) return;
    const left = Math.min(start.x, finish.x);
    const top = Math.min(start.y, finish.y);
    roiSelection.style.left = `${left}px`;
    roiSelection.style.top = `${top}px`;
    roiSelection.style.width = `${Math.max(1, Math.abs(finish.x - start.x))}px`;
    roiSelection.style.height = `${Math.max(1, Math.abs(finish.y - start.y))}px`;
    roiSelection.hidden = false;
  }

  videoLayer.addEventListener("pointermove", (event) => {
    if (!state.selectionStart) return;
    const rect = videoLayer.getBoundingClientRect();
    updateRoiSelection(state.selectionStart, {
      x: event.clientX - rect.left,
      y: event.clientY - rect.top,
    });
  });

  videoLayer.addEventListener("pointerup", async (event) => {
    if (!state.selectionStart || !new Set(["video_track", "video_track_reanchor"]).has(state.mode)) return;
    const start = state.selectionStart;
    state.selectionStart = null;
    if (roiSelection) roiSelection.hidden = true;
    const rect = videoLayer.getBoundingClientRect();
    const finish = { x: event.clientX - rect.left, y: event.clientY - rect.top };
    const transform = window.cadsceneVideoDisplayTransform;
    const a = transform?.displayToSource(start);
    const b = transform?.displayToSource(finish);
    if (!a || !b) return setStatus("请在实际视频画面内选择目标");
    const pts = currentSourcePts();
    if (!Number.isInteger(pts)) return setStatus("当前帧还没有权威 source PTS");
    const isClick = Math.abs(b.x - a.x) < 4 && Math.abs(b.y - a.y) < 4;
    const width = isClick ? DEFAULT_VIDEO_ROI_SIZE : Math.max(24, Math.abs(b.x - a.x));
    const height = isClick ? DEFAULT_VIDEO_ROI_SIZE : Math.max(24, Math.abs(b.y - a.y));
    const bbox = [
      isClick ? a.x - width / 2 : Math.min(a.x, b.x),
      isClick ? a.y - height / 2 : Math.min(a.y, b.y),
      width,
      height,
    ];
    try {
      if (state.mode === "video_track_reanchor") {
        const annotation = selectedAnnotation();
        if (!annotation) return;
        await runTracking(annotation, { source_pts: pts, bbox });
      } else {
        const annotation = await createAnnotation("video_track", {
          initialization: { source_pts: pts, bbox },
        });
        state.pendingInitialTrackingId = annotation.annotation_id;
        focusSelectedAnnotationEditor();
        setStatus("视频标签已创建：输入文字并按 Enter 保存后开始跟踪");
      }
      state.mode = null;
      document.querySelectorAll("[data-annotation-mode]").forEach((button) => button.classList.remove("is-active"));
    } catch (error) {
      setStatus(error.message);
    }
  });

  function beginLabelDrag(event) {
    const annotation = state.annotations.find((item) => item.annotation_id === event.currentTarget.dataset.annotationId);
    if (!annotation) return;
    event.preventDefault();
    event.stopPropagation();
    selectAnnotation(annotation.annotation_id);
    state.drag = {
      annotation,
      startX: event.clientX,
      startY: event.clientY,
      offset: [...annotation.screen_offset],
    };
    event.currentTarget.classList.add("is-dragging");
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  window.addEventListener("pointermove", (event) => {
    if (!state.drag) return;
    const delta = window.CadsceneAnnotationPts?.displayDeltaToSource?.(
      event.clientX - state.drag.startX,
      event.clientY - state.drag.startY,
    ) || { x: event.clientX - state.drag.startX, y: event.clientY - state.drag.startY };
    state.drag.previewOffset = [state.drag.offset[0] + delta.x, state.drag.offset[1] + delta.y];
    scheduleRender();
  });

  window.addEventListener("pointerup", async () => {
    if (!state.drag) return;
    const drag = state.drag;
    state.drag = null;
    state.nodes.get(drag.annotation.annotation_id)?.label.classList.remove("is-dragging");
    state.cadNodes.get(drag.annotation.annotation_id)?.label.classList.remove("is-dragging");
    if (!drag.previewOffset) return;
    try {
      await updateAnnotation(drag.annotation, { screen_offset: drag.previewOffset });
      setStatus("标签位置已保存；不会重新跟踪");
    } catch (error) {
      setStatus(error.message);
    }
  });

  function scheduleRender() {
    if (state.renderQueued) return;
    state.renderQueued = true;
    requestAnimationFrame(renderAnnotations);
  }

  function styleCalloutEntry(entry, annotation, displayScale) {
    const panelWidth = Number(annotation.panel?.width_px ?? 320);
    const padding = Number(annotation.panel?.padding_px ?? 16);
    const radius = Number(annotation.panel?.border_radius_px ?? 6);
    entry.title.textContent = annotation.content?.title || "";
    entry.body.textContent = annotation.content?.body ?? annotation.text ?? "";
    entry.label.style.width = `${panelWidth * displayScale}px`;
    entry.label.style.fontSize = `${Number(annotation.style?.font_size_px ?? 28) * displayScale}px`;
    entry.label.style.color = annotation.style?.text_color || "#FFFFFF";
    entry.title.style.color = annotation.style?.title_color || "#69D2FF";
    entry.label.style.backgroundColor = colorWithOpacity(
      annotation.style?.background_color || "#000000B3",
      annotation.style?.background_opacity ?? 0.7,
    );
    entry.label.style.borderColor = annotation.style?.border_color || "#FFFFFFCC";
    entry.label.style.padding = `${padding * displayScale}px`;
    entry.label.style.borderRadius = `${radius * displayScale}px`;
    entry.label.style.borderWidth = `${Math.max(1, Number(annotation.leader?.line_width_px ?? 2) * displayScale)}px`;
    entry.label.classList.toggle("has-shadow", annotation.panel?.shadow !== false);
    entry.label.style.boxShadow = annotation.panel?.shadow === false ? "none"
      : `0 ${8 * displayScale}px ${28 * displayScale}px rgba(0, 0, 0, .38), 0 0 ${12 * displayScale}px rgba(86, 194, 255, .14)`;
    return panelWidth;
  }

  function setCalloutVisibility(entry, visible) {
    entry.label.hidden = !visible;
    entry.dot.hidden = !visible;
    // SVG 元素没有会反射为 hidden 属性的 .hidden 属性，必须显式切换属性。
    entry.line.toggleAttribute("hidden", !visible);
  }

  function renderCadInspectCallout(annotation, provider, displayScale, dragOffset) {
    const entry = ensureCadNode(annotation);
    const projected = window.cadsceneProjectCadWorldToInspect?.(
      annotation.anchor?.cad_world_xyz,
    );
    const visible = Boolean(
      annotation.user_visible !== false && projected?.visible && Array.isArray(projected.xy),
    );
    setCalloutVisibility(entry, visible);
    if (!visible) return;
    const panelWidth = styleCalloutEntry(entry, annotation, displayScale);
    const anchor = projected.xy.map(Number);
    const offset = dragOffset || annotation.screen_offset || [0, 0];
    const displayOffset = provider?.sourceDeltaToDisplay?.(
      Number(offset[0]),
      Number(offset[1]),
    ) || { x: Number(offset[0]), y: Number(offset[1]) };
    const layout = window.CadsceneCalloutLayout.layoutCallout({
      anchor_xy: anchor,
      screen_offset: [displayOffset.x, displayOffset.y],
      panel_size: [panelWidth * displayScale, entry.label.offsetHeight],
      viewport_size: [sceneContainer.clientWidth, sceneContainer.clientHeight],
      safe_margin: Number(annotation.panel?.safe_margin_px ?? 20) * displayScale,
      elbow_length: Number(annotation.leader?.elbow_length_px ?? 24) * displayScale,
    });
    entry.label.style.left = `${layout.panel_rect[0]}px`;
    entry.label.style.top = `${layout.panel_rect[1]}px`;
    entry.dot.style.left = `${anchor[0]}px`;
    entry.dot.style.top = `${anchor[1]}px`;
    entry.polyline.setAttribute(
      "points",
      layout.leader_points.map((point) => `${point[0]},${point[1]}`).join(" "),
    );
    entry.polyline.setAttribute("stroke", annotation.style?.border_color || "#FFFFFFCC");
    entry.polyline.setAttribute(
      "stroke-width",
      String(Number(annotation.leader?.line_width_px ?? 2) * displayScale),
    );
    const anchorRadius = Number(annotation.leader?.anchor_radius_px ?? 6) * displayScale;
    entry.dot.style.width = `${2 * anchorRadius}px`;
    entry.dot.style.height = `${2 * anchorRadius}px`;
    entry.dot.style.margin = `${-anchorRadius}px 0 0 ${-anchorRadius}px`;
    entry.dot.classList.toggle("is-crosshair", annotation.leader?.anchor_shape === "crosshair");
  }

  function renderAnnotations() {
    state.renderQueued = false;
    const provider = window.CadsceneAnnotationPts;
    for (const annotation of state.annotations.slice(0, MAX_DOM_LABELS)) {
      const entry = ensureNode(annotation);
      const dragOffset = state.drag?.annotation.annotation_id === annotation.annotation_id
        ? state.drag.previewOffset : null;
      const visual = provider?.visualFor?.(annotation, { screenOffset: dragOffset });
      const scale = provider?.sourceDeltaToDisplay?.(1, 1) || { x: 1, y: 1 };
      const displayScale = Math.max(0.0001, Math.min(Math.abs(scale.x || 1), Math.abs(scale.y || 1)));
      if (annotation.anchor_type === "cad_anchor") {
        renderCadInspectCallout(annotation, provider, displayScale, dragOffset);
      }
      const visible = Boolean(visual?.visible && annotation.user_visible !== false);
      setCalloutVisibility(entry, visible);
      if (!visible) {
        if (annotation.annotation_id === state.selectedId && trackingState) {
          trackingState.textContent = annotation.anchor_type === "cad_anchor"
            ? `CAD 投影已隐藏：${visual?.reason || "projection_invalid"}`
            : `视频标牌已隐藏：${visual?.reason || "tracking_lost"}`;
        }
        continue;
      }
      const anchor = visual.anchor_source_xy;
      const label = visual.label_source_xy;
      entry.title.textContent = annotation.content?.title || "";
      entry.body.textContent = annotation.content?.body ?? annotation.text ?? "";
      const panelWidth = Number(annotation.panel?.width_px ?? 320);
      const padding = Number(annotation.panel?.padding_px ?? 16);
      const radius = Number(annotation.panel?.border_radius_px ?? 6);
      entry.label.style.width = `${panelWidth * displayScale}px`;
      entry.label.style.fontSize = `${Number(annotation.style?.font_size_px ?? 28) * displayScale}px`;
      entry.label.style.color = annotation.style?.text_color || "#FFFFFF";
      entry.title.style.color = annotation.style?.title_color || "#69D2FF";
      entry.label.style.backgroundColor = colorWithOpacity(
        annotation.style?.background_color || "#000000B3",
        annotation.style?.background_opacity ?? 0.7,
      );
      entry.label.style.borderColor = annotation.style?.border_color || "#FFFFFFCC";
      entry.label.style.padding = `${padding * displayScale}px`;
      entry.label.style.borderRadius = `${radius * displayScale}px`;
      entry.label.style.borderWidth = `${Math.max(1, Number(annotation.leader?.line_width_px ?? 2) * displayScale)}px`;
      entry.label.classList.toggle("has-shadow", annotation.panel?.shadow !== false);
      entry.label.style.boxShadow = annotation.panel?.shadow === false ? "none"
        : `0 ${8 * displayScale}px ${28 * displayScale}px rgba(0, 0, 0, .38), 0 0 ${12 * displayScale}px rgba(86, 194, 255, .14)`;
      const layout = window.CadsceneCalloutLayout.layoutCallout({
        anchor_xy: anchor,
        screen_offset: [label[0] - anchor[0], label[1] - anchor[1]],
        panel_size: [panelWidth, entry.label.offsetHeight / displayScale],
        viewport_size: [video.videoWidth, video.videoHeight],
        safe_margin: Number(annotation.panel?.safe_margin_px ?? 20),
        elbow_length: Number(annotation.leader?.elbow_length_px ?? 24),
      });
      const panelOrigin = provider.sourceToDisplayPoint(layout.panel_rect.slice(0, 2));
      const anchorDisplay = provider.sourceToDisplayPoint(anchor);
      const leaderPoints = layout.leader_points.map((point) => provider.sourceToDisplayPoint(point));
      entry.label.style.left = `${panelOrigin.x}px`;
      entry.label.style.top = `${panelOrigin.y}px`;
      entry.dot.style.left = `${anchorDisplay.x}px`;
      entry.dot.style.top = `${anchorDisplay.y}px`;
      entry.polyline.setAttribute("points", leaderPoints.map((point) => `${point.x},${point.y}`).join(" "));
      entry.polyline.setAttribute("stroke", annotation.style?.border_color || "#FFFFFFCC");
      entry.polyline.setAttribute("stroke-width", String(Number(annotation.leader?.line_width_px ?? 2) * displayScale));
      const anchorRadius = Number(annotation.leader?.anchor_radius_px ?? 6) * displayScale;
      entry.dot.style.width = `${2 * anchorRadius}px`;
      entry.dot.style.height = `${2 * anchorRadius}px`;
      entry.dot.style.margin = `${-anchorRadius}px 0 0 ${-anchorRadius}px`;
      entry.dot.classList.toggle("is-crosshair", annotation.leader?.anchor_shape === "crosshair");
      if (annotation.annotation_id === state.selectedId && trackingState) {
        trackingState.textContent = annotation.anchor_type === "video_track"
          ? `跟踪状态：${visual.tracking_status || "—"} · 置信度 ${Number(visual.confidence || 0).toFixed(2)}`
          : `CAD 投影：${visual.reason || "visible"}`;
      }
    }
    window.setTimeout(scheduleRender, video.paused ? 120 : 33);
  }

  document.querySelectorAll("[data-annotation-mode]").forEach((button) => {
    button.addEventListener("click", () => activateMode(button.dataset.annotationMode));
  });
  editor.reanchor?.addEventListener("click", () => activateMode("video_track_reanchor"));
  editor.remove?.addEventListener("click", async () => {
    const annotation = selectedAnnotation();
    if (!annotation) return;
    try {
      const body = await request(annotationPath(annotation.annotation_id), {
        method: "DELETE",
        body: JSON.stringify({
          expected_revision: state.annotationsRevision,
          expected_annotation_revision: annotation.annotation_revision,
        }),
      });
      state.annotationsRevision = Number(body.annotations_revision);
      state.selectedId = null;
      await refreshSnapshot();
      setStatus("标签已删除");
    } catch (error) {
      setStatus(error.message);
    }
  });
  async function saveSelectedAnnotation() {
    const annotation = selectedAnnotation();
    if (!annotation) return;
    try {
      const updated = await updateAnnotation(annotation, {
        content: { title: editor.title.value, body: editor.body.value },
        panel: { ...annotation.panel, width_px: Number(editor.panelWidth.value || 320) },
        style: makeStyle(),
        source_pts_range: {
          ...annotation.source_pts_range,
          start_pts: Number(editor.startPts.value),
          end_pts_exclusive: Number(editor.endPts.value),
        },
        user_visible: editor.visible.checked,
      });
      if (state.pendingInitialTrackingId === updated.annotation_id) {
        await runTracking(updated);
        state.pendingInitialTrackingId = null;
      } else {
        setStatus("文字与样式已保存；不会重新跟踪");
      }
    } catch (error) {
      setStatus(error.message);
    }
  }

  editor.save?.addEventListener("click", saveSelectedAnnotation);
  editor.title?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    event.preventDefault();
    await saveSelectedAnnotation();
  });
  editor.body?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" || !event.ctrlKey || event.isComposing) return;
    event.preventDefault();
    await saveSelectedAnnotation();
  });

  async function initialize() {
    if (!token || !projectId) {
      setStatus("标签工具仅在项目片段工作台中启用");
      document.querySelectorAll("#annotationToolbar button, #annotationToolbar input").forEach((node) => { node.disabled = true; });
      return;
    }
    try {
      const session = await request(
        `/api/projects/${encodeURIComponent(projectId)}/workbench-sessions/${encodeURIComponent(token)}`,
        { headers: {} },
      );
      state.clipId = session.clip_id;
      await window.CadsceneAnnotationPts?.configure?.(projectId, state.clipId);
      await refreshSnapshot();
      setStatus("暂停视频后可创建 CAD 或视频目标标签");
      scheduleRender();
    } catch (error) {
      setStatus(error.message);
    }
  }

  initialize();
})();
