(function () {
  const paths = window.resolveViewerPaths();
  const CAD_PATH = paths.cad;
  const CAD_FALLBACKS = paths.cadFallbacks || [];
  const VIDEO_PATH = paths.video;
  const status = document.querySelector("#fallbackStatus");
  const hint = document.querySelector("#sceneHint");
  const canvas = document.querySelector("#fallbackSceneCanvas");
  const video = document.querySelector("#sourceVideo");

  window.addEventListener("error", (event) => {
    if (!window.__threeViewerLoaded) setStatus(`脚本错误：${event.message}`);
  });

  window.addEventListener("unhandledrejection", (event) => {
    if (!window.__threeViewerLoaded) setStatus(`模块错误：${event.reason?.message || event.reason}`);
  });

  function setStatus(message) {
    if (window.__threeViewerLoaded) return;
    if (status) status.textContent = message;
    if (hint) hint.textContent = message;
  }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width));
    const height = Math.max(1, Math.floor(rect.height));
    if (canvas.width !== width || canvas.height !== height) {
      canvas.width = width;
      canvas.height = height;
    }
    return { width, height };
  }

  async function fetchJsonWithFallback(paths, label) {
    let lastError = null;
    for (const path of paths.filter(Boolean)) {
      try {
        const response = await fetch(path);
        if (response.ok) return response.json();
        lastError = new Error(`${label} ${response.status}: ${path}`);
      } catch (error) {
        lastError = error;
      }
    }
    throw lastError || new Error(`${label} load failed`);
  }

  function drawFallbackCad(cadData) {
    if (!canvas || !cadData?.meta?.bbox) return;
    const ctx = canvas.getContext("2d");
    const { width, height } = resizeCanvas();
    const bbox = cadData.meta.bbox;
    const cadWidth = Math.max(1, cadData.meta.width || bbox.max_x - bbox.min_x);
    const cadHeight = Math.max(1, cadData.meta.height || bbox.max_y - bbox.min_y);
    const padding = 24;
    const scale = Math.min((width - padding * 2) / cadWidth, (height - padding * 2) / cadHeight);
    const offsetX = (width - cadWidth * scale) * 0.5;
    const offsetY = (height - cadHeight * scale) * 0.5;
    const project = (point) => [offsetX + (point[0] - bbox.min_x) * scale, offsetY + (bbox.max_y - point[1]) * scale];

    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = "#080b10";
    ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = "#263442";
    ctx.strokeRect(offsetX, offsetY, cadWidth * scale, cadHeight * scale);

    let entities = 0;
    for (const layer of cadData.layers || []) {
      for (const entity of layer.entities || []) {
        const points = entity.world_points;
        if (!Array.isArray(points) || points.length < 2) continue;
        ctx.beginPath();
        const first = project(points[0]);
        ctx.moveTo(first[0], first[1]);
        for (const point of points.slice(1)) {
          const next = project(point);
          ctx.lineTo(next[0], next[1]);
        }
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = entity.color || layer.color || "#ffffff";
        ctx.stroke();
        entities += 1;
      }
    }
    setStatus(`CAD 预览正常：${entities} 个实体`);
  }

  async function bootFallback() {
    if (location.protocol === "file:") {
      setStatus("请用本地 HTTP 服务打开，不要双击 HTML");
      return;
    }
    setStatus("诊断：正在加载 CAD JSON");
    const cadData = await fetchJsonWithFallback([CAD_PATH, ...CAD_FALLBACKS], "CAD JSON");
    drawFallbackCad(cadData);
    window.addEventListener("resize", () => drawFallbackCad(cadData));
    if (video) {
      video.addEventListener("loadedmetadata", () => setStatus(`CAD 预览正常，视频 ${video.videoWidth}x${video.videoHeight}`));
      video.addEventListener("error", () => setStatus(`CAD 预览正常，但视频加载失败：${VIDEO_PATH}`));
    }
  }

  bootFallback().catch((error) => setStatus(`诊断失败：${error.message}`));

  window.setTimeout(() => {
    if (!window.__threeViewerLoaded) {
      if (!window.THREE || !window.THREE.OrbitControls || !window.THREE.TransformControls) {
        setStatus("Three.js 未加载，仅显示 CAD 预览");
      } else {
        setStatus("viewer 初始化未完成，请查看控制台第一条错误");
      }
    }
  }, 3000);
})();
