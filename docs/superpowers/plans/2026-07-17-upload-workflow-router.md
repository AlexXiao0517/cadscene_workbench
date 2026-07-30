# Stage 6A 上传入口与轨迹工作流自动路由实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 提供独立上传入口、保守的 SRT 能力识别和 manifest 驱动的三种轨迹工作流状态，同时保持无 SRT SfM 流程可真实运行。

**架构：** `cadscene.srt` 只负责流式解析和能力分类；`data_import` 负责持久化分析与 manifest；`serve_viewer` 仅扩展 API；独立 portal 复用这些 API 并跳转至既有 viewer。partial/full 永远是接口状态，不能触发融合或直接姿态驱动。

**技术栈：** Python 3.11、标准库 HTTP server、pytest、无构建步骤的 HTML/CSS/JavaScript。

## 全局约束

- 不修改 SfM、alignment、quality、viewer scene、road diagnostics 或 render 的核心输出协议。
- 视频与 CAD 必传；SRT 选传；仅 `.srt` 可上传。
- 所有上传保留流式写入、文件名清理与路径穿越保护。
- `sfm_only` 为 `ready`；`srt_sfm_fused`/`srt_full_pose` 为 `interface_only`。
- 只提交合成的最小 fixture，绝不提交 `project/`、`data/`、`runs/` 或真实遥测数据。
- 不引入 npm、React、Vite 或新的 Python 运行依赖。

---

### Task 1：建立 SRT 解析和能力检测契约

**文件：**
- 新建：`cadscene/srt/__init__.py`
- 新建：`cadscene/srt/schema.py`
- 新建：`cadscene/srt/parser.py`
- 新建：`cadscene/srt/capability.py`
- 新建：`tests/srt/__init__.py`
- 新建：`tests/srt/test_capability.py`
- 新建：`tests/fixtures/srt/no_attitude_partial.srt`
- 新建：`tests/fixtures/srt/full_pose_example.srt`
- 新建：`tests/fixtures/srt/drone_attitude_only.srt`
- 新建：`tests/fixtures/srt/gps_missing.srt`
- 新建：`tests/fixtures/srt/malformed.srt`

**接口：**
- `analyze_srt_stream(stream, source_file, video_duration_sec=None) -> dict[str, Any]` 返回统一分析对象。
- `detect_trajectory_capability(records, video_duration_sec=None) -> dict[str, Any]` 返回字段、覆盖率、warnings 与模式。
- 输出模式仅为 `sfm_only`、`srt_sfm_fused`、`srt_full_pose`。

- [ ] **步骤 1：写失败测试**

```python
def test_full_camera_attitude_is_classified_as_full_pose():
    analysis = analyze_srt_stream(open(FIXTURES / "full_pose_example.srt", "rb"), "full.srt")
    assert analysis["detected_mode"] == "srt_full_pose"
    assert analysis["fields"]["yaw"] is True

def test_drone_attitude_without_gimbal_is_partial():
    analysis = analyze_srt_stream(open(FIXTURES / "drone_attitude_only.srt", "rb"), "drone.srt")
    assert analysis["detected_mode"] == "srt_sfm_fused"
    assert analysis["attitude_sources"]["drone"] is True
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/srt/test_capability.py -v`

预期：因 `cadscene.srt` 尚不存在而失败。

- [ ] **步骤 3：最小实现**

实现增量字幕块读取、键值别名归一化、GPS/高度/姿态覆盖率统计、DJI 常见 `[key:value]` 与文本别名识别。仅 camera/gimbal 三轴同时有效时产生 `srt_full_pose`；解析异常、GPS/高度不足产生 `sfm_only` 与 warning。

- [ ] **步骤 4：运行模块测试**

运行：`python -m pytest tests/srt/test_capability.py -v`

预期：所有 SRT fixture 测试通过，包括 malformed、GPS 缺失和视频时长不一致 warning。

### Task 2：持久化 SRT 分析并扩展 manifest

**文件：**
- 修改：`cadscene/workflow/data_import.py`
- 修改：`tests/workflow/test_data_import.py`

