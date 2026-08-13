(function () {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const token = params.get("projectWorkbenchToken") || "";
  const projectId = params.get("projectId") || params.get("dataset") || "";
  const video = document.querySelector("#sourceVideo");
  const videoLayer = video?.closest(".video-layer");
  const sceneContainer = document.querySelector("#sceneContainer");
  const overlay = document.querySelector("#annotationOverlay");
  const roiSelection = document.querySelector("#annotationRoiSelection");
  const status = document.querySelector("#annotationToolStatus");
  const trackingState = document.querySelector("#annotationTrackingState");
  const editor = {
    text: document.querySelector("#annotationText"),
    fontSize: document.querySelector("#annotationFontSize"),
    color: document.querySelector("#annotationTextColor"),
    startPts: document.querySelector("#annotationStartPts"),
    endPts: document.querySelector("#annotationEndPts"),
    visible: document.querySelector("#annotationVisible"),
    save: document.querySelector("#annotationSave"),
    remove: document.querySelector("#annotationDelete"),
    reanchor: document.querySelector("#annotationReanchor"),
  };
  if (!video || !videoLayer || !sceneContainer || !overlay) return;

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
    drag: null,
    renderQueued: false,
    pendingInitialTrackingId: null,
  };
  const MAX_DOM_LABELS = 250;
  const DEFAULT_VIDEO_ROI_SIZE = 64;

  function setStatus(message) {
    if (status) status.textContent = message;
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
    for (const control of [editor.text, editor.fontSize, editor.color, editor.startPts, editor.endPts, editor.visible]) {
      if (control) control.disabled = !enabled;
    }
    if (editor.save) editor.save.disabled = !enabled;
    if (editor.remove) editor.remove.disabled = !enabled;
    if (editor.reanchor) editor.reanchor.disabled = !annotation || annotation.anchor_type !== "video_track";
    if (!annotation) return;
    editor.text.value = annotation.text || "";
    editor.fontSize.value = String(annotation.style?.font_size_px || 28);
    editor.color.value = String(annotation.style?.text_color || "#FFFFFF").slice(0, 7);
    editor.startPts.value = String(annotation.source_pts_range.start_pts);
    editor.endPts.value = String(annotation.source_pts_range.end_pts_exclusive);
    editor.visible.checked = annotation.user_visible !== false;
  }

  function selectAnnotation(annotationId) {
    state.selectedId = annotationId;
    for (const [identity, entry] of state.nodes) {
      entry.label.classList.toggle("is-selected", identity === annotationId);
    }
    syncEditor();
  }

  function focusSelectedAnnotationEditor() {
    if (!editor.text || !selectedAnnotation()) return;
    editor.text.focus();
    editor.text.select();
  }

  function removeNode(annotationId) {
    const entry = state.nodes.get(annotationId);
    if (!entry) return;
    entry.label.remove();
    entry.line.remove();
    entry.dot.remove();
    state.nodes.delete(annotationId);
  }

  function ensureNode(annotation) {
    let entry = state.nodes.get(annotation.annotation_id);
    if (entry) return entry;
    const line = document.createElement("div");
    line.className = "annotation-connector";
    const dot = document.createElement("div");
    dot.className = "annotation-anchor-dot";
    const label = document.createElement("div");
    label.className = "annotation-label";
    label.dataset.annotationId = annotation.annotation_id;
    label.addEventListener("pointerdown", beginLabelDrag);
    label.addEventListener("click", (event) => {
      event.stopPropagation();
      selectAnnotation(annotation.annotation_id);
    });
    overlay.append(line, dot, label);
    entry = { line, dot, label };
    state.nodes.set(annotation.annotation_id, entry);
    return entry;
  }

  function reconcileNodes() {
    const visibleSet = new Set(
      state.annotations.slice(0, MAX_DOM_LABELS).map((item) => item.annotation_id),
    );
    for (const identity of state.nodes.keys()) {
      if (!visibleSet.has(identity)) removeNode(identity);
    }
    for (const annotation of state.annotations.slice(0, MAX_DOM_LABELS)) ensureNode(annotation);
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
      background_color: "#000000B3",
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
        text: editor.text.value.trim() || "新标签",
        anchor,
        source_pts_range: range,
        screen_offset: [18, -18],
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

  sceneContainer.addEventListener("click", async (event) => {
    if (state.mode !== "cad_anchor") return;
    event.preventDefault();
    event.stopPropagation();
    const pts = currentSourcePts();
    if (!Number.isInteger(pts)) return setStatus("当前帧还没有权威 source PTS");
    const picked = window.cadscenePickCadWorld?.(event);
    if (!picked?.cad_world_xyz) return setStatus("未选中 CAD 几何或地面");
    try {
      await createAnnotation("cad_anchor", picked);
      state.mode = null;
      document.querySelectorAll("[data-annotation-mode]").forEach((button) => button.classList.remove("is-active"));
      focusSelectedAnnotationEditor();
      setStatus("CAD 标签已创建：输入文字后按 Enter 保存，也可在左侧视频拖动标签位置");
    } catch (error) {
      setStatus(error.message);
    }
  }, true);

  videoLayer.addEventListener("pointerdown", (event) => {
    if (!new Set(["video_track", "video_track_reanchor"]).has(state.mode)) return;
    const rect = videoLayer.getBoundingClientRect();
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

  function renderAnnotations() {
    state.renderQueued = false;
    const provider = window.CadsceneAnnotationPts;
    for (const annotation of state.annotations.slice(0, MAX_DOM_LABELS)) {
      const entry = ensureNode(annotation);
      const dragOffset = state.drag?.annotation.annotation_id === annotation.annotation_id
        ? state.drag.previewOffset : null;
      const visual = provider?.visualFor?.(annotation, { screenOffset: dragOffset });
      const visible = Boolean(visual?.visible && annotation.user_visible !== false);
      entry.label.hidden = entry.line.hidden = entry.dot.hidden = !visible;
      if (!visible) continue;
      const anchor = visual.anchor_xy;
      const label = visual.label_xy;
      entry.label.textContent = annotation.text;
      entry.label.style.left = `${label[0]}px`;
      entry.label.style.top = `${label[1]}px`;
      entry.label.style.fontSize = `${annotation.style?.font_size_px || 28}px`;
      entry.label.style.color = annotation.style?.text_color || "#FFFFFF";
      entry.dot.style.left = `${anchor[0]}px`;
      entry.dot.style.top = `${anchor[1]}px`;
      const dx = label[0] - anchor[0];
      const dy = label[1] - anchor[1];
      entry.line.style.left = `${anchor[0]}px`;
      entry.line.style.top = `${anchor[1]}px`;
      entry.line.style.width = `${Math.hypot(dx, dy)}px`;
      entry.line.style.transform = `rotate(${Math.atan2(dy, dx)}rad)`;
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
        text: editor.text.value,
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
  editor.text?.addEventListener("keydown", async (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
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
