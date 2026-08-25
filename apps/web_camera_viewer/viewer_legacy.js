(function () {
  console.log("THREE available:", !!window.THREE);
  console.log("OrbitControls available:", !!(window.THREE && window.THREE.OrbitControls));
  const VIEWER_PATHS = window.resolveViewerPaths();
  const VIDEO_SRC = VIEWER_PATHS.video;
  const VIDEO_FALLBACKS = VIEWER_PATHS.videoFallbacks || [];
  // camera_track.json 里记录的 video 字段使用 ../ 前缀以外的相对路径，仅作元数据。
  const VIDEO_PATH = VIDEO_SRC.replace(/^\.\.\//, "");
  const CAD_PATH = VIEWER_PATHS.cad;
  const CAD_FALLBACKS = VIEWER_PATHS.cadFallbacks || [];
  const TRACK_PATH = VIEWER_PATHS.track;
  const TRACK_FALLBACKS = VIEWER_PATHS.trackFallbacks || [];
  const CAMERA_PATH = VIEWER_PATHS.camera;
  const REVIEW_PATH = VIEWER_PATHS.review;
  const INITIAL_FRAME_VALUE = new URLSearchParams(window.location.search).get("initialFrame");
  const DEFAULT_FPS = 23.976;
  const NEAR_PLANE = 0.1;
  const MAX_ACTIVE_CAD_TEXT_LABELS = 240;
  const MAX_CAD_TEXT_TEXTURES = 384;
  const MAX_PROJECTED_CAD_TEXT_CANDIDATES = 5000;
  const CAD_TEXT_VISUAL_SCALE = 1.2;
  const MAX_CAD_TEXT_GRID_AXIS = 32;
  const CAD_TEXT_REFRESH_MS = 120;
  const CAD_GIZMO_OVERLAY_REFRESH_MS = 120;
  const MAX_INTERACTIVE_OVERLAY_TEXT_CANDIDATES = 5000;
  const CAD_FOCUS_LAYER_GROUPS = [
    /中心线|道路|路面|标线|road|centerline|alignment/i,
    /建筑|总平|红线|building|site\s*plan|parcel/i,
  ];

  const video = document.querySelector("#sourceVideo");
  const videoLayer = video.closest(".video-layer");
  const overlayCanvas = document.querySelector("#overlayCanvas");
  const overlayContext = overlayCanvas.getContext("2d");
  const sceneContainer = document.querySelector("#sceneContainer");
  const controlContainer = document.querySelector("#cameraControls");
  const loadStatus = document.querySelector("#loadStatus");
  const drawStatus = document.querySelector("#drawStatus");
  const fallbackStatus = document.querySelector("#fallbackStatus");
  const videoInfo = document.querySelector("#videoInfo");
  const sceneHint = document.querySelector("#sceneHint");
  const fallbackCanvas = document.querySelector("#fallbackSceneCanvas");
  const timeStatus = document.querySelector("#timeStatus");
  const frameStatus = document.querySelector("#frameStatus");
  const keyframeStatus = document.querySelector("#keyframeStatus");
  const currentKeyframeStatus = document.querySelector("#currentKeyframeStatus");
  const reviewStatus = document.querySelector("#reviewStatus");
  const qualityCanvas = document.querySelector("#qualityTimelineCanvas");
  const qualityContext = qualityCanvas ? qualityCanvas.getContext("2d") : null;
  const qualityTip = document.querySelector("#qualityTimelineTip");
  const qualitySummary = document.querySelector("#qualityTimelineSummary");
  const qualityWrap = document.querySelector("#qualityTimelineWrap");
  const videoDisplayTransform = new window.VideoDisplayTransform();

  function updateVideoDisplayTransform() {
    const rect = videoLayer.getBoundingClientRect();
    videoDisplayTransform.update(video.videoWidth, video.videoHeight, rect.width, rect.height);
    overlayCanvas.style.left = `${videoDisplayTransform.offsetX}px`;
    overlayCanvas.style.top = `${videoDisplayTransform.offsetY}px`;
    overlayCanvas.style.width = `${videoDisplayTransform.displayWidth}px`;
    overlayCanvas.style.height = `${videoDisplayTransform.displayHeight}px`;
    return videoDisplayTransform;
  }

  function sourceToDisplay(point) {
    return videoDisplayTransform.sourceToDisplay(point);
  }

  function displayToSource(point) {
    return videoDisplayTransform.displayToSource(point);
  }

  window.cadsceneVideoDisplayTransform = {
    sourceToDisplay,
    displayToSource,
    update: updateVideoDisplayTransform,
  };

  const controlDefs = [
    { key: "x", label: "水平 X", step: 1 },
    { key: "y", label: "水平 Y", step: 1 },
    { key: "z", label: "高度 Z", min: 5, max: 1000, step: 1 },
    { key: "yaw", label: "偏航", min: -180, max: 180, step: 1 },
    { key: "pitch", label: "俯仰", min: -89, max: 10, step: 1 },
    { key: "roll", label: "滚转", min: -180, max: 180, step: 1 },
    { key: "fov", label: "视场角", min: 20, max: 130, step: 1 },
  ];

  let cadData = null;
  let camera = null;
  let defaultCamera = null;
  let threeScene = null;
  let cameraTrack = null;
  let reviewPacket = null;
  let manualFrameOverride = null;
  let lastOverlayDrawAt = 0;
  let videoRangeSupported = null;
  let showCadText = true;
  let cadTextEntityCount = 0;
  const OVERLAY_INTERVAL_PLAYING_MS = 200;
  const controlInputs = new Map();
  const lockableCameraFields = new Set(["z", "yaw", "pitch", "roll", "fov"]);
  const lockedCameraFields = { z: false, yaw: false, pitch: false, roll: false, fov: false };
  const pureRotationRestrictedFields = new Set();
  let pureRotationPlaybackActive = false;
  let pureRotationAuthoritativeMatrix = null;

  // 质量评估建议帧（keyframe_suggestions.json 或带 quality 的 track 归纳而来），仅用于可视化。
  let qualitySuggestions = [];
  // workflow 记录的已忽略建议只影响建议标记，不会抹掉质量风险色带。
  let ignoredSuggestionFrames = new Set();
  // 质量时间线（quality_timeline.csv），用于左侧进度条低/中/高风险色带。
  let qualityTimelineRows = [];
  // 关键帧计划独立于相机轨迹：仅提示待人工标定帧，绝不作为 Sim3 锚点。
  let keyframePlanFrames = [];
  // 进度条上每个建议标记的命中区间，供点击/悬停命中测试使用。
  let qualityMarkerHits = [];
  const RISK_COLORS = { low: "#39b54a", medium: "#f4c20d", high: "#e03131" };
  const SUGGESTIONS_PATH = VIEWER_PATHS.suggestions;
  const QUALITY_TIMELINE_PATH = VIEWER_PATHS.qualityTimeline
    || (SUGGESTIONS_PATH ? SUGGESTIONS_PATH.replace(/keyframe_suggestions\.json(?:\?.*)?$/, "quality_timeline.csv") : "");
  // SfM 诊断场景（点云 + 双轨迹 + 建议），仅用于右侧 3D 视图展示。
  let sfmScene = null;
  const SFM_SCENE_PATH = VIEWER_PATHS.sfmScene;
  const DIAGNOSTICS_SCENE_PATH = VIEWER_PATHS.diagnosticsScene;
  let diagnosticsScene = null;

  function suggestionFrameIndex(suggestion) {
    return Number(suggestion?.frame ?? suggestion?.frame_index);
  }

  function visibleQualitySuggestions(suggestions) {
    return (suggestions || []).filter((suggestion) => {
      const frame = suggestionFrameIndex(suggestion);
      return !Number.isFinite(frame) || !ignoredSuggestionFrames.has(frame);
    });
  }

  function setStatus(message) {
    loadStatus.textContent = message;
  }

  function degToRad(degrees) {
    return (degrees * Math.PI) / 180;
  }

  function radToDeg(radians) {
    return (radians * 180) / Math.PI;
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function isManualKeyframe(keyframe) {
    return window.CadsceneKeyframes.isConfirmedManualKeyframe(keyframe);
  }

  async function fetchJsonWithFallback(paths, label) {
    let lastError = null;
    for (const path of paths.filter(Boolean)) {
      try {
        const response = await fetch(path);
        if (response.ok) {
          console.info(`[cadscene viewer] loaded ${label}: ${path}`);
          return await response.json();
        }
        lastError = new Error(`${label} ${response.status}: ${path}`);
        console.warn(`[cadscene viewer] ${label} HTTP ${response.status}: ${path}`);
      } catch (error) {
        lastError = error;
        console.warn(`[cadscene viewer] ${label} load failed: ${path}`, error);
      }
    }
    throw lastError || new Error(`${label} load failed`);
  }

  function bindVideoFallbacks() {
    const candidates = [VIDEO_SRC, ...VIDEO_FALLBACKS].filter(Boolean);
    let index = 0;
    video.addEventListener("error", () => {
      if (index >= candidates.length - 1) return;
      index += 1;
      console.warn(`[cadscene viewer] video failed, trying fallback: ${candidates[index]}`);
      video.src = candidates[index];
      video.load();
    });
  }

  function manualKeyframes() {
    return window.CadsceneKeyframes.confirmedManualKeyframes(cameraTrack?.keyframes || []);
  }

  function dot(a, b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  }

  function cross(a, b) {
    return [
      a[1] * b[2] - a[2] * b[1],
      a[2] * b[0] - a[0] * b[2],
      a[0] * b[1] - a[1] * b[0],
    ];
  }

  function length(v) {
    return Math.hypot(v[0], v[1], v[2]);
  }

  function normalize(v) {
    const value = length(v);
    if (value < 1e-9) return [0, 0, 0];
    return [v[0] / value, v[1] / value, v[2] / value];
  }

  function scale(v, value) {
    return [v[0] * value, v[1] * value, v[2] * value];
  }

  function add(a, b) {
    return [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
  }

  function rotateAroundAxis(vector, axis, angle) {
    const unitAxis = normalize(axis);
    const cos = Math.cos(angle);
    const sin = Math.sin(angle);
    return add(
      add(scale(vector, cos), scale(cross(unitAxis, vector), sin)),
      scale(unitAxis, dot(unitAxis, vector) * (1 - cos)),
    );
  }

  function getCameraAxes(params) {
    const axesFromAuthoritativeRotation = (matrix) => ({
      right: normalize([matrix[0][0], matrix[1][0], matrix[2][0]]),
      down: normalize([matrix[0][1], matrix[1][1], matrix[2][1]]),
      forward: normalize([matrix[0][2], matrix[1][2], matrix[2][2]]),
    });
    if (pureRotationPlaybackActive && pureRotationAuthoritativeMatrix && params === camera) {
      return axesFromAuthoritativeRotation(pureRotationAuthoritativeMatrix);
    }
    // CAD 在 XY 地面上，Z 向上；pitch 为负数时虚拟相机向下看。
    const yaw = degToRad(params.yaw || 0);
    const pitch = degToRad(params.pitch || 0);
    const roll = degToRad(params.roll || 0);
    const forward = normalize([
      Math.cos(pitch) * Math.sin(yaw),
      Math.cos(pitch) * Math.cos(yaw),
      Math.sin(pitch),
    ]);
    const worldUp = [0, 0, 1];
    let right = cross(forward, worldUp);
    if (length(right) < 1e-9) right = [1, 0, 0];
    right = normalize(right);
    let down = normalize(cross(forward, right));
    if (Math.abs(roll) > 1e-9) {
      right = normalize(rotateAroundAxis(right, forward, roll));
      down = normalize(rotateAroundAxis(down, forward, roll));
    }
    return { right, down, forward };
  }

  function clearPureRotationAuthoritativeMatrix() {
    pureRotationAuthoritativeMatrix = null;
  }

  function notifyManualCameraChanged(source) {
    window.dispatchEvent(new CustomEvent("cadsceneManualCameraChanged", {
      detail: { source },
    }));
  }

  function worldToCamera(point, params, axes) {
    const delta = [point[0] - params.x, point[1] - params.y, point[2] - params.z];
    return [dot(delta, axes.right), dot(delta, axes.down), dot(delta, axes.forward)];
  }

  function cameraIntrinsics(width, height, fovDegrees) {
    // 使用水平视场角计算焦距，和 Python 原型保持一致。
    const fov = degToRad(fovDegrees || 70);
    const focal = (width * 0.5) / Math.tan(fov * 0.5);
    return { fx: focal, fy: focal, cx: width * 0.5, cy: height * 0.5 };
  }

  function projectCameraPoint(point, intrinsics) {
    if (point[2] <= NEAR_PLANE) return null;
    return [
      intrinsics.fx * (point[0] / point[2]) + intrinsics.cx,
      intrinsics.fy * (point[1] / point[2]) + intrinsics.cy,
    ];
  }

  function clipSegmentToNear(a, b) {
    if (a[2] <= NEAR_PLANE && b[2] <= NEAR_PLANE) return null;
    if (a[2] > NEAR_PLANE && b[2] > NEAR_PLANE) return [a, b];
    const t = (NEAR_PLANE - a[2]) / (b[2] - a[2]);
    const clipped = [
      a[0] + t * (b[0] - a[0]),
      a[1] + t * (b[1] - a[1]),
      a[2] + t * (b[2] - a[2]),
    ];
    return a[2] <= NEAR_PLANE ? [clipped, b] : [a, clipped];
  }

  function getWorldPoints(entity) {
    if (Array.isArray(entity.world_points)) {
      return entity.world_points.map((point) => [point[0], point[1], 0]);
    }
    if (Array.isArray(entity.world_position)) {
      return [[entity.world_position[0], entity.world_position[1], 0]];
    }
    return [];
  }

  function colorFor(entity, layer) {
    return entity.color || layer.color || "#ffffff";
  }

  function projectPolyline(points, params, width, height) {
    const axes = getCameraAxes(params);
    const intrinsics = cameraIntrinsics(width, height, params.fov || 70);
    const cameraPoints = points.map((point) => worldToCamera(point, params, axes));
    const segments = [];
    for (let index = 0; index < cameraPoints.length - 1; index += 1) {
      const clipped = clipSegmentToNear(cameraPoints[index], cameraPoints[index + 1]);
      if (!clipped) continue;
      const start = projectCameraPoint(clipped[0], intrinsics);
      const end = projectCameraPoint(clipped[1], intrinsics);
      if (start && end) segments.push([start, end]);
    }
    return segments;
  }

  function projectPoint(point, params, width, height) {
    const axes = getCameraAxes(params);
    const intrinsics = cameraIntrinsics(width, height, params.fov || 70);
    return projectCameraPoint(worldToCamera(point, params, axes), intrinsics);
  }

  function projectedTextAngle(entity, params, width, height) {
    const base = getWorldPoints(entity)[0];
    if (!base) return 0;
    const rotation = degToRad(Number(entity.cad_rotation || 0));
    const lengthHint = Math.max(Number(entity.cad_height || 8) * 3, 8);
    const end = [
      base[0] + Math.cos(rotation) * lengthHint,
      base[1] + Math.sin(rotation) * lengthHint,
      base[2] || 0,
    ];
    const p0 = projectPoint(base, params, width, height);
    const p1 = projectPoint(end, params, width, height);
    if (!p0 || !p1) return 0;
    return Math.atan2(p1[1] - p0[1], p1[0] - p0[0]);
  }

  function fontSizeForText(entity, highQuality) {
    const cadHeight = Number(entity.cad_height || 0);
    const base = cadHeight >= 18 ? 22 : 16;
    return Math.max(10, Math.round(base * (highQuality ? 1 : 0.72)));
  }

  function cloneCameraPose(source) {
    const pose = {};
    for (const def of controlDefs) pose[def.key] = Number(source[def.key]);
    return pose;
  }

  function currentFrame() {
    if (manualFrameOverride !== null) return manualFrameOverride;
    return Math.round((video.currentTime || 0) * cameraTrack.fps);
  }

  function frameToTime(frame) {
    return frame / cameraTrack.fps;
  }

  function sortKeyframes() {
    cameraTrack.keyframes.sort((a, b) => a.frame - b.frame);
  }

  function findKeyframe(frame) {
    return cameraTrack.keyframes.find((keyframe) => keyframe.frame === frame);
  }

  function makeKeyframe(frame, pose, source) {
    const entry = { frame: Number(frame), time: frameToTime(Number(frame)), camera: cloneCameraPose(pose) };
    if (source) entry.source = source;
    return entry;
  }

  function upsertKeyframe(entry) {
    const frame = Number(entry.frame);
    const existing = findKeyframe(frame);
    if (existing) {
      existing.time = Number(entry.time ?? frameToTime(frame));
      existing.camera = cloneCameraPose(entry.camera);
      if (entry.source) existing.source = entry.source;
    } else {
      cameraTrack.keyframes.push({
        frame,
        time: Number(entry.time ?? frameToTime(frame)),
        source: entry.source,
        camera: cloneCameraPose(entry.camera),
      });
      sortKeyframes();
    }
  }

  function normalizeAngleDelta(delta) {
    return ((delta + 540) % 360) - 180;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function interpolateCameraAtFrame(frame) {
    const keyframes = cameraTrack.keyframes;
    if (keyframes.length === 0) return cloneCameraPose(camera);
    if (keyframes.length === 1 || frame <= keyframes[0].frame) return cloneCameraPose(keyframes[0].camera);
    const last = keyframes[keyframes.length - 1];
    if (frame >= last.frame) return cloneCameraPose(last.camera);

    let previous = keyframes[0];
    let next = last;
    for (let index = 0; index < keyframes.length - 1; index += 1) {
      if (frame >= keyframes[index].frame && frame <= keyframes[index + 1].frame) {
        previous = keyframes[index];
        next = keyframes[index + 1];
        break;
      }
    }

    const t = (frame - previous.frame) / Math.max(1, next.frame - previous.frame);
    const pose = {};
    for (const key of ["x", "y", "z", "pitch", "roll", "fov"]) {
      pose[key] = lerp(previous.camera[key], next.camera[key], t);
    }
    pose.yaw = previous.camera.yaw + normalizeAngleDelta(next.camera.yaw - previous.camera.yaw) * t;
    return pose;
  }

  // 纯 SfM 预测：直接用 sfm_viewer_scene.json 的 global_sfm_track（仅 global sim3，未分段锚定）
  // 逐帧插值出相机位姿，用于"把虚拟相机放到原始 SfM 轨迹上"对照观察。
  let sfmFollowMode = false;

  function interpolateSfmPoseAtFrame(frame) {
    const track = (sfmScene && sfmScene.tracks && sfmScene.tracks.global_sfm_track) || [];
    if (track.length === 0) return null;
    const fi = (e) => Number(e.frame_index ?? e.frame ?? 0);
    if (frame <= fi(track[0])) return sfmEntryToPose(track[0]);
    const lastEntry = track[track.length - 1];
    if (frame >= fi(lastEntry)) return sfmEntryToPose(lastEntry);
    let a = track[0];
    let b = lastEntry;
    for (let i = 0; i < track.length - 1; i += 1) {
      if (frame >= fi(track[i]) && frame <= fi(track[i + 1])) {
        a = track[i];
        b = track[i + 1];
        break;
      }
    }
    const t = (frame - fi(a)) / Math.max(1, fi(b) - fi(a));
    const pose = {
      x: lerp(a.x, b.x, t),
      y: lerp(a.y, b.y, t),
      z: lerp(a.z, b.z, t),
      pitch: lerp(a.pitch, b.pitch, t),
      roll: lerp(a.roll, b.roll, t),
      fov: lerp(a.fov ?? camera.fov, b.fov ?? camera.fov, t),
    };
    pose.yaw = a.yaw + normalizeAngleDelta(b.yaw - a.yaw) * t;
    return pose;
  }

  function sfmEntryToPose(e) {
    return {
      x: e.x, y: e.y, z: e.z,
      yaw: e.yaw, pitch: e.pitch, roll: e.roll,
      fov: e.fov ?? camera.fov,
    };
  }

  // 统一入口：SfM 跟随模式下用原始 SfM 轨迹，否则用已加载的（人工/预测）关键帧轨迹。
  function poseForFrame(frame) {
    if (sfmFollowMode) {
      const p = interpolateSfmPoseAtFrame(frame);
      if (p) return p;
    }
    return interpolateCameraAtFrame(frame);
  }

  function applyCameraLocks(nextPose, previousPose) {
    // TransformControls 会同时改变位置和姿态；锁定字段时保留拖动前的值，
    // 便于只在水平面移动或只调某几个自由度。
    if (!previousPose) return nextPose;
    const pose = { ...nextPose };
    for (const key of Object.keys(lockedCameraFields)) {
      if (lockedCameraFields[key]) pose[key] = previousPose[key];
    }
    for (const key of pureRotationRestrictedFields) pose[key] = previousPose[key];
    return pose;
  }

  function currentTrackAsAnchoredPath() {
    // 右侧 3D 的“锚定后轨迹”应跟随当前 URL/导入的 track，而不是 sfm_scene 内嵌旧轨迹。
    const rows = (cameraTrack?.keyframes || [])
      .filter((kf) => kf.camera && Number.isFinite(Number(kf.frame)))
      .map((kf) => ({
        frame_index: Number(kf.frame),
        x: Number(kf.camera.x),
        y: Number(kf.camera.y),
        z: Number(kf.camera.z),
        yaw: Number(kf.camera.yaw || 0),
        pitch: Number(kf.camera.pitch || 0),
        roll: Number(kf.camera.roll || 0),
        fov: Number(kf.camera.fov || 70),
        source: kf.source || "",
      }));
    rows.sort((a, b) => a.frame_index - b.frame_index);
    return rows;
  }

  function syncSfmAnchoredTrackFromCurrentTrack() {
    if (!sfmScene) return;
    const anchored = currentTrackAsAnchoredPath();
    if (anchored.length > 0) {
      sfmScene.tracks = sfmScene.tracks || {};
      sfmScene.tracks.anchored_camera_path = anchored;
      if (threeScene && threeScene.setAnchoredTrackData) threeScene.setAnchoredTrackData(anchored);
    }
    updateSfmInfoPanel();
    updateSfmCurrentFrameInfo(currentFrame());
  }

  function createInitialTrack(initialPose) {
    return {
      version: 1,
      video: VIDEO_PATH,
      fps: DEFAULT_FPS,
      keyframes: [{ frame: 0, time: 0, camera: cloneCameraPose(initialPose) }],
    };
  }

  function resizeOverlayCanvas(highQuality) {
    if (!video.videoWidth || !video.videoHeight) return false;
    updateVideoDisplayTransform();
    const scale = highQuality ? 1 : 0.5;
    const targetWidth = Math.max(1, Math.round(video.videoWidth * scale));
    const targetHeight = Math.max(1, Math.round(video.videoHeight * scale));
    if (overlayCanvas.width !== targetWidth || overlayCanvas.height !== targetHeight) {
      overlayCanvas.width = targetWidth;
      overlayCanvas.height = targetHeight;
    }
    return true;
  }

  function drawOverlay(options = {}) {
    const highQuality = options.highQuality !== false;
    if (!cadData || !camera || !resizeOverlayCanvas(highQuality)) return;
    const width = overlayCanvas.width;
    const height = overlayCanvas.height;
    overlayContext.clearRect(0, 0, width, height);
    overlayContext.lineCap = "round";
    overlayContext.lineJoin = "round";
    overlayContext.globalAlpha = 0.95;

    let projectedEntities = 0;
    let projectedSegments = 0;
    const projectedText = [];
    let textEntityIndex = 0;
    const interactiveTextStride = highQuality
      ? 1
      : Math.max(1, Math.ceil(cadTextEntityCount / MAX_INTERACTIVE_OVERLAY_TEXT_CANDIDATES));
    for (const layer of cadData.layers || []) {
      for (const entity of layer.entities || []) {
        if (entity.type === "text") {
          const currentTextIndex = textEntityIndex;
          textEntityIndex += 1;
          if (!highQuality
            && !window.CadsceneCadText.isStationLabel(entity)
            && currentTextIndex % interactiveTextStride !== 0) continue;
        }
        const points = getWorldPoints(entity);
        if (points.length === 0) continue;
        if (entity.type === "line" || entity.type === "polyline") {
          const segments = projectPolyline(points, camera, width, height);
          if (segments.length === 0) continue;
          overlayContext.beginPath();
          for (const [start, end] of segments) {
            overlayContext.moveTo(start[0], start[1]);
            overlayContext.lineTo(end[0], end[1]);
          }
          // 黑色描边让白色 CAD 在亮色视频上也能看见。
          overlayContext.strokeStyle = "rgba(0, 0, 0, 0.72)";
          overlayContext.lineWidth = 5;
          overlayContext.stroke();
          overlayContext.strokeStyle = colorFor(entity, layer);
          overlayContext.lineWidth = 2;
          overlayContext.stroke();
          projectedEntities += 1;
          projectedSegments += segments.length;
        } else if (entity.type === "text") {
          if (!showCadText) continue;
          const screenPoint = projectPoint(points[0], camera, width, height);
          if (!screenPoint) continue;
          projectedText.push({
            x: screenPoint[0],
            y: screenPoint[1],
            depth: 0,
            entity,
            layerData: layer,
          });
        }
      }
    }
    if (showCadText && window.CadsceneCadText) {
      const selectedText = window.CadsceneCadText.selectProjectedLabels(projectedText, {
        width,
        height,
        cellSize: highQuality ? 84 : 60,
        maxLabels: MAX_ACTIVE_CAD_TEXT_LABELS,
        margin: 0.04,
      });
      for (const candidate of selectedText) {
        const { entity, layerData } = candidate;
        const lines = String(entity.text || "").split(/\r?\n/);
        const angle = projectedTextAngle(entity, camera, width, height);
        const fontSize = fontSizeForText(entity, highQuality);
        const lineHeight = fontSize * 1.18;
        const totalHeight = lineHeight * lines.length;
        const vertical = entity.vertical_align || "baseline";
        let firstLineY = 0;
        if (vertical === "top") firstLineY = lineHeight * 0.5;
        else if (vertical === "middle") firstLineY = -totalHeight * 0.5 + lineHeight * 0.5;
        else if (vertical === "bottom") firstLineY = -totalHeight + lineHeight * 0.5;
        overlayContext.save();
        overlayContext.translate(candidate.x, candidate.y);
        overlayContext.rotate(angle);
        overlayContext.font = `bold ${fontSize}px Arial, Microsoft YaHei, sans-serif`;
        overlayContext.textAlign = ["left", "center", "right"].includes(entity.horizontal_align)
          ? entity.horizontal_align
          : "center";
        overlayContext.textBaseline = "middle";
        overlayContext.lineWidth = 4;
        overlayContext.strokeStyle = "rgba(0, 0, 0, 0.82)";
        overlayContext.fillStyle = colorFor(entity, layerData);
        lines.forEach((line, index) => {
          const y = firstLineY + index * lineHeight;
          overlayContext.strokeText(line, 0, y);
          overlayContext.fillText(line, 0, y);
        });
        overlayContext.restore();
        projectedEntities += 1;
        projectedSegments += 1;
      }
    }
    overlayContext.globalAlpha = 1;
    drawStatus.textContent = `${projectedEntities} 个实体，${projectedSegments} 段线段`;
  }

  function bboxCenter(data) {
    const bbox = selectCadFocusBounds(data);
    return { x: (bbox.min_x + bbox.max_x) * 0.5, y: (bbox.min_y + bbox.max_y) * 0.5 };
  }

  function selectCadFocusBounds(data) {
    const fullBounds = data.meta.bbox;
    const configured = data.meta.viewer_focus_bbox;
    if (configured && Number.isFinite(Number(configured.min_x)) && Number.isFinite(Number(configured.min_y))
      && Number.isFinite(Number(configured.max_x)) && Number.isFinite(Number(configured.max_y))) {
      return configured;
    }

    const cadScale = Math.max(Number(data.meta.cad_scale) || 1, 1e-9);
    for (const layerPattern of CAD_FOCUS_LAYER_GROUPS) {
      const focusBounds = { min_x: Infinity, min_y: Infinity, max_x: -Infinity, max_y: -Infinity };
      let matchedEntityCount = 0;
      for (const layer of data.layers || []) {
        for (const entity of layer.entities || []) {
          const layerName = `${entity.layer || ""} ${layer.name || layer.layer || ""}`;
          if (!layerPattern.test(layerName)) continue;
          const points = getWorldPoints(entity);
          if (points.length === 0) continue;
          matchedEntityCount += 1;
          for (const point of points) {
            focusBounds.min_x = Math.min(focusBounds.min_x, point[0]);
            focusBounds.min_y = Math.min(focusBounds.min_y, point[1]);
            focusBounds.max_x = Math.max(focusBounds.max_x, point[0]);
            focusBounds.max_y = Math.max(focusBounds.max_y, point[1]);
          }
        }
      }
      const width = focusBounds.max_x - focusBounds.min_x;
      const height = focusBounds.max_y - focusBounds.min_y;
      if (matchedEntityCount >= 2 && Math.max(width, height) >= 10 / cadScale) {
        const margin = Math.max(Math.max(width, height) * 0.05, 1 / cadScale);
        return {
          min_x: focusBounds.min_x - margin,
          min_y: focusBounds.min_y - margin,
          max_x: focusBounds.max_x + margin,
          max_y: focusBounds.max_y + margin,
        };
      }
    }
    return fullBounds;
  }

  function initialCameraFromCad(data) {
    const center = bboxCenter(data);
    const cadScale = Math.max(Number(data.meta.cad_scale) || 1, 1e-9);
    return { x: center.x, y: center.y, z: 120 / cadScale, yaw: 0, pitch: -45, roll: 0, fov: 70 };
  }

  async function loadInitialCamera(data) {
    const fallback = initialCameraFromCad(data);
    try {
      const response = await fetch(CAMERA_PATH);
      if (!response.ok) return fallback;
      const payload = await response.json();
      return Object.assign({}, fallback, payload.camera || payload);
    } catch {
      return fallback;
    }
  }

  function configureControlRanges(data) {
    const bbox = data.meta.bbox;
    const margin = Math.max(data.meta.width || 1, data.meta.height || 1) * 0.7;
    const cadScale = Math.max(Number(data.meta.cad_scale) || 1, 1e-9);
    for (const def of controlDefs) {
      if (def.key === "x") {
        def.min = bbox.min_x - margin;
        def.max = bbox.max_x + margin;
      }
      if (def.key === "y") {
        def.min = bbox.min_y - margin;
        def.max = bbox.max_y + margin;
      }
      if (def.key === "z") {
        def.min = 5 / cadScale;
        def.max = 1000 / cadScale;
        def.step = Math.max(1, 1 / cadScale);
      }
    }
  }

  function syncControls() {
    for (const def of controlDefs) {
      const pair = controlInputs.get(def.key);
      if (!pair) continue;
      const value = camera[def.key];
      pair.range.value = String(value);
      pair.number.value = Number.isInteger(value) ? String(value) : value.toFixed(3);
      const disabled = pureRotationRestrictedFields.has(def.key) || Boolean(pair.lock?.checked);
      pair.range.disabled = disabled;
      pair.number.disabled = disabled;
    }
  }

  function updateTrackStatus() {
    const frame = currentFrame();
    const time = video.currentTime || 0;
    timeStatus.textContent = `时间：${time.toFixed(3)} 秒`;
    frameStatus.textContent = `帧：${frame}`;
    keyframeStatus.textContent = `人工关键帧：${manualKeyframes().length}`;
    currentKeyframeStatus.textContent = manualKeyframes().some((keyframe) => keyframe.frame === frame)
      ? "当前：有人工关键帧"
      : "当前：无人工关键帧";
    renderQualityTimeline();
    if (threeScene && sfmScene) {
      threeScene.updateSfmGhost(frame);
      updateSfmCurrentFrameInfo(frame);
    }
  }

  function createControls() {
    controlContainer.replaceChildren();
    for (const def of controlDefs) {
      const item = document.createElement("div");
      item.className = "control-item";
      const label = document.createElement("label");
      label.innerHTML = `<span>${def.label}</span><span>${def.key}</span>`;
      const range = document.createElement("input");
      range.type = "range";
      range.min = String(def.min);
      range.max = String(def.max);
      range.step = String(def.step);
      const number = document.createElement("input");
      number.type = "number";
      number.min = String(def.min);
      number.max = String(def.max);
      number.step = String(def.step);
      const lockLabel = document.createElement("label");
      lockLabel.className = "control-lock";
      let lockInput = null;
      if (lockableCameraFields.has(def.key)) {
        lockInput = document.createElement("input");
        lockInput.type = "checkbox";
        lockInput.checked = !!lockedCameraFields[def.key];
        const lockIcon = document.createElement("span");
        lockIcon.className = "lock-icon";
        lockIcon.textContent = lockInput.checked ? "🔒" : "🔓";
        lockLabel.title = "锁定该自由度";
        lockLabel.append(lockInput, lockIcon);
        lockInput.addEventListener("change", () => {
          lockedCameraFields[def.key] = lockInput.checked;
          range.disabled = lockInput.checked || pureRotationRestrictedFields.has(def.key);
          number.disabled = lockInput.checked || pureRotationRestrictedFields.has(def.key);
          lockIcon.textContent = lockInput.checked ? "🔒" : "🔓";
          setStatus(`${def.label} ${lockInput.checked ? "已锁定" : "已解锁"}`);
        });
      } else {
        lockLabel.classList.add("control-lock-placeholder");
        lockLabel.setAttribute("aria-hidden", "true");
        lockLabel.textContent = " ";
      }
      const update = (value) => {
        if (lockedCameraFields[def.key] || pureRotationRestrictedFields.has(def.key)) return;
        clearPureRotationAuthoritativeMatrix();
        camera[def.key] = Number(value);
        syncControls();
        updateViews();
        notifyManualCameraChanged("parameter");
      };
      range.addEventListener("input", () => update(range.value));
      number.addEventListener("input", () => update(number.value));
      if (lockInput) {
        range.disabled = lockInput.checked || pureRotationRestrictedFields.has(def.key);
        number.disabled = lockInput.checked || pureRotationRestrictedFields.has(def.key);
      }
      item.append(label, lockLabel, range, number);
      controlContainer.appendChild(item);
      controlInputs.set(def.key, { range, number, lock: lockInput });
    }
    syncControls();
  }

  function worldToScene(point, origin) {
    // Three.js 使用 Y 作为竖直轴；这里把 CAD 世界坐标映射到场景坐标。
    return new THREE.Vector3(point[0] - origin.x, point[2] || 0, -(point[1] - origin.y));
  }

  function sceneToWorld(position, origin) {
    return { x: position.x + origin.x, y: origin.y - position.z, z: position.y };
  }

  function directionToScene(vector) {
    return new THREE.Vector3(vector[0], vector[2], -vector[1]).normalize();
  }

  function directionToWorld(vector) {
    return normalize([vector.x, -vector.z, vector.y]);
  }

  function setActiveGizmoButton(mode) {
    document.querySelector("#translateMode").classList.toggle("is-active", mode === "translate");
    document.querySelector("#rotateMode").classList.toggle("is-active", mode === "rotate");
  }

  function makeTextTexture(text, color, isMajor) {
    const lines = String(text || "").split(/\r?\n/);
    const fontSize = isMajor ? 48 : 36;
    const lineHeight = Math.ceil(fontSize * 1.2);
    const padding = isMajor ? 18 : 14;
    const canvas = document.createElement("canvas");
    const measure = canvas.getContext("2d");
    measure.font = `bold ${fontSize}px Arial, Microsoft YaHei, sans-serif`;
    const measuredWidth = Math.max(...lines.map((line) => measure.measureText(line).width), fontSize * 2);
    const contentWidth = Math.ceil(measuredWidth + padding * 2);
    const contentHeight = Math.ceil(lineHeight * lines.length + padding * 2);
    canvas.width = Math.min(1024, THREE.MathUtils.ceilPowerOfTwo(contentWidth));
    canvas.height = Math.min(1024, THREE.MathUtils.ceilPowerOfTwo(contentHeight));
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.font = `bold ${fontSize}px Arial, Microsoft YaHei, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineWidth = isMajor ? 8 : 6;
    ctx.strokeStyle = "rgba(0, 0, 0, 0.9)";
    ctx.fillStyle = color || "#ffffff";
    const firstLineY = canvas.height / 2 - (lines.length - 1) * lineHeight / 2;
    lines.forEach((line, index) => {
      const y = firstLineY + index * lineHeight;
      ctx.strokeText(line, canvas.width / 2, y);
      ctx.fillText(line, canvas.width / 2, y);
    });
    const texture = new THREE.CanvasTexture(canvas);
    texture.needsUpdate = true;
    return { texture, aspect: canvas.width / canvas.height };
  }

  function createThreeScene(data) {
    if (!window.THREE || !THREE.OrbitControls || !THREE.TransformControls || !window.CadsceneCadText) {
      throw new Error("THREE controls or CadsceneCadText is not loaded");
    }

    const bbox = data.meta.bbox;
    const focusBounds = selectCadFocusBounds(data);
    const origin = bboxCenter(data);
    const focusWidth = focusBounds.max_x - focusBounds.min_x;
    const focusHeight = focusBounds.max_y - focusBounds.min_y;
    const maxSize = Math.max(focusWidth, focusHeight, 1);
    const cadScale = Math.max(Number(data.meta.cad_scale) || 1, 1e-9);
    // 旧界面按 cad_scale=0.06 设计相机模型；换成毫米图纸后仍保持相同物理尺寸。
    const cameraMarkerScale = 0.06 / cadScale;
    // 二维 CAD 与地面近乎共面时，大场景深度精度会把线条遮掉，视觉上轻微抬高即可。
    const cadVisualLift = Math.max(maxSize * 1e-5, 0.01 / cadScale);
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(0x07090d);

    const renderer = new THREE.WebGLRenderer({ antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.domElement.className = "three-scene-canvas";
    sceneContainer.appendChild(renderer.domElement);

    // far plane 要覆盖整张 CAD，否则 19km 这种大图会被裁掉看不到。
    const fullRadius = Math.max(
      Math.hypot(bbox.min_x - origin.x, bbox.min_y - origin.y),
      Math.hypot(bbox.min_x - origin.x, bbox.max_y - origin.y),
      Math.hypot(bbox.max_x - origin.x, bbox.min_y - origin.y),
      Math.hypot(bbox.max_x - origin.x, bbox.max_y - origin.y),
    );
    const sceneFar = Math.max(fullRadius * 4, maxSize * 8, 8000);
    const inspectCamera = new THREE.PerspectiveCamera(45, 1, 0.5, sceneFar);
    const orbitControls = new THREE.OrbitControls(inspectCamera, renderer.domElement);
    orbitControls.enableDamping = true;
    orbitControls.enablePan = true;
    orbitControls.screenSpacePanning = true; // 右键/双指拖动像拖地图一样平移
    orbitControls.panSpeed = 0.9;
    orbitControls.zoomSpeed = 1.1;
    orbitControls.maxDistance = sceneFar * 0.9;

    const ground = new THREE.Mesh(
      new THREE.PlaneGeometry(maxSize * 1.35, maxSize * 1.35),
      new THREE.MeshBasicMaterial({ color: 0x111820, transparent: true, opacity: 0.78, side: THREE.DoubleSide }),
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.03;
    scene.add(ground);
    scene.add(new THREE.GridHelper(maxSize * 1.35, 24, 0x355063, 0x1e2a34));
    scene.add(new THREE.AxesHelper(maxSize * 0.18));
    scene.add(new THREE.HemisphereLight(0xffffff, 0x253040, 2.4));

    const cadGroup = new THREE.Group();
    const cadLineBuckets = new Map();
    const cadTextEntities = [];
    for (const layer of data.layers || []) {
      for (const entity of layer.entities || []) {
        const points = getWorldPoints(entity);
        if (entity.type === "text") {
          if (points.length >= 1 && String(entity.text || "").trim()) {
            cadTextEntities.push({
              key: `${entity.entity_id || entity.entity_type || "text"}:${cadTextEntities.length}`,
              entity,
              layerData: layer,
              worldPoint: points[0],
            });
          }
        } else {
          if (points.length < 2) continue;
          const color = colorFor(entity, layer);
          if (!cadLineBuckets.has(color)) cadLineBuckets.set(color, []);
          const vertices = cadLineBuckets.get(color);
          for (let index = 1; index < points.length; index += 1) {
            vertices.push(worldToScene(points[index - 1], origin), worldToScene(points[index], origin));
          }
        }
      }
    }
    const cadTextGroup = new THREE.Group();
    cadTextGroup.position.y = 0.45;
    cadGroup.add(cadTextGroup);
    const cadTextGeometry = new THREE.PlaneGeometry(1, 1);
    const activeCadText = new Map();
    const cadTextTextureCache = window.CadsceneCadText.createLruCache(
      MAX_CAD_TEXT_TEXTURES,
      (entry) => entry.texture.dispose(),
    );
    const cadTextSpatialIndex = window.CadsceneCadText.createSpatialLabelIndex(
      cadTextEntities,
      { maxCellsPerAxis: MAX_CAD_TEXT_GRID_AXIS },
    );
    const projectedCadTextCandidatePool = Array.from(
      { length: MAX_PROJECTED_CAD_TEXT_CANDIDATES },
      () => ({ x: 0, y: 0, depth: Infinity, entity: null, entry: null }),
    );
    const projectedCadTextPoint = new THREE.Vector3();
    const projectedCadTextCell = new THREE.Vector3();
    let cadTextVisible = showCadText;
    let cadTextRefreshTimer = null;
    let lastCadTextRefreshAt = -Infinity;

    function textureForCadText(entry) {
      const label = String(entry.entity.text || "");
      const color = colorFor(entry.entity, entry.layerData);
      const isMajor = window.CadsceneCadText.isStationLabel(entry.entity)
        || Number(entry.entity.cad_height || 0) >= 18;
      const textureKey = `${label}\u0000${color}\u0000${isMajor ? 1 : 0}`;
      let cached = cadTextTextureCache.get(textureKey);
      if (!cached) {
        cached = makeTextTexture(label, color, isMajor);
        cadTextTextureCache.set(textureKey, cached);
      }
      return { ...cached, textureKey };
    }

    function cadTextAnchorOffset(entity, width, height) {
      const horizontal = entity.horizontal_align || "center";
      const vertical = entity.vertical_align || "middle";
      const x = horizontal === "left" ? width / 2 : horizontal === "right" ? -width / 2 : 0;
      const y = vertical === "top" ? -height / 2
        : vertical === "bottom" ? height / 2
          : vertical === "baseline" ? height * 0.4 : 0;
      return { x, y };
    }

    function createCadTextMesh(entry) {
      const textureData = textureForCadText(entry);
      const material = new THREE.MeshBasicMaterial({
        map: textureData.texture,
        transparent: true,
        depthTest: false,
        depthWrite: false,
        side: THREE.DoubleSide,
      });
      const lineCount = Math.max(1, Number(entry.entity.text_lines) || String(entry.entity.text || "").split(/\r?\n/).length);
      const charHeight = Math.max(Number(entry.entity.cad_height) || 1, 1e-6);
      const height = charHeight * lineCount * 1.2 * CAD_TEXT_VISUAL_SCALE;
      const width = Math.max(height * textureData.aspect, charHeight * 1.5);
      const rotation = window.CadsceneCadText.cadRotationToSceneZ(
        entry.entity.cad_rotation,
      );
      const offset = cadTextAnchorOffset(entry.entity, width, height);
      const offsetX = Math.cos(rotation) * offset.x - Math.sin(rotation) * offset.y;
      const offsetY = Math.sin(rotation) * offset.x + Math.cos(rotation) * offset.y;
      const position = worldToScene([
        entry.worldPoint[0] + offsetX,
        entry.worldPoint[1] + offsetY,
        entry.worldPoint[2] || 0,
      ], origin);
      const mesh = new THREE.Mesh(cadTextGeometry, material);
      mesh.position.copy(position);
      mesh.scale.set(width, height, 1);
      mesh.rotation.x = -Math.PI / 2;
      mesh.rotation.z = rotation;
      mesh.renderOrder = 11;
      mesh.userData.isCadText = true;
      mesh.userData.textureKey = textureData.textureKey;
      return mesh;
    }

    function refreshCadTextLabels() {
      cadTextRefreshTimer = null;
      lastCadTextRefreshAt = performance.now();
      if (!cadTextVisible || cadTextEntities.length === 0) return;
      inspectCamera.updateMatrixWorld();
      const width = Math.max(1, renderer.domElement.clientWidth || sceneContainer.clientWidth);
      const height = Math.max(1, renderer.domElement.clientHeight || sceneContainer.clientHeight);
      const visibleEntries = cadTextSpatialIndex.collect((cell) => {
        projectedCadTextCell.set(
          cell.center[0] - origin.x,
          cadVisualLift + 0.45,
          -(cell.center[1] - origin.y),
        ).project(inspectCamera);
        const margin = 0.5;
        if (projectedCadTextCell.z < -1 || projectedCadTextCell.z > 1
          || projectedCadTextCell.x < -1 - margin || projectedCadTextCell.x > 1 + margin
          || projectedCadTextCell.y < -1 - margin || projectedCadTextCell.y > 1 + margin) {
          return null;
        }
        return -Math.hypot(projectedCadTextCell.x, projectedCadTextCell.y);
      }, MAX_PROJECTED_CAD_TEXT_CANDIDATES);
      for (let index = 0; index < visibleEntries.length; index += 1) {
        const entry = visibleEntries[index];
        const candidate = projectedCadTextCandidatePool[index];
        candidate.entry = entry;
        candidate.entity = entry.entity;
        projectedCadTextPoint.set(
          entry.worldPoint[0] - origin.x,
          (entry.worldPoint[2] || 0) + cadVisualLift + 0.45,
          -(entry.worldPoint[1] - origin.y),
        ).project(inspectCamera);
        candidate.x = (projectedCadTextPoint.x + 1) * width / 2;
        candidate.y = (1 - projectedCadTextPoint.y) * height / 2;
        candidate.depth = projectedCadTextPoint.z;
      }
      const projectedCadTextCandidates = projectedCadTextCandidatePool.slice(0, visibleEntries.length);
      const selected = window.CadsceneCadText.selectProjectedLabels(projectedCadTextCandidates, {
        width,
        height,
        cellSize: 84,
        maxLabels: MAX_ACTIVE_CAD_TEXT_LABELS,
        margin: 0.08,
      });
      const selectedKeys = new Set(selected.map((candidate) => candidate.entry.key));
      for (const [key, mesh] of activeCadText.entries()) {
        if (selectedKeys.has(key)) {
          cadTextTextureCache.get(mesh.userData.textureKey);
          continue;
        }
        cadTextGroup.remove(mesh);
        mesh.material.dispose();
        activeCadText.delete(key);
      }
      for (const candidate of selected) {
        const { entry } = candidate;
        if (activeCadText.has(entry.key)) continue;
        const mesh = createCadTextMesh(entry);
        activeCadText.set(entry.key, mesh);
        cadTextGroup.add(mesh);
      }
    }

    function scheduleCadTextRefresh(immediate = false) {
      if (!cadTextVisible || cadTextRefreshTimer !== null) return;
      const elapsed = performance.now() - lastCadTextRefreshAt;
      const delay = immediate ? 0 : Math.max(0, CAD_TEXT_REFRESH_MS - elapsed);
      cadTextRefreshTimer = window.setTimeout(refreshCadTextLabels, delay);
    }

    orbitControls.addEventListener("change", () => scheduleCadTextRefresh(false));
    for (const [color, vertices] of cadLineBuckets.entries()) {
      if (vertices.length === 0) continue;
      const geometry = new THREE.BufferGeometry().setFromPoints(vertices);
      const material = new THREE.LineBasicMaterial({
        color: new THREE.Color(color),
        transparent: true,
        opacity: 0.95,
        depthTest: false,
        depthWrite: false,
      });
      const cadLine = new THREE.LineSegments(geometry, material);
      cadLine.renderOrder = 10;
      cadGroup.add(cadLine);
    }
    cadGroup.position.y = cadVisualLift;
    scene.add(cadGroup);

    function setCadTextVisible(visible) {
      cadTextVisible = !!visible;
      cadTextGroup.visible = cadTextVisible;
      if (cadTextVisible) scheduleCadTextRefresh(true);
    }

    // 把第三人称观察相机聚焦到“虚拟无人机所在路段”，而不是 19km 整图的几何中心，
    // 否则初始视角会落在路的另一端、相机又超出可视范围。
    function focusInspectOnCamera(params) {
      const rigScene = worldToScene([params.x, params.y, params.z || 0], origin);
      const groundScene = worldToScene([params.x, params.y, 0], origin);
      const axes = getCameraAxes(params);
      const forwardScene = directionToScene(axes.forward);
      const distance = Math.max((params.z || 120) * 2.5, 400);
      inspectCamera.position.set(
        rigScene.x - forwardScene.x * distance,
        (params.z || 120) + distance * 0.7,
        rigScene.z - forwardScene.z * distance,
      );
      orbitControls.target.copy(groundScene);
      orbitControls.update();
    }

    function focusInspectOnCameraAndCad(params) {
      const rigScene = worldToScene([params.x, params.y, params.z || 0], origin);
      const bounds = new THREE.Box3();
      bounds.expandByPoint(rigScene);
      for (const point of [
        [focusBounds.min_x, focusBounds.min_y, 0],
        [focusBounds.min_x, focusBounds.max_y, 0],
        [focusBounds.max_x, focusBounds.min_y, 0],
        [focusBounds.max_x, focusBounds.max_y, 0],
      ]) {
        bounds.expandByPoint(worldToScene(point, origin));
      }
      const center = bounds.getCenter(new THREE.Vector3());
      const sphere = bounds.getBoundingSphere(new THREE.Sphere());
      const distance = Math.max(
        (Math.max(sphere.radius, 1) / Math.tan(degToRad(inspectCamera.fov * 0.5))) * 1.35,
        240,
      );
      inspectCamera.position.copy(center).add(new THREE.Vector3(distance * 0.45, distance * 0.72, distance * 0.72));
      orbitControls.target.copy(center);
      orbitControls.update();
    }

    function ensureCameraNearCad(params) {
      // Manual placements may legitimately sit outside the CAD footprint. Only
      // recover obviously mismatched coordinate systems (for example [0, 0]
      // against a survey drawing with million-scale world coordinates).
      const padding = Math.max(maxSize * 5, 1);
      const inside =
        Number(params.x) >= focusBounds.min_x - padding
        && Number(params.x) <= focusBounds.max_x + padding
        && Number(params.y) >= focusBounds.min_y - padding
        && Number(params.y) <= focusBounds.max_y + padding;
      if (inside) return { moved: false, pose: { ...params } };
      return {
        moved: true,
        pose: {
          ...params,
          x: (focusBounds.min_x + focusBounds.max_x) * 0.5,
          y: (focusBounds.min_y + focusBounds.max_y) * 0.5,
          z: Number.isFinite(Number(params.z)) && Number(params.z) > 0 ? Number(params.z) : 120,
        },
      };
    }

    function focusInspectOnCad() {
      const radius = Math.max(Math.hypot(focusWidth || 1, focusHeight || 1) * 0.5, 1);
      const distance = Math.max((radius / Math.tan(degToRad(inspectCamera.fov * 0.5))) * 1.2, 400);
      inspectCamera.position.set(distance * 0.35, distance * 0.72, distance * 0.72);
      orbitControls.target.set(0, 0, 0);
      orbitControls.update();
    }
    focusInspectOnCad();

    const uavCameraRig = new THREE.Group();
    scene.add(uavCameraRig);
    const cameraVisualGroup = new THREE.Group();
    uavCameraRig.add(cameraVisualGroup);

    const body = new THREE.Mesh(
      new THREE.BoxGeometry(18 * cameraMarkerScale, 10 * cameraMarkerScale, 14 * cameraMarkerScale),
      new THREE.MeshBasicMaterial({ color: 0xffd36e }),
    );
    cameraVisualGroup.add(body);
    cameraVisualGroup.add(new THREE.ArrowHelper(
      new THREE.Vector3(0, 0, -1),
      new THREE.Vector3(0, 0, 0),
      50 * cameraMarkerScale,
      0x56c2ff,
      14 * cameraMarkerScale,
      8 * cameraMarkerScale,
    ));

    const frustumMaterial = new THREE.LineBasicMaterial({ color: 0x56c2ff, transparent: true, opacity: 0.85 });
    const frustumLine = new THREE.LineSegments(new THREE.BufferGeometry(), frustumMaterial);
    cameraVisualGroup.add(frustumLine);

    const transformControls = new THREE.TransformControls(inspectCamera, renderer.domElement);
    transformControls.attach(uavCameraRig);
    transformControls.setMode("translate");
    transformControls.setSpace("world");
    scene.add(transformControls);
    setActiveGizmoButton("translate");

    let updatingRig = false;

    function localFrustumPoints(fov) {
      // 视锥在 rig 本地坐标里绘制；rig 的 -Z 是虚拟相机前方。
      const distance = 180 * cameraMarkerScale;
      const aspect = 16 / 9;
      const halfWidth = Math.tan(degToRad(fov || 70) * 0.5) * distance;
      const halfHeight = halfWidth / aspect;
      const originPoint = new THREE.Vector3(0, 0, 0);
      const centerPoint = new THREE.Vector3(0, 0, -distance);
      const topLeft = centerPoint.clone().add(new THREE.Vector3(-halfWidth, halfHeight, 0));
      const topRight = centerPoint.clone().add(new THREE.Vector3(halfWidth, halfHeight, 0));
      const bottomLeft = centerPoint.clone().add(new THREE.Vector3(-halfWidth, -halfHeight, 0));
      const bottomRight = centerPoint.clone().add(new THREE.Vector3(halfWidth, -halfHeight, 0));
      return [
        originPoint, topLeft,
        originPoint, topRight,
        originPoint, bottomLeft,
        originPoint, bottomRight,
        topLeft, topRight,
        topRight, bottomRight,
        bottomRight, bottomLeft,
        bottomLeft, topLeft,
      ];
    }

    function updateFrustum(fov) {
      frustumLine.geometry.dispose();
      frustumLine.geometry = new THREE.BufferGeometry().setFromPoints(localFrustumPoints(fov));
    }

    function updateCameraVisualScale() {
      // 相机位姿使用真实 CAD 单位；仅让显示模型在总览和局部视图中都保持可辨认。
      const distance = Math.max(inspectCamera.position.distanceTo(uavCameraRig.position), 1);
      const visibleHeight = 2 * distance * Math.tan(degToRad(inspectCamera.fov * 0.5));
      const desiredBodyWidth = visibleHeight * 0.025;
      const baseBodyWidth = Math.max(18 * cameraMarkerScale, 1e-6);
      const visualScale = clamp(desiredBodyWidth / baseBodyWidth, 1, 20);
      cameraVisualGroup.scale.setScalar(visualScale);
    }

    function setRigFromCamera(params) {
      updatingRig = true;
      uavCameraRig.position.copy(worldToScene([params.x, params.y, params.z], origin));
      const axes = getCameraAxes(params);
      const right = directionToScene(axes.right);
      const up = directionToScene(scale(axes.down, -1));
      const backward = directionToScene(scale(axes.forward, -1));
      const matrix = new THREE.Matrix4().makeBasis(right, up, backward);
      uavCameraRig.quaternion.setFromRotationMatrix(matrix);
      updateFrustum(params.fov);
      updatingRig = false;
    }

    function cameraFromRig() {
      const worldPosition = sceneToWorld(uavCameraRig.position, origin);
      const quaternion = uavCameraRig.quaternion;
      const rightWorld = directionToWorld(new THREE.Vector3(1, 0, 0).applyQuaternion(quaternion));
      const forwardWorld = directionToWorld(new THREE.Vector3(0, 0, -1).applyQuaternion(quaternion));
      const pitch = radToDeg(Math.asin(clamp(forwardWorld[2], -1, 1)));
      const yaw = radToDeg(Math.atan2(forwardWorld[0], forwardWorld[1]));
      const baseAxes = getCameraAxes({ yaw, pitch, roll: 0 });
      const roll = radToDeg(
        Math.atan2(
          dot(cross(baseAxes.right, rightWorld), forwardWorld),
          dot(baseAxes.right, rightWorld),
        ),
      );
      return {
        x: worldPosition.x,
        y: worldPosition.y,
        z: worldPosition.z,
        yaw,
        pitch,
        roll,
        fov: camera.fov,
      };
    }

    function rotationMatrixFromRig() {
      const quaternion = uavCameraRig.quaternion;
      const right = directionToWorld(new THREE.Vector3(1, 0, 0).applyQuaternion(quaternion));
      const down = directionToWorld(new THREE.Vector3(0, -1, 0).applyQuaternion(quaternion));
      const forward = directionToWorld(new THREE.Vector3(0, 0, -1).applyQuaternion(quaternion));
      return [
        [right[0], down[0], forward[0]],
        [right[1], down[1], forward[1]],
        [right[2], down[2], forward[2]],
      ];
    }

    let gizmoOverlayTimer = null;
    let lastGizmoOverlayAt = -Infinity;

    function scheduleGizmoOverlay() {
      if (gizmoOverlayTimer !== null) return;
      const elapsed = performance.now() - lastGizmoOverlayAt;
      const delay = Math.max(0, CAD_GIZMO_OVERLAY_REFRESH_MS - elapsed);
      gizmoOverlayTimer = window.setTimeout(() => {
        gizmoOverlayTimer = null;
        lastGizmoOverlayAt = performance.now();
        drawOverlay({ highQuality: false });
      }, delay);
    }

    transformControls.addEventListener("dragging-changed", (event) => {
      orbitControls.enabled = !event.value;
      if (event.value) video.pause();
      else {
        if (gizmoOverlayTimer !== null) {
          window.clearTimeout(gizmoOverlayTimer);
          gizmoOverlayTimer = null;
        }
        lastGizmoOverlayAt = performance.now();
        drawOverlay({ highQuality: true });
      }
    });
    transformControls.addEventListener("objectChange", () => {
      if (updatingRig) return;
      camera = applyCameraLocks(cameraFromRig(), camera);
      if (pureRotationPlaybackActive && pureRotationRestrictedFields.has("yaw")) {
        pureRotationAuthoritativeMatrix = rotationMatrixFromRig();
      } else {
        clearPureRotationAuthoritativeMatrix();
      }
      setRigFromCamera(camera);
      syncControls();
      scheduleGizmoOverlay();
      updateTrackStatus();
      notifyManualCameraChanged("gizmo");
    });

    function setMode(mode) {
      transformControls.setMode(mode);
      setActiveGizmoButton(mode);
    }

    function setTransformSpace(space) {
      transformControls.setSpace(space === "local" ? "local" : "world");
    }

    function setGizmoVisible(visible) {
      transformControls.visible = visible;
      transformControls.enabled = visible;
      document.querySelector("#toggleGizmo").textContent = visible ? "隐藏 Gizmo" : "显示 Gizmo";
    }

    // ------------------------------------------------------------------
    // SfM 诊断场景：点云 + 原始SfM轨迹 + 锚定轨迹 + 建议标记 + ghost 相机
    // 全部落在与 CAD/相机同一 cad_world 坐标（worldToScene 统一映射），
    // 仅用于诊断/展示，不改动任何相机轨迹。
    // ------------------------------------------------------------------
    const sfmGroup = new THREE.Group();
    scene.add(sfmGroup);
    const suggestionGroup = new THREE.Group();
    sfmGroup.add(suggestionGroup);
    const markerSize = Math.max(maxSize * 0.004, 14);

    let sfmPoints = null;
    let sfmPointRgb = null;       // Float32Array 原始 rgb（0..1）
    let sfmPointHeights = null;   // Float32Array 每点世界 z（高度着色用）
    let sfmHasRgb = false;
    let globalTrackLine = null;
    let anchoredTrackLine = null;
    let ghostCamera = null;
    let suggestionMarkers = [];   // {mesh, frame, data}
    let sfmGlobalTrack = [];
    let sfmAnchoredTrack = [];
    let sfmPointSize = 3;
    let sfmColorMode = "rgb";
    const sfmRaycaster = new THREE.Raycaster();
    const sfmPointer = new THREE.Vector2();

    function disposeObject(obj) {
      if (!obj) return;
      if (obj.geometry) obj.geometry.dispose();
      if (obj.material) {
        if (Array.isArray(obj.material)) obj.material.forEach((m) => m.dispose());
        else obj.material.dispose();
      }
    }

    function clearSfmScene() {
      if (sfmPoints) { sfmGroup.remove(sfmPoints); disposeObject(sfmPoints); sfmPoints = null; }
      if (globalTrackLine) { sfmGroup.remove(globalTrackLine); disposeObject(globalTrackLine); globalTrackLine = null; }
      if (anchoredTrackLine) { sfmGroup.remove(anchoredTrackLine); disposeObject(anchoredTrackLine); anchoredTrackLine = null; }
      if (ghostCamera) { sfmGroup.remove(ghostCamera); disposeObject(ghostCamera); ghostCamera = null; }
      for (const m of suggestionMarkers) { suggestionGroup.remove(m.mesh); disposeObject(m.mesh); }
      suggestionMarkers = [];
      sfmPointRgb = null;
      sfmPointHeights = null;
    }

    function buildTrackLine(track, colorHex, opacity) {
      if (!track || track.length < 2) return null;
      const pts = track.map((t) => worldToScene([t.x, t.y, t.z], origin));
      const geometry = new THREE.BufferGeometry().setFromPoints(pts);
      const material = new THREE.LineBasicMaterial({
        color: new THREE.Color(colorHex), transparent: true, opacity,
      });
      return new THREE.Line(geometry, material);
    }

    function trackPoseAtFrame(track, frame) {
      // 最近帧查找（track 已按 frame 升序），返回 {x,y,z,...} 或 null。
      if (!track || track.length === 0) return null;
      let lo = 0;
      let hi = track.length - 1;
      if (frame <= track[0].frame_index) return track[0];
      if (frame >= track[hi].frame_index) return track[hi];
      while (lo < hi) {
        const mid = (lo + hi) >> 1;
        if (track[mid].frame_index < frame) lo = mid + 1;
        else hi = mid;
      }
      const a = track[Math.max(0, lo - 1)];
      const b = track[lo];
      return (frame - a.frame_index) <= (b.frame_index - frame) ? a : b;
    }

    const SUGGEST_COLOR = { high: 0xe03131, medium: 0xf4c20d, low: 0x39b54a };

    function buildSuggestionMarkers(suggestions, track) {
      const list = suggestions || [];
      const ref = (track && track.length) ? track : sfmGlobalTrack;
      for (const s of list) {
        const pose = trackPoseAtFrame(ref, s.frame_index);
        if (!pose) continue;
        const color = SUGGEST_COLOR[(s.priority || s.risk_level || "medium")] || SUGGEST_COLOR.medium;
        const mesh = new THREE.Mesh(
          new THREE.ConeGeometry(markerSize * 0.5, markerSize * 1.4, 12),
          new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0.9 }),
        );
        const sp = worldToScene([pose.x, pose.y, pose.z], origin);
        mesh.position.set(sp.x, sp.y + markerSize * 1.6, sp.z);
        mesh.rotation.x = Math.PI; // 尖头朝下指向轨迹
        mesh.userData.sfmSuggestion = true;
        suggestionGroup.add(mesh);
        suggestionMarkers.push({ mesh, frame: s.frame_index, data: s });
      }
    }

    function refreshSuggestionMarkers(suggestions) {
      // 单独加载新的 keyframe_suggestions.json 时，刷新右侧 3D 建议标记；
      // 点云/轨迹仍来自当前 SfM 场景，不重新加载大 JSON。
      for (const m of suggestionMarkers) {
        suggestionGroup.remove(m.mesh);
        disposeObject(m.mesh);
      }
      suggestionMarkers = [];
      buildSuggestionMarkers(suggestions || [],
        sfmAnchoredTrack.length ? sfmAnchoredTrack : sfmGlobalTrack);
    }

    function computePointColors() {
      // 依据 sfmColorMode 生成颜色属性数组（0..1）。
      if (!sfmPoints) return null;
      const n = sfmPointHeights ? sfmPointHeights.length : 0;
      const colors = new Float32Array(n * 3);
      if (sfmColorMode === "rgb" && sfmHasRgb && sfmPointRgb) {
        colors.set(sfmPointRgb);
      } else if (sfmColorMode === "height" && sfmPointHeights) {
        let mn = Infinity;
        let mx = -Infinity;
        for (let i = 0; i < n; i += 1) { mn = Math.min(mn, sfmPointHeights[i]); mx = Math.max(mx, sfmPointHeights[i]); }
        const span = Math.max(mx - mn, 1e-6);
        for (let i = 0; i < n; i += 1) {
          const t = (sfmPointHeights[i] - mn) / span;
          colors[i * 3] = t; colors[i * 3 + 1] = 0.25; colors[i * 3 + 2] = 1 - t;
        }
      } else {
        for (let i = 0; i < n; i += 1) { colors[i * 3] = 0.62; colors[i * 3 + 1] = 0.68; colors[i * 3 + 2] = 0.75; }
      }
      return colors;
    }

    function refreshPointColors() {
      if (!sfmPoints) return;
      const colors = computePointColors();
      if (!colors) return;
      sfmPoints.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
      sfmPoints.geometry.attributes.color.needsUpdate = true;
    }

    function loadSfmScene(sceneData) {
      clearSfmScene();
      if (!sceneData) return;
      const pd = sceneData.points || {};
      const rows = pd.data || [];
      sfmHasRgb = !!pd.has_rgb;
      if (rows.length) {
        const positions = new Float32Array(rows.length * 3);
        const heights = new Float32Array(rows.length);
        const rgb = new Float32Array(rows.length * 3);
        for (let i = 0; i < rows.length; i += 1) {
          const r = rows[i];
          const sp = worldToScene([r[0], r[1], r[2]], origin);
          positions[i * 3] = sp.x; positions[i * 3 + 1] = sp.y; positions[i * 3 + 2] = sp.z;
          heights[i] = r[2];
          if (sfmHasRgb && r.length >= 6) {
            // cadscene 后端导出的 RGB 是 0..255 整数；Three.js 颜色属性使用 0..1。
            rgb[i * 3] = Number(r[3]) > 1 ? Number(r[3]) / 255 : Number(r[3]);
            rgb[i * 3 + 1] = Number(r[4]) > 1 ? Number(r[4]) / 255 : Number(r[4]);
            rgb[i * 3 + 2] = Number(r[5]) > 1 ? Number(r[5]) / 255 : Number(r[5]);
          }
        }
        sfmPointRgb = sfmHasRgb ? rgb : null;
        sfmPointHeights = heights;
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        // sizeAttenuation:false -> 用屏幕像素尺寸，避免大场景里点被缩到不可见
        const material = new THREE.PointsMaterial({
          size: sfmPointSize, vertexColors: true, sizeAttenuation: false,
        });
        sfmPoints = new THREE.Points(geometry, material);
        sfmGroup.add(sfmPoints);
        refreshPointColors();
      }
      sfmGlobalTrack = (sceneData.tracks && sceneData.tracks.global_sfm_track) || [];
      sfmAnchoredTrack = (sceneData.tracks && sceneData.tracks.anchored_camera_path) || [];
      globalTrackLine = buildTrackLine(sfmGlobalTrack, 0x9aa6b6, 0.55);
      anchoredTrackLine = buildTrackLine(sfmAnchoredTrack, 0xffa500, 0.95);
      if (globalTrackLine) sfmGroup.add(globalTrackLine);
      if (anchoredTrackLine) sfmGroup.add(anchoredTrackLine);
      buildSuggestionMarkers(sceneData.suggestions || [],
        sfmAnchoredTrack.length ? sfmAnchoredTrack : sfmGlobalTrack);
      ghostCamera = new THREE.Mesh(
        new THREE.SphereGeometry(markerSize * 0.4, 12, 12),
        new THREE.MeshBasicMaterial({ color: 0x9aa6b6, transparent: true, opacity: 0.85 }),
      );
      ghostCamera.visible = false;
      sfmGroup.add(ghostCamera);
    }

    function updateSfmGhost(frame) {
      if (!ghostCamera || sfmGlobalTrack.length === 0) return;
      const pose = trackPoseAtFrame(sfmGlobalTrack, frame);
      if (!pose) { ghostCamera.visible = false; return; }
      const sp = worldToScene([pose.x, pose.y, pose.z], origin);
      ghostCamera.position.copy(sp);
      ghostCamera.visible = globalTrackLine ? globalTrackLine.visible : true;
    }

    function setSfmPointsVisible(v) { if (sfmPoints) sfmPoints.visible = v; }
    function setGlobalTrackVisible(v) {
      if (globalTrackLine) globalTrackLine.visible = v;
      if (ghostCamera) ghostCamera.visible = v && ghostCamera.visible;
    }
    function setAnchoredTrackVisible(v) { if (anchoredTrackLine) anchoredTrackLine.visible = v; }
    function setAnchoredTrackData(track) {
      sfmAnchoredTrack = track || [];
      if (anchoredTrackLine) {
        sfmGroup.remove(anchoredTrackLine);
        disposeObject(anchoredTrackLine);
        anchoredTrackLine = null;
      }
      anchoredTrackLine = buildTrackLine(sfmAnchoredTrack, 0xffa500, 0.95);
      if (anchoredTrackLine) sfmGroup.add(anchoredTrackLine);
      refreshSuggestionMarkers(
        (sfmScene && sfmScene.suggestions) ? sfmScene.suggestions : suggestionsForSfmScene(),
      );
    }
    function setSuggestionsVisible(v) { suggestionGroup.visible = v; }
    function setSfmSuggestions(suggestions) { refreshSuggestionMarkers(suggestions); }
    function setFrustumVisible(v) { frustumLine.visible = v; }
    function setSfmPointSize(v) {
      sfmPointSize = Number(v);
      if (sfmPoints) { sfmPoints.material.size = sfmPointSize; sfmPoints.material.needsUpdate = true; }
    }
    function setSfmColorMode(mode) { sfmColorMode = mode; refreshPointColors(); }

    function pickSuggestion(event) {
      if (suggestionMarkers.length === 0 || !suggestionGroup.visible) return null;
      const rect = renderer.domElement.getBoundingClientRect();
      sfmPointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      sfmPointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      sfmRaycaster.setFromCamera(sfmPointer, inspectCamera);
      const hits = sfmRaycaster.intersectObjects(suggestionMarkers.map((m) => m.mesh), false);
      if (hits.length === 0) return null;
      return suggestionMarkers.find((m) => m.mesh === hits[0].object) || null;
    }

    const cadPickRaycaster = new THREE.Raycaster();
    const cadPickPointer = new THREE.Vector2();
    cadPickRaycaster.params.Line.threshold = Math.max(0.5, maxSize * 0.0005);

    function pickCadWorld(event) {
      const rect = renderer.domElement.getBoundingClientRect();
      cadPickPointer.x = ((Number(event.clientX) - rect.left) / rect.width) * 2 - 1;
      cadPickPointer.y = -((Number(event.clientY) - rect.top) / rect.height) * 2 + 1;
      cadPickRaycaster.setFromCamera(cadPickPointer, inspectCamera);
      const cadHits = cadPickRaycaster.intersectObjects(cadGroup.children, true)
        .filter((hit) => !hit.object.userData?.isCadText);
      const hit = cadHits[0] || cadPickRaycaster.intersectObject(ground, false)[0];
      if (!hit) return null;
      const scenePoint = hit.point.clone();
      if (cadHits.length > 0) scenePoint.y -= cadVisualLift;
      const world = sceneToWorld(scenePoint, origin);
      return {
        cad_world_xyz: [world.x, world.y, world.z],
        cad_entity_reference: hit.object.userData?.entityReference || null,
      };
    }

    function projectCadWorldToInspect(point) {
      if (!Array.isArray(point) || point.length !== 3) {
        return { visible: false, reason: "projection_unavailable" };
      }
      inspectCamera.updateMatrixWorld(true);
      const scenePoint = worldToScene(point.map(Number), origin);
      const cameraPoint = scenePoint.clone().applyMatrix4(inspectCamera.matrixWorldInverse);
      if (cameraPoint.z >= -inspectCamera.near) {
        return { visible: false, reason: "behind_inspect_camera" };
      }
      const projected = scenePoint.clone().project(inspectCamera);
      if (!Number.isFinite(projected.x) || !Number.isFinite(projected.y)) {
        return { visible: false, reason: "projection_invalid" };
      }
      const width = Math.max(1, renderer.domElement.clientWidth || sceneContainer.clientWidth);
      const height = Math.max(1, renderer.domElement.clientHeight || sceneContainer.clientHeight);
      const x = (projected.x + 1) * width * 0.5;
      const y = (1 - projected.y) * height * 0.5;
      if (x < 0 || x >= width || y < 0 || y >= height) {
        return { visible: false, reason: "outside_inspect_viewport" };
      }
      return { visible: true, reason: "visible", xy: [x, y] };
    }

    renderer.domElement.addEventListener("click", (event) => {
      const hit = pickSuggestion(event);
      if (!hit) return;
      const s = hit.data;
      setStatus(`跳转到建议帧 ${hit.frame}（${s.priority || s.risk_level || ""}）`);
      goToFrame(hit.frame);
    });

    renderer.domElement.addEventListener("mousemove", (event) => {
      const tip = document.querySelector("#sfmSceneTip");
      if (!tip) return;
      const hit = pickSuggestion(event);
      if (!hit) { tip.hidden = true; return; }
      const s = hit.data;
      const reasons = Array.isArray(s.reason_codes) ? s.reason_codes.join(", ") : "";
      tip.innerHTML = `建议帧 ${hit.frame}（${s.priority || s.risk_level || ""}，score ${Number(s.risk_score || 0).toFixed(2)}）`
        + (reasons ? `<br>原因: ${reasons}` : "")
        + (s.suggest_action ? `<br>${s.suggest_action}` : "");
      const rect = sceneContainer.getBoundingClientRect();
      tip.style.left = `${event.clientX - rect.left}px`;
      tip.style.top = `${event.clientY - rect.top}px`;
      tip.hidden = false;
    });
    renderer.domElement.addEventListener("mouseleave", () => {
      const tip = document.querySelector("#sfmSceneTip");
      if (tip) tip.hidden = true;
    });

    function resize() {
      const width = Math.max(1, sceneContainer.clientWidth);
      const height = Math.max(1, sceneContainer.clientHeight);
      renderer.setSize(width, height, false);
      inspectCamera.aspect = width / height;
      inspectCamera.updateProjectionMatrix();
      scheduleCadTextRefresh(true);
    }

    function animate() {
      requestAnimationFrame(animate);
      orbitControls.update();
      updateCameraVisualScale();
      renderer.render(scene, inspectCamera);
    }

    window.addEventListener("resize", resize);
    const resizeObserver = typeof ResizeObserver === "function"
      ? new ResizeObserver(() => resize())
      : null;
    if (resizeObserver) resizeObserver.observe(sceneContainer);
    resize();
    animate();
    return {
      updateVirtualCamera: setRigFromCamera, setMode, setTransformSpace, setGizmoVisible, setCadTextVisible,
      focusInspectOnCamera, focusInspectOnCameraAndCad, focusInspectOnCad, ensureCameraNearCad, transformControls,
      loadSfmScene, updateSfmGhost, setSfmPointsVisible, setGlobalTrackVisible,
      setAnchoredTrackVisible, setSuggestionsVisible, setFrustumVisible,
      setSfmPointSize, setSfmColorMode, setSfmSuggestions, setAnchoredTrackData,
      pickCadWorld, projectCadWorldToInspect,
    };
  }

  function updateViews(options = {}) {
    const forceOverlay = options.forceOverlay === true;
    const followCamera = options.followCamera === true;
    const now = performance.now();
    const shouldDrawOverlay = forceOverlay
      || video.paused
      || now - lastOverlayDrawAt >= OVERLAY_INTERVAL_PLAYING_MS;
    if (shouldDrawOverlay) {
      drawOverlay({ highQuality: forceOverlay || video.paused });
      lastOverlayDrawAt = now;
    }
    if (threeScene && options.updateThree !== false) {
      threeScene.updateVirtualCamera(camera);
      if (followCamera) threeScene.focusInspectOnCamera(camera);
    }
    updateTrackStatus();
  }

  let followOnPlay = false;

  function applyTrackPoseForCurrentFrame() {
    if (pureRotationPlaybackActive && typeof window.cadsceneRefreshPureRotationPose === "function") {
      window.cadsceneRefreshPureRotationPose();
      return;
    }
    if (!video.paused && (sfmFollowMode || cameraTrack.keyframes.length > 0)) {
      camera = poseForFrame(currentFrame());
    }
    updateViews({ forceOverlay: false, updateThree: true, followCamera: followOnPlay });
  }

  function addOrUpdateKeyframe() {
    const frame = currentFrame();
    const source = reviewPacket && Number(reviewPacket.review_frame) === frame ? "manual_corrected" : "manual_anchor";
    upsertKeyframe(makeKeyframe(frame, camera, source));
    syncSfmAnchoredTrackFromCurrentTrack();
    updateTrackStatus();
  }

  function deleteCurrentKeyframe() {
    const frame = currentFrame();
    cameraTrack.keyframes = cameraTrack.keyframes.filter((keyframe) => keyframe.frame !== frame);
    syncSfmAnchoredTrackFromCurrentTrack();
    updateTrackStatus();
  }

  function goToKeyframe(direction) {
    const frame = currentFrame();
    const sorted = manualKeyframes().sort((a, b) => a.frame - b.frame);
    if (sorted.length === 0) return;
    let target = null;
    if (direction < 0) {
      for (let index = sorted.length - 1; index >= 0; index -= 1) {
        if (sorted[index].frame < frame) {
          target = sorted[index];
          break;
        }
      }
    } else {
      target = sorted.find((keyframe) => keyframe.frame > frame);
    }
    if (!target) target = direction < 0 ? sorted[0] : sorted[sorted.length - 1];
    goToFrame(target.frame);
  }

  function videoBaseUrl() {
    return VIDEO_SRC.split("#")[0].split("?")[0];
  }

  function waitForVideoSeek(timeoutMs = 1500) {
    return new Promise((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        video.removeEventListener("seeked", finish);
        resolve();
      };
      video.addEventListener("seeked", finish, { once: true });
      window.setTimeout(finish, timeoutMs);
    });
  }

  function applyFramePreview(targetFrame) {
    manualFrameOverride = targetFrame;
    if (sfmFollowMode || cameraTrack.keyframes.length > 0) {
      camera = poseForFrame(targetFrame);
      syncControls();
    }
    updateViews({ forceOverlay: true });
    if (threeScene) threeScene.focusInspectOnCamera(camera);
  }

  async function seekVideoToFrame(targetFrame) {
    const targetTime = frameToTime(targetFrame);
    const duration = Number.isFinite(video.duration) ? video.duration : targetTime;
    const clampedTime = clamp(targetTime, 0, Math.max(duration, 0));
    video.pause();
    applyFramePreview(targetFrame);

    const assignCurrentTime = () => {
      if (typeof video.fastSeek === "function") {
        video.fastSeek(clampedTime);
      } else {
        video.currentTime = clampedTime;
      }
    };

    try {
      assignCurrentTime();
    } catch (error) {
      setStatus(`视频跳转失败：${error.message}`);
    }
    await waitForVideoSeek();

    let actualFrame = Math.round((video.currentTime || 0) * cameraTrack.fps);
    if (Math.abs(actualFrame - targetFrame) > 2) {
      const base = videoBaseUrl();
      video.src = `${base}#t=${clampedTime.toFixed(3)}`;
      video.load();
      await new Promise((resolve) => {
        video.addEventListener("loadeddata", resolve, { once: true });
        video.addEventListener("error", resolve, { once: true });
      });
      actualFrame = Math.round((video.currentTime || 0) * cameraTrack.fps);
      if (Math.abs(actualFrame - targetFrame) > 2) {
        video.src = base;
        assignCurrentTime();
        await waitForVideoSeek();
        actualFrame = Math.round((video.currentTime || 0) * cameraTrack.fps);
      }
    }

    if (Math.abs(actualFrame - targetFrame) <= 2) {
      // Preserve the requested source-frame index for keyframe saving. Near the
      // end of a video, browser decoding may display an adjacent frame.
      syncControls();
      setStatus(`已跳转到帧 ${targetFrame}`);
    } else if (videoRangeSupported === false) {
      setStatus(`相机已对齐帧 ${targetFrame}；视频仍在帧 ${actualFrame}，当前服务器不支持 Range，请用 serve_web_viewer`);
    } else {
      setStatus(`相机已对齐帧 ${targetFrame}；视频仍在帧 ${actualFrame}，878MB 高分辨率视频 seek 可能较慢，请稍候再试`);
    }
    updateViews({ forceOverlay: true });
    if (threeScene) threeScene.focusInspectOnCamera(camera);
  }

  function goToFrame(frame) {
    const targetFrame = Math.max(0, Math.round(Number(frame)));
    if (!Number.isFinite(targetFrame)) return;
    return seekVideoToFrame(targetFrame);
  }

  async function detectVideoRangeSupport() {
    try {
      const response = await fetch(videoBaseUrl(), { headers: { Range: "bytes=0-1023" } });
      videoRangeSupported = response.status === 206;
    } catch {
      videoRangeSupported = false;
    }
    if (videoRangeSupported === false) {
      setStatus("警告：视频服务器不支持 Range，播放/跳帧可能卡顿或失败，请用 serve_web_viewer");
    }
  }

  function trackDownloadName() {
    sortKeyframes();
    const count = cameraTrack.keyframes.length;
    const frames = cameraTrack.keyframes.map((keyframe) => keyframe.frame);
    const range = frames.length ? `f${frames[0]}-${frames[frames.length - 1]}` : "empty";
    const stamp = new Date()
      .toISOString()
      .replace(/[-:]/g, "")
      .replace(/\..+$/, "")
      .replace("T", "_");
    return `camera_track_${count}kf_${range}_${stamp}.json`;
  }

  function exportTrack() {
    sortKeyframes();
    const blob = new Blob([JSON.stringify(cameraTrack, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = trackDownloadName();
    link.click();
    URL.revokeObjectURL(url);
  }

  window.cadsceneGetCameraTrack = function () {
    sortKeyframes();
    return JSON.parse(JSON.stringify(cameraTrack));
  };

  window.cadsceneSetKeyframePlan = function (plan) {
    keyframePlanFrames = Array.isArray(plan?.frames) ? plan.frames.slice() : [];
    renderQualityTimeline();
  };

  window.cadsceneSetIgnoredSuggestions = function (frames) {
    ignoredSuggestionFrames = new Set(
      (frames || []).map(Number).filter((frame) => Number.isFinite(frame)),
    );
    if (sfmScene) {
      sfmScene.suggestions = qualitySuggestions.length > 0
        ? suggestionsForSfmScene()
        : visibleQualitySuggestions(sfmScene.suggestions);
      if (threeScene) threeScene.setSfmSuggestions(sfmScene.suggestions);
    }
    summarizeQualitySuggestions();
    renderQualityTimeline();
    updateSfmInfoPanel();
  };

  window.cadsceneApplyCameraParameters = function (params) {
    if (!camera || !params) return false;
    const allowed = new Set(["yaw", "pitch", "roll", "fov"]);
    const safeFields = Array.isArray(params.safe_fields)
      ? params.safe_fields.filter((key) => allowed.has(key))
      : [...allowed];
    for (const key of safeFields) {
      const value = Number(params[key]);
      if (Number.isFinite(value)) camera[key] = value;
    }
    if (defaultCamera) {
      for (const key of safeFields) defaultCamera[key] = camera[key];
    }
    const initialKeyframe = cameraTrack?.keyframes?.length === 1 ? cameraTrack.keyframes[0] : null;
    if (initialKeyframe && Number(initialKeyframe.frame) === 0 && !initialKeyframe.source) {
      for (const key of safeFields) initialKeyframe.camera[key] = camera[key];
    }
    const appliedFields = [...safeFields];
    const fps = Number(params.fps);
    if (cameraTrack && Number.isFinite(fps) && fps > 0) {
      cameraTrack.fps = fps;
      for (const keyframe of cameraTrack.keyframes) {
        keyframe.time = frameToTime(Number(keyframe.frame));
      }
      appliedFields.push("fps");
    }
    syncControls();
    updateViews({ forceOverlay: true });
    setStatus(`已应用 SfM 相机初值：${appliedFields.join(", ")}`);
    return appliedFields;
  };

  window.cadsceneApplyPureRotationPose = function (pose) {
    if (!camera || !pose || !window.CadscenePureRotationMath) return false;
    pureRotationPlaybackActive = true;
    const rotation = pose.rotation_cad_from_camera || window.CadscenePureRotationMath.localRotationToViewerMatrix(pose.rotation_local_from_camera);
    const center = pose.camera_center_web || pose.camera_center_local || [0, 0, 0];
    if (!Array.isArray(rotation) || !Array.isArray(center) || center.length !== 3) return false;
    pureRotationAuthoritativeMatrix = rotation;
    const euler = window.CadscenePureRotationMath.matrixToViewerEuler(rotation);
    euler.yaw = window.CadscenePureRotationMath.unwrapDegreesNear(euler.yaw, camera.yaw);
    euler.roll = window.CadscenePureRotationMath.unwrapDegreesNear(euler.roll, camera.roll);
    let nextCamera = {
      ...camera,
      x: Number(center[0]),
      y: Number(center[1]),
      z: Number(center[2]),
      yaw: euler.yaw,
      pitch: euler.pitch,
      roll: euler.roll,
    };
    if (Number.isFinite(Number(pose.display_fov))) nextCamera.fov = Number(pose.display_fov);
    if (threeScene && pureRotationRestrictedFields.size === 0) {
      nextCamera = threeScene.ensureCameraNearCad(nextCamera).pose;
    }
    camera = nextCamera;
    syncControls();
    updateViews({ forceOverlay: true, updateThree: true });
    return { x: camera.x, y: camera.y, z: camera.z, yaw: camera.yaw, pitch: camera.pitch, roll: camera.roll, fov: camera.fov };
  };

  window.cadsceneApplyManualPureRotationMatrix = function (rotation) {
    if (!camera || !Array.isArray(rotation) || !window.CadscenePureRotationMath) return false;
    const fixedCenter = [camera.x, camera.y, camera.z];
    pureRotationPlaybackActive = true;
    pureRotationAuthoritativeMatrix = rotation.map((row) => row.map(Number));
    camera = {
      ...camera,
      x: fixedCenter[0],
      y: fixedCenter[1],
      z: fixedCenter[2],
    };
    syncControls();
    updateViews({ forceOverlay: true, updateThree: true });
    return {
      rotation_cad_from_camera: pureRotationAuthoritativeMatrix.map((row) => row.slice()),
      camera_center_web: fixedCenter,
    };
  };

  window.cadsceneFocusVirtualCamera = function () {
    if (!camera || !threeScene) return false;
    threeScene.focusInspectOnCameraAndCad(camera);
    return true;
  };

  window.cadsceneEnsureVirtualCameraNearCad = function () {
    if (!camera || !threeScene) return { moved: false };
    const result = threeScene.ensureCameraNearCad(camera);
    if (!result.moved) return result;
    camera = { ...camera, ...result.pose };
    syncControls();
    updateViews({ forceOverlay: true, updateThree: true });
    return result;
  };

  window.cadsceneSetPureRotationEditMode = function (mode) {
    pureRotationPlaybackActive = true;
    pureRotationRestrictedFields.clear();
    const correctionMode = mode === "correction";
    if (correctionMode) {
      for (const key of ["x", "y", "z", "yaw", "pitch", "roll", "fov"]) pureRotationRestrictedFields.add(key);
    }
    if (threeScene) threeScene.setMode(correctionMode ? "rotate" : "translate");
    if (threeScene) threeScene.setTransformSpace(correctionMode ? "local" : "world");
    for (const id of ["translateMode"]) {
      const button = document.querySelector(`#${id}`);
      if (button) button.disabled = correctionMode;
    }
    syncControls();
    return { mode: correctionMode ? "correction" : "placement", restricted: [...pureRotationRestrictedFields] };
  };

  window.cadsceneGetCurrentCameraPose = function () {
    if (!camera) return null;
    const pose = cloneCameraPose(camera);
    if (pureRotationAuthoritativeMatrix) {
      pose.rotation_cad_from_camera = pureRotationAuthoritativeMatrix.map((row) => row.slice());
    }
    return pose;
  };

  window.cadscenePickCadWorld = function (event) {
    return threeScene?.pickCadWorld(event) || null;
  };

  window.cadsceneProjectCadWorldToInspect = function (point) {
    return threeScene?.projectCadWorldToInspect(point)
      || { visible: false, reason: "projection_unavailable" };
  };

  window.cadsceneProjectCadWorldPoint = function (point) {
    if (!camera || !Array.isArray(point) || !video.videoWidth || !video.videoHeight) {
      return { visible: false, reason: "projection_unavailable" };
    }
    const projected = projectPoint(point, camera, video.videoWidth, video.videoHeight);
    if (!projected) return { visible: false, reason: "behind_camera" };
    if (
      projected[0] < 0 || projected[0] >= video.videoWidth
      || projected[1] < 0 || projected[1] >= video.videoHeight
    ) return { visible: false, reason: "outside_viewport" };
    const axes = getCameraAxes(camera);
    const cameraPoint = worldToCamera(point, camera, axes);
    return {
      visible: true,
      reason: "visible",
      source_xy: projected,
      depth_m: Number(cameraPoint[2]),
    };
  };

  window.cadsceneGetDefaultCameraPose = function () {
    if (!defaultCamera || !window.CadscenePureRotationMath) return null;
    const pose = cloneCameraPose(defaultCamera);
    pose.rotation_cad_from_camera = window.CadscenePureRotationMath.viewerEulerToMatrix(pose);
    return pose;
  };

  function applyTrackPayload(payload) {
    cameraTrack = {
      version: payload.version || 1,
      video: payload.video || VIDEO_PATH,
      fps: Number(payload.fps || DEFAULT_FPS),
      keyframes: Array.isArray(payload.keyframes) ? payload.keyframes : [],
    };
    cameraTrack.keyframes = cameraTrack.keyframes.map((keyframe) => ({
      frame: Number(keyframe.frame),
      time: Number(keyframe.time ?? frameToTime(Number(keyframe.frame))),
      source: keyframe.source,
      camera: cloneCameraPose(keyframe.camera),
      // 质量评估附加字段（evaluate_sfm_alignment_quality 产出，可选；向后兼容，仅展示不影响对齐）
      quality: keyframe.quality || null,
    }));
    sortKeyframes();
    summarizeQualitySuggestions();
    if (cameraTrack.keyframes.length > 0) {
      camera = interpolateCameraAtFrame(currentFrame());
      syncControls();
    }
    syncSfmAnchoredTrackFromCurrentTrack();
    updateViews({ forceOverlay: true });
  }

  function collectSuggestedFrames() {
    // 汇总所有建议补帧的帧号：优先使用独立加载的 keyframe_suggestions.json，
    // 若无则退回 track 关键帧上标记 suggested 的帧。
    const frames = new Set();
    for (const s of visibleQualitySuggestions(qualitySuggestions)) frames.add(s.frame);
    for (const kf of cameraTrack.keyframes) {
      if (kf.quality && kf.quality.suggested) frames.add(kf.frame);
    }
    return [...frames].sort((a, b) => a - b);
  }

  function summarizeQualitySuggestions() {
    // 若导入的 track 带质量信息，在状态栏汇总「建议补关键帧」的位置，便于用户跳转。
    // 仅读取展示，不修改任何相机位姿（与只读评估的定位一致）。
    const annotated = cameraTrack.keyframes.filter((kf) => kf.quality);
    const suggested = collectSuggestedFrames();
    if (qualitySummary) {
      if (annotated.length === 0 && qualitySuggestions.length === 0 && qualityTimelineRows.length === 0) {
        qualitySummary.textContent = "未加载质量数据";
      } else if (suggested.length > 0) {
        const timelineText = qualityTimelineRows.length ? `风险时间线 ${qualityTimelineRows.length} 帧；` : "";
        qualitySummary.textContent = `${timelineText}建议补帧 ${suggested.length} 处：${suggested.slice(0, 8).join(", ")}${suggested.length > 8 ? " …" : ""}`;
      } else {
        const timelineText = qualityTimelineRows.length ? `已加载风险时间线 ${qualityTimelineRows.length} 帧，` : "已加载质量标注，";
        qualitySummary.textContent = `${timelineText}无高风险建议帧`;
      }
    }
    if (annotated.length === 0 && qualitySuggestions.length === 0 && qualityTimelineRows.length === 0) return;
    if (suggested.length > 0) {
      setStatus(`质量评估：建议优先检查/补关键帧的帧 -> ${suggested.join(", ")}`);
    } else {
      setStatus(`质量评估：已加载 ${annotated.length} 帧质量标注，无高风险建议帧`);
    }
  }

  function normalizeSuggestion(entry) {
    if (!entry || typeof entry !== "object") return null;
    const frame = Number(entry.frame_index ?? entry.frame);
    if (!Number.isFinite(frame)) return null;
    const reason = Array.isArray(entry.reason)
      ? entry.reason.join("；")
      : String(entry.reason || "");
    return {
      frame: Math.round(frame),
      priority: String(entry.priority || entry.risk_level || "medium").toLowerCase(),
      risk_score: Number(entry.risk_score ?? 0),
      reason,
      reason_codes: Array.isArray(entry.reason_codes) ? entry.reason_codes : [],
      suggest_action: String(entry.suggest_action || ""),
    };
  }

  function suggestionsForSfmScene() {
    // 右侧 3D 场景使用 frame_index 字段；质量时间轴内部使用 frame 字段。
    // 单独加载 keyframe_suggestions.json 时，用这份数据覆盖 sfm_viewer_scene.json 内嵌的旧建议。
    return visibleQualitySuggestions(qualitySuggestions).map((s) => ({
      frame_index: s.frame,
      priority: s.priority,
      risk_level: s.priority,
      risk_score: s.risk_score,
      reason: s.reason,
      reason_codes: s.reason_codes || [],
      suggest_action: s.suggest_action || "",
    }));
  }

  function applySuggestionsPayload(payload) {
    // 支持数组或 {suggestions:[...]} 两种结构。
    const list = Array.isArray(payload) ? payload : payload && payload.suggestions;
    qualitySuggestions = Array.isArray(list)
      ? list.map(normalizeSuggestion).filter(Boolean).sort((a, b) => a.frame - b.frame)
      : [];
    if (sfmScene) {
      sfmScene.suggestions = suggestionsForSfmScene();
      if (threeScene) threeScene.setSfmSuggestions(sfmScene.suggestions);
    }
    summarizeQualitySuggestions();
    renderQualityTimeline();
    updateSfmInfoPanel();
    updateSfmCurrentFrameInfo(currentFrame());
  }

  function parseCsvLine(line) {
    // 简单 CSV 解析，支持引号字段；足够读取 quality_timeline.csv。
    const out = [];
    let cur = "";
    let quoted = false;
    for (let i = 0; i < line.length; i += 1) {
      const ch = line[i];
      if (ch === "\"") {
        if (quoted && line[i + 1] === "\"") { cur += "\""; i += 1; }
        else quoted = !quoted;
      } else if (ch === "," && !quoted) {
        out.push(cur);
        cur = "";
      } else {
        cur += ch;
      }
    }
    out.push(cur);
    return out;
  }

  function applyQualityTimelineCsv(text) {
    const cleaned = String(text || "").replace(/^\uFEFF/, "").trim();
    if (!cleaned) {
      qualityTimelineRows = [];
      renderQualityTimeline();
      return;
    }
    const lines = cleaned.split(/\r?\n/).filter(Boolean);
    const header = parseCsvLine(lines[0]);
    const idxFrame = header.indexOf("frame_index");
    const idxScore = header.indexOf("risk_score");
    const idxLevel = header.indexOf("risk_level");
    if (idxFrame < 0 || idxLevel < 0) {
      qualityTimelineRows = [];
      return;
    }
    qualityTimelineRows = lines.slice(1).map((line) => {
      const cols = parseCsvLine(line);
      return {
        frame: Math.round(Number(cols[idxFrame])),
        risk_score: Number(cols[idxScore] || 0),
        risk_level: String(cols[idxLevel] || "medium").toLowerCase(),
      };
    }).filter((r) => Number.isFinite(r.frame)).sort((a, b) => a.frame - b.frame);
    summarizeQualitySuggestions();
    renderQualityTimeline();
  }

  async function loadQualityTimelineFromPath(path) {
    if (!path) return false;
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) return false;
      applyQualityTimelineCsv(await response.text());
      return true;
    } catch {
      return false;
    }
  }

  function importSuggestions(file) {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      try {
        applySuggestionsPayload(JSON.parse(reader.result));
      } catch (error) {
        setStatus(`质量建议解析失败：${error.message}`);
      }
    });
    reader.readAsText(file);
  }

  async function loadSuggestionsFromPath(path) {
    if (!path) return false;
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) return false;
      applySuggestionsPayload(await response.json());
      return true;
    } catch {
      return false;
    }
  }

  function timelineTotalFrames() {
    // 优先用视频真实时长换算总帧数；不可用时退回关键帧/建议帧的最大帧号。
    const fps = cameraTrack.fps || DEFAULT_FPS;
    if (Number.isFinite(video.duration) && video.duration > 0) {
      return Math.max(1, Math.round(video.duration * fps));
    }
    let maxFrame = 1;
    for (const kf of cameraTrack.keyframes) maxFrame = Math.max(maxFrame, kf.frame);
    for (const plan of keyframePlanFrames) maxFrame = Math.max(maxFrame, Number(plan.frame_index));
    for (const s of qualitySuggestions) maxFrame = Math.max(maxFrame, s.frame);
    for (const r of qualityTimelineRows) maxFrame = Math.max(maxFrame, r.frame);
    return maxFrame;
  }

  function renderQualityTimeline() {
    if (!qualityCanvas || !qualityContext) return;
    const dpr = window.devicePixelRatio || 1;
    const cssW = Math.max(1, qualityCanvas.clientWidth);
    const cssH = Math.max(1, qualityCanvas.clientHeight);
    const needW = Math.round(cssW * dpr);
    const needH = Math.round(cssH * dpr);
    if (qualityCanvas.width !== needW) qualityCanvas.width = needW;
    if (qualityCanvas.height !== needH) qualityCanvas.height = needH;

    const ctx = qualityContext;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const total = timelineTotalFrames();
    const xForFrame = (frame) => clamp(frame / total, 0, 1) * cssW;
    qualityMarkerHits = [];

    // 底轨
    ctx.fillStyle = "#0e1116";
    ctx.fillRect(0, 0, cssW, cssH);

    const bandTop = 6;
    const bandBottom = cssH - 14;
    const bandH = Math.max(4, bandBottom - bandTop);

    // 1) 风险着色带：优先用 quality_timeline.csv；否则退回 track 内嵌 quality。
    if (qualityTimelineRows.length > 0) {
      let start = qualityTimelineRows[0];
      for (let i = 1; i <= qualityTimelineRows.length; i += 1) {
        const row = qualityTimelineRows[i];
        const prev = qualityTimelineRows[i - 1];
        if (!row || row.risk_level !== start.risk_level || row.frame > prev.frame + 1) {
          const x0 = xForFrame(start.frame);
          const x1 = xForFrame((prev ? prev.frame : start.frame) + 1);
          ctx.globalAlpha = 0.32;
          ctx.fillStyle = RISK_COLORS[start.risk_level] || RISK_COLORS.medium;
          ctx.fillRect(x0, bandTop, Math.max(1, x1 - x0), bandH);
          ctx.globalAlpha = 1;
          start = row;
        }
      }
    } else {
      const annotated = cameraTrack.keyframes
        .filter((kf) => kf.quality && kf.quality.risk_level)
        .sort((a, b) => a.frame - b.frame);
      for (let i = 0; i < annotated.length; i += 1) {
        const kf = annotated[i];
        const nextFrame = i + 1 < annotated.length ? annotated[i + 1].frame : total;
        const x0 = xForFrame(kf.frame);
        const x1 = xForFrame(nextFrame);
        ctx.globalAlpha = 0.32;
        ctx.fillStyle = RISK_COLORS[kf.quality.risk_level] || RISK_COLORS.medium;
        ctx.fillRect(x0, bandTop, Math.max(1, x1 - x0), bandH);
        ctx.globalAlpha = 1;
      }
    }

    // 2) 关键帧刻度（浅蓝细线）。
    ctx.strokeStyle = "#56c2ff";
    ctx.lineWidth = 1;
    for (const kf of manualKeyframes()) {
      const x = xForFrame(kf.frame);
      ctx.beginPath();
      ctx.moveTo(x, bandBottom - 4);
      ctx.lineTo(x, bandBottom + 3);
      ctx.stroke();
    }

    // 3) 建议补帧：竖线 + 顶部三角标记，按 priority 上色，并记录命中区间。
    // 计划帧用紫色标出；未完成时使用虚线，避免与质量建议混淆。
    for (const plan of keyframePlanFrames) {
      const x = xForFrame(Number(plan.frame_index));
      ctx.strokeStyle = plan.status === "completed" ? "#c792ea" : "#9b59b6";
      ctx.lineWidth = 1;
      ctx.setLineDash(plan.status === "completed" ? [] : [3, 3]);
      ctx.beginPath();
      ctx.moveTo(x, bandTop + 2);
      ctx.lineTo(x, bandBottom - 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    for (const s of visibleQualitySuggestions(qualitySuggestions)) {
      const x = xForFrame(s.frame);
      const color = RISK_COLORS[s.priority] || "#ff6b6b";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(x, bandTop);
      ctx.lineTo(x, bandBottom);
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x, bandTop);
      ctx.lineTo(x - 4, bandTop - 5);
      ctx.lineTo(x + 4, bandTop - 5);
      ctx.closePath();
      ctx.fill();
      qualityMarkerHits.push({ x, frame: s.frame, suggestion: s });
    }

    // 4) 播放头
    const playX = xForFrame(currentFrame());
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(playX, 0);
    ctx.lineTo(playX, cssH);
    ctx.stroke();
  }

  function suggestionAtX(px) {
    // 命中测试：点击/悬停位置附近 6px 内的建议标记。
    let best = null;
    let bestDist = 7;
    for (const hit of qualityMarkerHits) {
      const d = Math.abs(hit.x - px);
      if (d < bestDist) {
        bestDist = d;
        best = hit;
      }
    }
    return best;
  }

  function handleTimelineClick(event) {
    if (!cameraTrack) return;
    const rect = qualityCanvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const hit = suggestionAtX(px);
    const total = timelineTotalFrames();
    const targetFrame = hit ? hit.frame : Math.round(clamp(px / rect.width, 0, 1) * total);
    if (hit && hit.suggestion.reason) {
      setStatus(`跳转到建议帧 ${targetFrame}（${hit.suggestion.priority}）：${hit.suggestion.reason}`);
    }
    goToFrame(targetFrame);
  }

  function handleTimelineHover(event) {
    if (!qualityTip) return;
    const rect = qualityCanvas.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const total = timelineTotalFrames();
    const hit = suggestionAtX(px);
    if (hit) {
      const s = hit.suggestion;
      qualityTip.innerHTML = `建议帧 ${s.frame}（${s.priority}，score ${s.risk_score.toFixed(2)}）<br>${s.reason || "—"}`;
    } else {
      const frame = Math.round(clamp(px / rect.width, 0, 1) * total);
      qualityTip.textContent = `帧 ${frame}`;
    }
    qualityTip.hidden = false;
    const wrapRect = qualityWrap.getBoundingClientRect();
    qualityTip.style.left = `${event.clientX - wrapRect.left}px`;
    qualityTip.style.top = `${qualityCanvas.offsetTop - 6}px`;
  }

  function hideTimelineTip() {
    if (qualityTip) qualityTip.hidden = true;
  }

  // --------------------------------------------------------------------
  // SfM 诊断场景加载与信息面板（不改任何相机轨迹）
  // --------------------------------------------------------------------
  function normalizeSceneTrackEntry(entry) {
    if (!entry) return entry;
    const cam = entry.camera || entry;
    return {
      frame_index: Number(entry.frame_index ?? entry.frame ?? 0),
      x: Number(cam.x ?? 0),
      y: Number(cam.y ?? 0),
      z: Number(cam.z ?? 0),
      yaw: Number(cam.yaw ?? 0),
      pitch: Number(cam.pitch ?? 0),
      roll: Number(cam.roll ?? 0),
      fov: Number(cam.fov ?? 70),
      source: entry.source || "",
    };
  }

  function normalizeSfmScenePayload(data) {
    if (!data) return null;
    const scene = data;
    scene.tracks = scene.tracks || {};
    scene.tracks.global_sfm_track = (scene.tracks.global_sfm_track || []).map(normalizeSceneTrackEntry);
    scene.tracks.anchored_camera_path = (scene.tracks.anchored_camera_path || []).map(normalizeSceneTrackEntry);
    return scene;
  }

  const UPSTREAM_SFM_MANUAL_FOV_WARNING =
    "Upstream SfM intrinsics/geometry are unreliable; manual FOV is being used.";

  function sfmSceneWarnings() {
    if (!sfmScene) return [];
    const warnings = Array.isArray(sfmScene.warnings)
      ? sfmScene.warnings.map((item) => String(item)).filter(Boolean)
      : [];
    const validation = (sfmScene.meta && sfmScene.meta.alignment_validation) || {};
    if (
      warnings.length === 0
      && validation.status === "warning"
      && validation.fov_source === "manual"
      && validation.intrinsics_warning
    ) {
      warnings.push(UPSTREAM_SFM_MANUAL_FOV_WARNING);
    }
    return warnings;
  }

  function displaySfmSceneWarnings() {
    const warnings = sfmSceneWarnings();
    if (warnings.length === 0) return false;
    setStatus(`警告：${warnings.join("；")}`);
    return true;
  }

  function applySfmScenePayload(data) {
    sfmScene = normalizeSfmScenePayload(data);
    if (sfmScene && qualitySuggestions.length > 0) {
      // 若先加载了 suggestions URL，再加载 sfmScene，用最新 suggestions 覆盖场景内旧标记。
      sfmScene.suggestions = suggestionsForSfmScene();
    }
    if (sfmScene) {
      const anchored = currentTrackAsAnchoredPath();
      if (anchored.length > 0) {
        sfmScene.tracks = sfmScene.tracks || {};
        sfmScene.tracks.anchored_camera_path = anchored;
      }
    }
    if (threeScene && sfmScene) threeScene.loadSfmScene(sfmScene);
    updateSfmInfoPanel();
    if (threeScene && sfmScene) {
      threeScene.updateSfmGhost(currentFrame());
      updateSfmCurrentFrameInfo(currentFrame());
    }
    displaySfmSceneWarnings();
  }

  function importSfmScene(file) {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      try {
        applySfmScenePayload(JSON.parse(reader.result));
        if (!displaySfmSceneWarnings()) setStatus("已加载 SfM 诊断场景");
      } catch (error) {
        setStatus(`SfM 场景解析失败：${error.message}`);
      }
    });
    reader.readAsText(file);
  }

  async function loadSfmSceneFromPath(path) {
    if (!path) return false;
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) return false;
      applySfmScenePayload(await response.json());
      return true;
    } catch {
      return false;
    }
  }

  async function loadDiagnosticsSceneFromPath(path) {
    if (!path) return false;
    try {
      const response = await fetch(path, { cache: "no-store" });
      if (!response.ok) return false;
      diagnosticsScene = await response.json();
      const warnings = diagnosticsScene && diagnosticsScene.warnings ? diagnosticsScene.warnings.length : 0;
      console.info(`[cadscene viewer] diagnosticsScene loaded, warnings=${warnings}`);
      return true;
    } catch (error) {
      console.warn("[cadscene viewer] diagnosticsScene load failed", error);
      return false;
    }
  }

  // workflow 的质量任务结束后调用：重新读取只读产物，不触碰人工相机轨迹。
  window.cadsceneReloadQualityArtifacts = async function () {
    const [timelineLoaded, suggestionsLoaded, sceneLoaded, diagnosticsLoaded] = await Promise.all([
      loadQualityTimelineFromPath(QUALITY_TIMELINE_PATH),
      loadSuggestionsFromPath(SUGGESTIONS_PATH),
      loadSfmSceneFromPath(SFM_SCENE_PATH),
      loadDiagnosticsSceneFromPath(DIAGNOSTICS_SCENE_PATH),
    ]);
    updateViews({ forceOverlay: true });
    return { timelineLoaded, suggestionsLoaded, sceneLoaded, diagnosticsLoaded };
  };

  function sfmTrackBboxSpanWarn() {
    // 简单坐标一致性检查：点云与锚定轨迹 bbox 中心水平相差过大则提醒。
    if (!sfmScene || !sfmScene.points || !sfmScene.tracks) return "";
    const pb = sfmScene.points.bbox;
    const track = sfmScene.tracks.anchored_camera_path || [];
    if (!pb || !pb.min || track.length === 0) return "";
    const xs = track.map((t) => t.x);
    const ys = track.map((t) => t.y);
    const ac = [(Math.min(...xs) + Math.max(...xs)) / 2, (Math.min(...ys) + Math.max(...ys)) / 2];
    const pc = [(pb.min[0] + pb.max[0]) / 2, (pb.min[1] + pb.max[1]) / 2];
    const dxy = Math.hypot(pc[0] - ac[0], pc[1] - ac[1]);
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1);
    if (dxy > 20 * span) {
      console.warn(`[SfM] 点云与锚定轨迹 bbox 中心相差 ${dxy.toFixed(0)}（>20x 轨迹跨度），坐标系可能不一致。`);
      return "（警告：点云与轨迹坐标可能不一致）";
    }
    return "";
  }

  function updateSfmInfoPanel() {
    const info = document.querySelector("#sfmInfo");
    if (!info) return;
    if (!sfmScene) {
      info.textContent = "当前项目没有可用的 SfM 诊断场景";
      return;
    }
    const p = sfmScene.points || {};
    const t = sfmScene.tracks || {};
    const g = t.global_sfm_track || [];
    const a = t.anchored_camera_path || [];
    const sug = sfmScene.suggestions || [];
    const sceneWarnings = sfmSceneWarnings();
    if ((p.count_exported || 0) > 200000) {
      setStatus("点云数量较大，建议用 --max-points 降采样后重新导出。");
    }
    const warn = sfmTrackBboxSpanWarn();
    info.textContent = [
      `点云：${p.count_exported || 0} / 原始 ${p.count_original || 0}（${p.sample_mode || "-"}，RGB ${p.has_rgb ? "有" : "无"}）`,
      `原始SfM轨迹帧：${g.length}　锚定轨迹帧：${a.length}　建议：${sug.length}`,
      ...sceneWarnings.map((item) => `警告：${item}`),
      warn,
    ].filter(Boolean).join("\n");
  }

  function updateSfmCurrentFrameInfo(frame) {
    const el = document.querySelector("#sfmFrameInfo");
    if (!el || !sfmScene) return;
    const nearest = (arr) => {
      if (!arr || arr.length === 0) return null;
      let best = arr[0];
      let bd = Math.abs(arr[0].frame_index - frame);
      for (const it of arr) {
        const d = Math.abs(it.frame_index - frame);
        if (d < bd) { bd = d; best = it; }
      }
      return best;
    };
    const a = nearest((sfmScene.tracks || {}).anchored_camera_path);
    const nsug = nearest(sfmScene.suggestions);
    const parts = [`帧 ${frame}`];
    if (a) parts.push(`锚定 xy=(${a.x.toFixed(1)}, ${a.y.toFixed(1)}) z=${a.z.toFixed(1)}`);
    if (nsug) parts.push(`最近建议 ${nsug.frame_index}（${nsug.priority || nsug.risk_level || ""}）`);
    el.textContent = parts.join("　");
  }

  function importTrack(file) {
    const reader = new FileReader();
    reader.addEventListener("load", () => {
      applyTrackPayload(JSON.parse(reader.result));
    });
    reader.readAsText(file);
  }

  async function loadTrackFromPaths(paths) {
    // 优先使用对齐后的轨迹；未对齐时回读本 run 已保存的人工关键帧。
    for (const path of paths.filter(Boolean)) {
      try {
        const response = await fetch(path, { cache: "no-store" });
        if (!response.ok) continue;
        applyTrackPayload(await response.json());
        console.info(`[cadscene viewer] loaded camera track: ${path}`);
        return true;
      } catch (error) {
        console.warn(`[cadscene viewer] camera track load failed: ${path}`, error);
      }
    }
    return false;
  }

  function setReviewStatus(message) {
    if (reviewStatus) reviewStatus.textContent = message;
  }

  function reviewFrame() {
    return Number(reviewPacket?.review_frame ?? reviewPacket?.predicted_keyframe?.frame);
  }

  function applyReviewPrediction() {
    if (!reviewPacket || !reviewPacket.predicted_keyframe) return;
    const keyframe = reviewPacket.predicted_keyframe;
    camera = cloneCameraPose(keyframe.camera);
    syncControls();
    updateViews({ forceOverlay: true });
    if (threeScene) threeScene.focusInspectOnCamera(camera);
    const confidence = Number(keyframe.confidence ?? 0);
    setReviewStatus(`审核帧 ${keyframe.frame} | 置信度 ${confidence.toFixed(3)} | ${keyframe.status || "预测"}`);
  }

  async function loadReviewFromPath(path) {
    if (!path) {
      setReviewStatus("审核：未加载");
      return false;
    }
    try {
      const response = await fetch(path);
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      reviewPacket = await response.json();
      const frame = reviewFrame();
      if (Number.isFinite(frame)) {
        await goToFrame(frame);
      }
      applyReviewPrediction();
      return true;
    } catch (error) {
      setReviewStatus(`审核包加载失败：${error.message}`);
      return false;
    }
  }

  function acceptReviewPrediction() {
    if (!reviewPacket?.predicted_keyframe) return;
    const keyframe = Object.assign({}, reviewPacket.predicted_keyframe, {
      source: "accepted_prediction",
    });
    upsertKeyframe(keyframe);
    camera = cloneCameraPose(keyframe.camera);
    syncControls();
    syncSfmAnchoredTrackFromCurrentTrack();
    updateViews({ forceOverlay: true });
    setReviewStatus(`已确认预测帧 ${keyframe.frame}，正在导出轨迹`);
    exportTrack();
  }

  function saveAdjustedReviewKeyframe() {
    const frame = currentFrame();
    if (!Number.isFinite(frame)) return;
    const keyframe = makeKeyframe(frame, camera, "manual_corrected");
    upsertKeyframe(keyframe);
    syncSfmAnchoredTrackFromCurrentTrack();
    updateViews({ forceOverlay: true });
    setReviewStatus(`已保存微调关键帧 ${frame}，正在导出轨迹`);
    exportTrack();
  }

  function rejectReviewPrediction() {
    if (!reviewPacket) return;
    const frame = reviewFrame();
    setReviewStatus(`已拒绝审核帧 ${Number.isFinite(frame) ? frame : "?"}`);
  }

  function toggleCadText() {
    showCadText = !showCadText;
    const button = document.querySelector("#toggleCadText");
    if (button) button.textContent = showCadText ? "隐藏标注" : "显示标注";
    if (threeScene) threeScene.setCadTextVisible(showCadText);
    updateViews({ forceOverlay: true, updateThree: false });
  }

  function isEditableTarget(target) {
    if (!target) return false;
    if (target.isContentEditable) return true;
    return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
  }

  function hotkeyName(event) {
    const key = (event.key || "").toLowerCase();
    if (["w", "e", "g", "f"].includes(key)) return key;
    const codeMap = { KeyW: "w", KeyE: "e", KeyG: "g", KeyF: "f" };
    return codeMap[event.code] || "";
  }

  function handleSceneHotkey(event) {
    if (!threeScene || isEditableTarget(event.target)) return;
    const key = hotkeyName(event);
    if (!key) return;
    event.preventDefault();
    event.stopPropagation();
    if (key === "w") threeScene.setMode("translate");
    if (key === "e") threeScene.setMode("rotate");
    if (key === "g") threeScene.setGizmoVisible(!threeScene.transformControls.visible);
    if (key === "f") threeScene.focusInspectOnCamera(camera);
  }

  function bindButtons() {
    document.querySelector("#resetCamera").addEventListener("click", () => {
      camera = Object.assign({}, defaultCamera);
      syncControls();
      updateViews();
      if (threeScene) threeScene.focusInspectOnCamera(camera);
    });
    document.querySelector("#addKeyframe").addEventListener("click", addOrUpdateKeyframe);
    document.querySelector("#deleteKeyframe").addEventListener("click", deleteCurrentKeyframe);
    document.querySelector("#previousKeyframe").addEventListener("click", () => goToKeyframe(-1));
    document.querySelector("#nextKeyframe").addEventListener("click", () => goToKeyframe(1));
    document.querySelector("#translateMode").addEventListener("click", () => threeScene && threeScene.setMode("translate"));
    document.querySelector("#rotateMode").addEventListener("click", () => threeScene && threeScene.setMode("rotate"));
    document.querySelector("#toggleGizmo").addEventListener("click", () => {
      if (!threeScene) return;
      threeScene.setGizmoVisible(!threeScene.transformControls.visible);
    });
    document.querySelector("#toggleCadText")?.addEventListener("click", toggleCadText);
    document.querySelector("#goToFrame").addEventListener("click", () => {
      const input = document.querySelector("#frameInput");
      if (!input || input.value === "") return;
      input.blur();
      goToFrame(input.value);
    });
    document.querySelector("#acceptPrediction")?.addEventListener("click", acceptReviewPrediction);
    document.querySelector("#saveAdjustedKeyframe")?.addEventListener("click", saveAdjustedReviewKeyframe);
    document.querySelector("#rejectReview")?.addEventListener("click", rejectReviewPrediction);
    document.querySelector("#frameInput")?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        document.querySelector("#goToFrame")?.click();
      }
    });
    if (qualityCanvas) {
      qualityCanvas.addEventListener("click", handleTimelineClick);
      qualityCanvas.addEventListener("mousemove", handleTimelineHover);
      qualityCanvas.addEventListener("mouseleave", hideTimelineTip);
      window.addEventListener("resize", renderQualityTimeline);
    }
    bindSfmSceneControls();
    document.addEventListener("keydown", handleSceneHotkey, true);
  }

  function bindSfmSceneControls() {
    const bindToggle = (id, fn) => {
      const el = document.querySelector(id);
      if (el) el.addEventListener("change", () => { if (threeScene) fn(el.checked); });
    };
    bindToggle("#sfmShowPoints", (v) => threeScene.setSfmPointsVisible(v));
    bindToggle("#sfmShowGlobal", (v) => threeScene.setGlobalTrackVisible(v));
    bindToggle("#sfmShowAnchored", (v) => threeScene.setAnchoredTrackVisible(v));
    bindToggle("#sfmShowFrustum", (v) => threeScene.setFrustumVisible(v));
    bindToggle("#sfmShowSuggestions", (v) => threeScene.setSuggestionsVisible(v));
    const followEl = document.querySelector("#sfmFollowOnPlay");
    if (followEl) {
      followOnPlay = followEl.checked;
      followEl.addEventListener("change", () => {
        followOnPlay = followEl.checked;
        if (followOnPlay && threeScene) threeScene.focusInspectOnCamera(camera);
      });
    }
    const useSfmEl = document.querySelector("#sfmUseSfmPose");
    if (useSfmEl) {
      sfmFollowMode = useSfmEl.checked;
      useSfmEl.addEventListener("change", () => {
        sfmFollowMode = useSfmEl.checked;
        if (sfmFollowMode && !(sfmScene && sfmScene.tracks && sfmScene.tracks.global_sfm_track)) {
          if (fallbackStatus) fallbackStatus.textContent = "未加载 SfM 场景，无法跟随原始 SfM 轨迹";
          useSfmEl.checked = false;
          sfmFollowMode = false;
          return;
        }
        const p = poseForFrame(currentFrame());
        if (p) { camera = p; syncControls(); }
        updateViews({ forceOverlay: true });
        if (threeScene) threeScene.focusInspectOnCamera(camera);
      });
    }
    document.querySelector("#sfmPointSize")?.addEventListener("input", (event) => {
      if (threeScene) threeScene.setSfmPointSize(event.target.value);
    });
    document.querySelector("#sfmColorMode")?.addEventListener("change", (event) => {
      if (threeScene) threeScene.setSfmColorMode(event.target.value);
    });
  }

  function bindVideo() {
    video.preload = "metadata";
    video.src = VIDEO_SRC;
    bindVideoFallbacks();
    detectVideoRangeSupport();
    videoInfo.textContent = "视频：正在加载…";
    video.addEventListener("loadedmetadata", () => {
      videoInfo.textContent = `视频：${video.videoWidth} × ${video.videoHeight}`;
      updateVideoDisplayTransform();
      updateViews({ forceOverlay: true });
    });
    if (typeof ResizeObserver === "function") {
      new ResizeObserver(() => {
        updateVideoDisplayTransform();
        updateViews({ forceOverlay: true });
      }).observe(videoLayer);
    }
    video.addEventListener("error", () => {
      const code = video.error?.code ?? "?";
      videoInfo.textContent = `视频加载失败（code ${code}），需 H.264 编码的 MP4`;
    });
    video.addEventListener("play", () => {
      manualFrameOverride = null;
      requestAnimationFrame(tick);
    });
    video.addEventListener("pause", () => updateViews({ forceOverlay: true }));
    video.addEventListener("seeked", () => {
      if (pureRotationPlaybackActive) {
        window.cadsceneRefreshPureRotationPose?.();
        return;
      }
      const actualFrame = Math.round((video.currentTime || 0) * cameraTrack.fps);
      const targetFrame = manualFrameOverride ?? actualFrame;
      if (sfmFollowMode || cameraTrack.keyframes.length > 0) {
        camera = poseForFrame(targetFrame);
        syncControls();
      }
      updateViews({ forceOverlay: true });
    });
  }

  async function boot() {
    cameraTrack = createInitialTrack({ x: 0, y: 0, z: 120, yaw: 0, pitch: -45, roll: 0, fov: 70 });
    bindVideo();

    setStatus("正在加载 CAD…");
    cadData = await fetchJsonWithFallback([CAD_PATH, ...CAD_FALLBACKS], "CAD JSON");
    cadTextEntityCount = (cadData.layers || []).reduce(
      (count, layer) => count + (layer.entities || []).filter((entity) => entity.type === "text").length,
      0,
    );
    configureControlRanges(cadData);
    defaultCamera = await loadInitialCamera(cadData);
    camera = Object.assign({}, defaultCamera);
    cameraTrack = createInitialTrack(camera);
    createControls();
    bindButtons();

    threeScene = createThreeScene(cadData);
    threeScene.updateVirtualCamera(camera);
    window.__threeViewerLoaded = true;
    if (fallbackCanvas) fallbackCanvas.style.display = "none";
    if (fallbackStatus) fallbackStatus.textContent = "Three.js 已加载";
    if (sceneHint) sceneHint.textContent = "左键旋转，右键平移，滚轮缩放。W/E 切换 Gizmo，G 显示/隐藏，F 聚焦 UAV。";
    setStatus(`${cadData.layers.length} 个图层，CAD 已就绪`);

    const trackLoaded = await loadTrackFromPaths([TRACK_PATH, ...TRACK_FALLBACKS]);
    if (trackLoaded) {
      setStatus(`${cadData.layers.length} 个图层，已加载 ${cameraTrack.keyframes.length} 个关键帧`);
    }
    await loadReviewFromPath(REVIEW_PATH);
    await loadSuggestionsFromPath(SUGGESTIONS_PATH);
    await loadQualityTimelineFromPath(QUALITY_TIMELINE_PATH);
    await loadSfmSceneFromPath(SFM_SCENE_PATH);
    await loadDiagnosticsSceneFromPath(DIAGNOSTICS_SCENE_PATH);
    // 通知 workflow：人工轨迹已回读完毕，此时再应用 SfM FOV 不会被旧 track 覆盖。
    window.dispatchEvent(new Event("cadsceneViewerReady"));
    if (trackLoaded) threeScene.focusInspectOnCamera(camera);
    else threeScene.focusInspectOnCad();
    updateViews({ forceOverlay: true });
    const initialFrame = Number.parseInt(INITIAL_FRAME_VALUE || "", 10);
    if (Number.isInteger(initialFrame) && initialFrame >= 0) {
      if (video.readyState < 1) {
        await new Promise((resolve) => video.addEventListener("loadedmetadata", resolve, { once: true }));
      }
      await goToFrame(initialFrame);
    }
  }

  function tick() {
    applyTrackPoseForCurrentFrame();
    if (!video.paused && !video.ended) requestAnimationFrame(tick);
  }

  boot().catch((error) => {
    setStatus(`加载失败：${error.message}`);
    if (fallbackStatus) fallbackStatus.textContent = `加载失败：${error.message}`;
    if (sceneHint) sceneHint.textContent = `加载失败：${error.message}`;
    console.error(error);
  });
})();