**接口：**
- `import_srt(root, dataset, filename, stream, video_duration_sec=None) -> dict[str, Any]`。
- `load_srt_analysis(root, dataset) -> dict[str, Any]`。
- manifest 始终有 `srt` 与 `workflow` 块；创建数据集时其默认值表示缺失 SRT 的 `sfm_only/ready`。

- [ ] **步骤 1：写失败测试**

```python
def test_srt_import_writes_analysis_and_manifest(tmp_path: Path):
    import_video(tmp_path, "demo", "clip.mp4", io.BytesIO(b"video"))
    import_srt(tmp_path, "demo", "partial.srt", fixture_stream("no_attitude_partial.srt"))
    manifest = load_dataset_manifest(tmp_path, "demo")
    assert manifest["workflow"]["trajectory_mode"] == "srt_sfm_fused"
    assert manifest["srt"]["status"] == "partial"
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/workflow/test_data_import.py -v`

预期：`import_srt` 尚不存在而失败。

- [ ] **步骤 3：最小实现**

流式复制 SRT 到 `telemetry/`，再以文件流分析，原子写 JSON/Markdown 报告；更新 manifest 的路径、状态、覆盖率、姿态来源、warnings 和 workflow。失败时保留视频/CAD 状态，写 `failed` SRT 状态和 `sfm_only`。

- [ ] **步骤 4：运行模块测试**

运行：`python -m pytest tests/workflow/test_data_import.py tests/srt/test_capability.py -v`

预期：manifest、路径安全、缺失 SRT 默认值与失败隔离测试通过。

### Task 3：扩展 server SRT API 与 workflow 状态

**文件：**
- 修改：`cadscene/cli/serve_viewer.py`
- 修改：`tests/cli/test_serve_viewer_upload_api.py`
- 修改：`tests/cli/test_serve_viewer_workflow_api.py`

**接口：**
- `POST /api/workflow/upload-srt?dataset=<dataset>[&runId=<run_id>]`。
- `GET /api/workflow/srt-analysis?dataset=<dataset>`。
- API 成功结果含 `ok`、`dataset`、`srt_status`、`trajectory_mode`、`analysis`、`message`。

- [ ] **步骤 1：写失败测试**

```python
def test_upload_srt_api_returns_partial_mode_and_manifest(tmp_path: Path):
    process, port = start_server_with_dataset(tmp_path)
    status, payload = upload(port, "/api/workflow/upload-srt?dataset=demo", "partial.srt", PARTIAL_SRT)
    assert status == 200
    assert payload["trajectory_mode"] == "srt_sfm_fused"

def test_srt_analysis_api_returns_json_and_bad_extension_is_rejected(tmp_path: Path):
    process, port = start_server_with_dataset(tmp_path)
    status, rejected = upload(port, "/api/workflow/upload-srt?dataset=demo", "bad.txt", b"not telemetry")
    assert status == 400
    assert "extension" in rejected["error"]
    status, analysis = json_request(port, "GET", "/api/workflow/srt-analysis?dataset=demo")
    assert status == 404
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/cli/test_serve_viewer_upload_api.py -v`

预期：端点尚未注册，测试以 404 失败。

- [ ] **步骤 3：最小实现**

将 SRT 端点接入既有 `_multipart_upload` 和 JSON 错误处理；将状态写进 upload stage，但 partial/full 的下一阶段消息明确为“功能待启用”，不调用 `JobRunner.start_stage` 的融合算法。

- [ ] **步骤 4：运行 API 测试**

运行：`python -m pytest tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_workflow_api.py -v`

预期：上传、分析读取、错误 JSON、原 API 兼容和无 SRT 状态测试通过。

### Task 4：实现独立上传 portal 与 viewer 模式提示

