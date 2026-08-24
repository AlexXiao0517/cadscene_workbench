"use strict";

const VIEW_KEY = "mediaflow-project-library-view";
const state = {
  projects: [],
  view: localStorage.getItem(VIEW_KEY) === "list" ? "list" : "cards",
  loading: false,
  editingProjectId: null,
};
const $ = (selector) => document.querySelector(selector);

function svgIcon(path) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const value of path) {
    const item = document.createElementNS("http://www.w3.org/2000/svg", "path");
    item.setAttribute("d", value);
    svg.append(item);
  }
  return svg;
}

function formatTime(value) {
  if (!value) return "更新时间不可用";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {dateStyle: "medium", timeStyle: "short"}).format(date);
}

function statusLabel(status) {
  return ({new: "新建", ready: "待处理", processing: "处理中", completed: "已完成", failed: "需要处理", unavailable: "不可读取"})[status] || status;
}

function openProject(project) {
  if (!project.openable) return;
  const target = new URL("/apps/project_workspace/", window.location.origin);
  target.searchParams.set("projectId", project.project_id);
  window.location.assign(target.toString());
}

function renameButton(project, row) {
  const button = document.createElement("button");
  button.className = "project-rename-button";
  button.type = "button";
  button.title = "重命名项目";
  button.setAttribute("aria-label", `重命名 ${project.display_name}`);
  button.append(svgIcon(["m4 20 4.4-1 10.7-10.7a2.1 2.1 0 0 0-3-3L5.4 16Z", "m14.8 6.2 3 3"]));
  button.addEventListener("click", (event) => {
    event.stopPropagation();
    startRename(project, row);
  });
  return button;
}

function nameRow(project) {
  const row = document.createElement("div");
  row.className = "project-name-row";
  const name = document.createElement("button");
  name.className = "project-name-button";
  name.type = "button";
  name.textContent = project.display_name;
  name.title = project.display_name;
  name.disabled = !project.openable;
  name.addEventListener("click", () => openProject(project));
  row.append(name);
  if (project.openable) row.append(renameButton(project, row));
  return row;
}

async function startRename(project, row) {
  if (state.editingProjectId) return;
  state.editingProjectId = project.project_id;
  const input = document.createElement("input");
  input.className = "rename-input";
  input.maxLength = 120;
  input.value = project.display_name;
  input.setAttribute("aria-label", "项目名称");
  row.replaceChildren(input);
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save) => {
    if (finished) return;
    finished = true;
    state.editingProjectId = null;
    const displayName = input.value.trim();
    if (!save || !displayName || displayName === project.display_name) {
      renderProjects();
      return;
    }
    await saveRename(project, displayName);
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true);
    if (event.key === "Escape") finish(false);
  });
  input.addEventListener("blur", () => finish(true));
}

async function saveRename(project, displayName) {
  setMessage("正在保存项目名称…");
  try {
    const response = await fetch(`/api/projects/${encodeURIComponent(project.project_id)}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({display_name: displayName, expected_revision: project.revision}),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (payload.error === "revision_conflict") {
        await loadProjects();
        throw new Error("项目已在其他位置更新，已重新加载最新名称");
      }
      throw new Error(payload.error || "项目重命名失败");
    }
    await loadProjects();
    setMessage("项目名称已保存");
  } catch (error) {
    setMessage(error instanceof Error ? error.message : "项目重命名失败", true);
    renderProjects();
  }
}

function renderCards() {
  const grid = $("#projectGrid");
  grid.replaceChildren();
  for (const project of state.projects) {
    const card = document.createElement("article");
    card.className = `project-card ${project.openable ? "" : "unavailable"}`.trim();
    const open = document.createElement("button");
    open.type = "button";
    open.className = "folder-open";
    open.disabled = !project.openable;
    open.setAttribute("aria-label", `打开项目 ${project.display_name}`);
    const folder = document.createElement("span");
    folder.className = "folder-shape";
    open.append(folder);
    open.addEventListener("click", () => openProject(project));
    const time = document.createElement("time");
    time.className = "project-time";
    time.dateTime = project.updated_at || "";
    time.textContent = formatTime(project.updated_at);
    card.append(open, nameRow(project), time);
    grid.append(card);
  }
}

function renderList() {
  const body = $("#projectTableBody");
  body.replaceChildren();
  for (const project of state.projects) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    nameCell.className = "table-name-cell";
    nameCell.append(nameRow(project));
    const assets = document.createElement("td");
    const assetNames = document.createElement("span");
    assetNames.className = "asset-names";
    const video = document.createElement("span");
    video.textContent = project.video_filename || "未上传视频";
    const cad = document.createElement("small");
    cad.textContent = project.cad_filename || "未上传 CAD";
    assetNames.append(video, cad);
    assets.append(assetNames);
    const completion = document.createElement("td");
    completion.className = "completion";
    completion.textContent = `${project.rendered_clip_count} / ${project.clip_count}`;
    const status = document.createElement("td");
    const pill = document.createElement("span");
    pill.className = `status-pill ${project.status}`;
    pill.textContent = statusLabel(project.status);
    status.append(pill);
    const updated = document.createElement("td");
    updated.textContent = formatTime(project.updated_at);
    row.append(nameCell, assets, completion, status, updated);
    body.append(row);
  }
}

function renderProjects() {
  const hasProjects = state.projects.length > 0;
  $("#emptyState").hidden = hasProjects || state.loading;
  $("#projectGrid").hidden = !hasProjects || state.view !== "cards";
  $("#projectTable").hidden = !hasProjects || state.view !== "list";
  $("#cardViewButton").classList.toggle("active", state.view === "cards");
  $("#listViewButton").classList.toggle("active", state.view === "list");
  $("#cardViewButton").setAttribute("aria-pressed", String(state.view === "cards"));
  $("#listViewButton").setAttribute("aria-pressed", String(state.view === "list"));
  if (!hasProjects) return;
  renderCards();
  renderList();
}

function setView(view) {
  state.view = view;
  localStorage.setItem(VIEW_KEY, view);
  renderProjects();
}

function setMessage(message, error = false) {
  const holder = $("#liveMessage");
  holder.textContent = message;
  holder.classList.toggle("error", error);
}

async function loadProjects() {
  state.loading = true;
  $("#errorState").hidden = true;
  setMessage("正在读取本地项目…");
  renderProjects();
  try {
    const response = await fetch("/api/projects", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "项目库读取失败");
    state.projects = Array.isArray(payload.projects) ? payload.projects : [];
    setMessage(state.projects.length ? `共 ${state.projects.length} 个项目` : "");
  } catch (error) {
    state.projects = [];
    $("#errorState").hidden = false;
    $("#errorMessage").textContent = error instanceof Error ? error.message : "项目库读取失败";
    setMessage("项目库读取失败", true);
  } finally {
    state.loading = false;
    renderProjects();
  }
}

$("#cardViewButton").addEventListener("click", () => setView("cards"));
$("#listViewButton").addEventListener("click", () => setView("list"));
$("#refreshButton").addEventListener("click", loadProjects);
$("#retryButton").addEventListener("click", loadProjects);
$("#sidebarToggle").addEventListener("click", () => {
  const collapsed = $("#appShell").classList.toggle("sidebar-collapsed");
  $("#sidebarToggle").setAttribute("aria-expanded", String(!collapsed));
  $("#sidebarToggle").setAttribute("aria-label", collapsed ? "展开侧栏" : "收起侧栏");
});

renderProjects();
loadProjects();
