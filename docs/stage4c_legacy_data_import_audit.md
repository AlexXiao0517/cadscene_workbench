# Stage 4C 旧数据导入实现审计

## 可直接复刻的约定

- `project/web_camera_viewer/paths.js` 的路径优先级可继续使用：显式 URL 参数优先，其次按 dataset 推导数据路径。新实现保留这一行为，并增加 `dataset_manifest.json` 作为后端任务的统一输入来源。
- 旧 viewer 使用 `data/<dataset>/<dataset>.mp4`、`data/<dataset>/design.json` 和 `camera_track_*.json`。新项目继续兼容这些文件名，同时允许 manifest 指向上传时的原始视频文件名。
- 旧 viewer 对 `design.json`、camera track、quality suggestions 和 SfM scene 的加载结构已经验证可用；Stage 4C 不改变这些前端 schema。
- `project/cadvideo/pipeline/export_web_viewer_assets.py` 中将 `road_center.json`、`road_edge.json`、`road_ref.json` 汇总为 viewer assets 的组织方式可作为 CAD assets ZIP 的兼容依据。ZIP 内这些文件会保留，嵌套的 `design.json` 会提升为 dataset 根目录的 canonical 文件。

## 仅作为参考的实现

- `export_web_viewer_assets.py` 的视频硬链接、符号链接、裁剪和复制策略仅用于理解旧数据准备流程。浏览器上传采用流式复制和原子替换，不创建指向旧目录的链接。
- 旧 pipeline 中 DXF 文本解析和 CAD 导出参数只作为未来 CAD 转换阶段参考。本阶段只接收已经生成的 `design.json` 或 CAD assets ZIP。
- 旧 `out/` 中的 metadata 只用于理解输入输出关联，不作为新项目默认路径，也不作为运行时依赖。

## 不迁移

- 不迁移旧 `out/` 默认路径、旧项目绝对路径和 `project/` 运行时引用。
- 不迁移 DXF/DWG 自动解析、视频裁剪、硬链接或符号链接逻辑。
- 不迁移旧 tracker、semantic refine 或其他已归档算法。

## 新项目映射

- 数据流式保存、manifest、ZIP 安全检查：`cadscene/workflow/data_import.py`
- 上传和查询 API：`cadscene/cli/serve_viewer.py`
- 上传面板和进度：`apps/web_camera_viewer/index.html`、`apps/web_camera_viewer/workflow.js`
- SfM/quality/render 输入解析：`cadscene/workflow/job_runner.py`
- 标准目录：`data/<dataset>/`；运行输出仍为 `runs/<dataset>/<run_id>/`

`project/` 在整个实现中只用于本次只读审计；删除该目录后，上述导入、viewer 和工作流仍可独立运行。
