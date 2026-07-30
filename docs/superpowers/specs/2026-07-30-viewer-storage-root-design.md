# Viewer 独立存储根目录设计

## 背景

`serve_viewer` 当前用同一个 `--root` 同时定位网页静态资源以及 `data/`、`runs/`。当网页代码从 Git 工作树提供、数据需要写回主项目目录时，只能使用 junction；路径解析后文件位于工作树之外，清单相对路径和静态文件安全检查因此拒绝上传。

## 目标

- 网页和 Python 代码可以来自独立工作树。
- 上传数据和运行产物可以明确写入另一项目目录。
- 不传新参数时保持现有行为。
- 保留目录穿越和符号链接逃逸防护。

## 方案

为 `cadscene.cli.serve_viewer` 新增可选参数 `--storage-root`：

- `--root` 继续表示静态资源根目录。
- `--storage-root` 表示工作流存储根目录；省略时默认等于 `--root`。
- 所有 dataset manifest、上传文件、job status、阶段日志和产物均使用 storage root。
- `JobRunner` 使用 storage root。
- 当两个根目录不同时，服务器把 URL 前缀 `/data/` 和 `/runs/` 安全映射到 storage root 下的同名目录；其他静态资源仍来自 `--root`。

每个映射分别以其解析后的目录作为安全边界。不得通过取消 `resolve()` 或放宽整个根目录检查来兼容 junction。

## 数据流

1. 浏览器从 static root 加载 workflow portal。
2. 创建 dataset 时，在 `<storage-root>/data/<dataset>/` 写入 manifest。
3. 创建 run 时，在 `<storage-root>/runs/<dataset>/<run-id>/` 写入 `job_status.json`。
4. manifest 继续保存 `data/...` 形式的 URL 相对路径。
5. 浏览器访问 `/data/...` 或 `/runs/...` 时，由独立安全映射读取 storage root。

## 错误处理

- static root 或 storage root 不存在时，服务启动失败并输出明确路径。
- `--extra-root` 不得覆盖保留映射名 `data` 或 `runs`。
- dataset、run ID 和映射内路径继续使用现有校验。

## 测试

- 参数默认值：未传 `--storage-root` 时与 `--root` 相同。
- 分离根目录集成测试：创建 dataset、上传视频、读取 manifest，并确认文件实际落在 storage root。
- 确认 `job_status.json` 写入 storage root 的 `runs/`。
- 通过 `/data/...` 读取已上传文件。
- 保留并运行现有路径穿越、非法 run ID、上传与完整测试套件。

## 非目标

- 不改变 workflow 路由规则。
- 不迁移或删除已有 `data/`、`runs/`。
- 不修改 SfM、partial-SRT、pure-rotation 算法。