**文件：**
- 新建：`apps/workflow_portal/index.html`
- 新建：`apps/workflow_portal/workflow_portal.js`
- 新建：`apps/workflow_portal/style.css`
- 修改：`apps/web_camera_viewer/index.html`
- 修改：`apps/web_camera_viewer/workflow.js`
- 修改：`apps/web_camera_viewer/style.css`
- 新建：`tests/viewer/test_workflow_portal_static.py`
- 修改：`tests/viewer/test_workflow_ui_static.py`

**接口：**
- Portal 在本地生成不暴露给普通用户的 dataset/run 标识，调用 create/upload API 并跳转 `/apps/web_camera_viewer/?dataset=<dataset>&runId=<run_id>&trajectoryMode=<mode>`。
- viewer 读取 manifest 的 `workflow` 块显示中文模式与 interface-only 禁用说明。

- [ ] **步骤 1：写失败测试**

```python
def test_portal_marks_video_and_cad_required_but_srt_optional():
    html = (ROOT / "apps/workflow_portal/index.html").read_text(encoding="utf-8")
    assert "视频" in html and "必传" in html
    assert "SRT" in html and "选填" in html

def test_existing_viewer_has_manifest_backed_interface_only_copy():
    script = (ROOT / "apps/web_camera_viewer/workflow.js").read_text(encoding="utf-8")
    assert "interface_only" in script
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/viewer/test_workflow_portal_static.py tests/viewer/test_workflow_ui_static.py -v`

预期：portal 不存在且现有 viewer 无 interface-only 状态，测试失败。

- [ ] **步骤 3：最小实现**

创建单页三文件 portal：独立进度、普通模式字段隐藏、debug 字段显示、manifest 结果摘要和进入项目按钮。viewer 只添加顶部模式状态与禁止 partial/full 开始未实现阶段的提示；保留当前左右布局和旧上传面板。

- [ ] **步骤 4：JS 语法与静态测试**

运行：`node --check apps/workflow_portal/workflow_portal.js`，`node --check apps/web_camera_viewer/workflow.js`，`python -m pytest tests/viewer/test_workflow_portal_static.py tests/viewer/test_workflow_ui_static.py -v`

预期：两个脚本无语法错误，静态约束测试通过。

### Task 5：完善用户文档与回归验证

**文件：**
- 新建：`docs/workflow_routing.md`
- 新建：`docs/srt_capability_detection.md`
- 修改：`README.md`
- 修改：`CHANGELOG.md`
- 修改：`docs/roadmap.md`
- 修改：`tests/scripts/test_check_no_project_dependency.py`（仅在新包被独立性检查误判时）

- [ ] **步骤 1：写失败测试**

```python
def test_workflow_routing_documentation_describes_all_three_modes():
    text = (ROOT / "docs/workflow_routing.md").read_text(encoding="utf-8")
    assert all(mode in text for mode in ("sfm_only", "srt_sfm_fused", "srt_full_pose"))
```

- [ ] **步骤 2：运行失败测试**

运行：`python -m pytest tests/viewer/test_workflow_portal_static.py -v`

预期：缺少 workflow routing 文档时失败。

- [ ] **步骤 3：最小实现**

用中文记录输入规则、自动识别、当前状态、SRT 不能当作高精度真值、partial/full 未实现，以及 no-SRT 已真实可用；更新路线图对应 Stage 6A 状态。

- [ ] **步骤 4：完整验证和提交**

运行：

```bash
python -m pytest tests/workflow tests/srt tests/viewer tests/cli/test_serve_viewer_upload_api.py tests/cli/test_serve_viewer_workflow_api.py tests/scripts/test_check_no_project_dependency.py
python -m pytest
python scripts/check_no_project_dependency.py
python -m cadscene.cli.serve_viewer --help
node --check apps/workflow_portal/workflow_portal.js
node --check apps/web_camera_viewer/workflow.js
git diff --check
git status
```

审计暂存区不得包含 `project/`、`data/`、`runs/` 或真实视频/CAD/SRT/GPS 数据。提交：

```bash
git add cadscene apps tests docs README.md CHANGELOG.md
git commit -m "feat: add upload portal and automatic trajectory workflow routing"
git push -u origin feature/upload-workflow-router
```
