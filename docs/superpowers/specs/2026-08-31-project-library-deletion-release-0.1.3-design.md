# 项目库删除与 0.1.3 发布设计

## 目标

项目库支持勾选一个或多个项目并永久删除其工作空间。删除只覆盖 CADScene 在当前 `storage_root` 下为项目创建的目录，不读取或删除 manifest 中记录的外部 MP4、DXF 或 `dji` 源文件。

## 交互

- 卡片和列表均提供选择框，顶部显示“删除项目”及已选数量。
- 删除前使用页面内确认对话框列出项目名称，并明确提示“项目工作空间将永久删除，外部源文件不会删除”。
- 删除进行中禁用重复操作；成功后刷新项目库；部分失败时保留失败原因。
- 不可打开的损坏项目暂不允许从 UI 删除，避免缺少 revision 时误删。

## 服务与安全边界

- 新增 `DELETE /api/projects/<project_id>`，请求携带 `expected_revision` 和与项目 ID 完全一致的 `confirmation`。
- revision 不一致返回 409；确认文本不一致返回 400。
- 存在 queued/preparing/running/validating/publishing 任务，或 editing/pending_save 工作台会话时返回 409。
- 删除目标仅为：`projects/<project_id>`，以及 clips manifest 精确列出的 `data/<project_id>-<clip_id>`、`runs/<project_id>-<clip_id>`。
- 所有目标必须解析在 `storage_root` 内；先移动到同盘隐藏清理目录，再递归清理，避免项目库看到半删除状态。
- 不跟随 manifest 中的源文件路径，不使用宽泛 glob，不删除 storage root、`dji` 或任意外部目录。

## 兼容与发布

- 现有项目读取、重命名、工作台和批处理接口不变。
- 版本统一升级为 0.1.3；合并 main 的最新质量检测进度修复后运行全量回归。
- 本轮只本地合并 main，不自动推送。
