export async function createProject(projectId, displayName) {
  const response = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, display_name: displayName }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `创建项目失败（HTTP ${response.status}）`);
  return result;
}

export function uploadAsset(projectId, kind, file, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const body = new FormData();
    body.append("file", file, file.name);
    xhr.open("POST", `/api/projects/${encodeURIComponent(projectId)}/uploads/${kind}?useCurrentRevision=1`);
    xhr.responseType = "json";
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) onProgress({ loaded: event.loaded, total: event.total, acknowledged: false });
    });
    xhr.addEventListener("load", () => {
      const result = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress({ loaded: file.size, total: file.size, acknowledged: true });
        resolve(result);
      } else reject(new Error(result.error || `上传失败（HTTP ${xhr.status}）`));
    });
    xhr.addEventListener("error", () => reject(new Error("上传连接中断，请检查服务是否运行")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(body);
  });
}

export async function getSnapshot(projectId, etag = "") {
  const headers = etag ? { "If-None-Match": etag } : {};
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/snapshot`, { cache: "no-store", headers });
  if (response.status === 304) return { snapshot: null, etag };
  const snapshot = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(snapshot.error || `读取项目状态失败（HTTP ${response.status}）`);
  return { snapshot, etag: response.headers.get("ETag") || "" };
}

export async function retryAnalysis(projectId, expectedRevision) {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/analysis/start`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ expected_revision: expectedRevision }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `重试失败（HTTP ${response.status}）`);
  return result;
}

export async function activateCandidateAnalysis(
  projectId,
  candidateAnalysisRevision,
  expectedProjectRevision,
  expectedClipsRevision,
) {
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}/analysis/activate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      candidate_analysis_revision: candidateAnalysisRevision,
      expected_revision: expectedProjectRevision,
      expected_clips_revision: expectedClipsRevision,
    }),
  });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `激活分析结果失败（HTTP ${response.status}）`);
  return result;
}
