# 发布工程整理与项目库设计

## 目标

在不编写开源许可证、不引入账号系统、不改变现有视频处理数学和工作流契约的前提下，完成两项可独立验收的工作：

1. 清理明确废弃的仓库内容，补齐正式运行依赖、wheel 静态资源、统一 CLI 和环境检查；
2. 新增带统一侧栏的项目库，让本地用户无需保存 `projectId` URL 即可查看并打开已有项目。

## 发布定位与范围

- `sfm_only` 和 `pure_rotation` 都是正式可用工作流。
- 自动视频分析与工作流推荐仍是不成熟能力，界面必须允许用户人工覆盖。
- 语义分割不属于当前产品能力，删除 `segmentation` 安装 extra，不安装 Torch/Transformers。
- `pycolmap` 属于正式默认依赖。
- Pure Rotation 继续使用独立的 `pure_rotation_camera_poc` OpenGV 后端。它属于完整产品的正式必备后端，但本轮不搬入主仓库、不改为 submodule。
- 不新增 LICENSE，不制作 exe，不增加用户注册、登录、删除、归档或项目导出。

## 仓库清理

- 删除明确废弃的 `apps/web_camera_viewer_broken_stage3f`。
- 保留 `legacy` 迁移说明、现有技术文档和历史设计记录。
- 继续忽略运行日志、本地项目、大型媒体、模型、构建目录和 wheel 产物。
- 不删除用户数据、`dji` 数据源、现有项目或运行记录。

## 依赖、wheel 与资源定位

基础安装声明当前服务和正式工作流直接需要的依赖，包括 NumPy、SciPy、PyYAML、Pillow、OpenCV、ezdxf、imageio-ffmpeg 和 pycolmap。诊断依赖保留为独立 extra；语义分割 extra 删除。

正式 `apps/` 与 `configs/` 必须进入 wheel。运行时通过统一资源定位器解析应用根目录，使源码 checkout、editable install 和 wheel install 使用同一份前端及配置，不维护复制的静态资源目录。wheel smoke 必须证明：

- 上传页、项目库、项目管理和工作台静态文件存在；
- SfM 与 Pure Rotation 固定配置存在；
- 安装后的 CLI 不依赖 Git 仓库根目录。

## CLI 与环境检查

新增统一入口：

```text
cadscene-workbench serve [现有 serve_viewer 参数]
cadscene-workbench doctor [--storage-root PATH] [--json]
```

`serve` 保持默认监听 `127.0.0.1`，并在占用端口、静态资源缺失、配置缺失或存储目录不可写时快速失败。现有 `python -m cadscene.cli.serve_viewer` 继续兼容。

`doctor` 分组报告：

- 核心 Python 依赖与正式包资源；
- FFmpeg/ffprobe；
- SfM 的 pycolmap；
- Pure Rotation 后端目录、固定 OpenGV 版本、专用 Python 环境、标定配置和必要依赖；
- storage root 的存在性和可写性。

缺少 Pure Rotation 后端时服务仍可启动并使用 SfM，但 doctor 必须明确报告“完整能力缺失”，不能称其为实验能力。自动视频分析精度不由 doctor 判定。

## 项目库 API

新增只读 `GET /api/projects`。服务从当前 `storage-root/projects` 枚举合法项目，按 `updated_at` 倒序返回：

- `project_id`
- `revision`
- `display_name`
- `updated_at`
- 视频与 CAD 文件名
- 片段总数、已完成数、进行中数
- 汇总状态

API 不返回绝对路径。单个损坏 manifest 不得使整个列表失败；该目录以 `unavailable` 项目返回，只暴露安全项目 ID 和用户可读状态，禁止打开。

项目重命名复用现有 `PATCH /api/projects/{project_id}`、`expected_revision` 和 ProjectService 原子更新，不建立第二套 repository 或清单。revision conflict 时前端重新加载项目库并提示用户，不覆盖并发修改。

## 项目库界面

新增 `/apps/project_library/`，使用与项目管理页一致的深色应用壳、Logo、导航顺序和可收起侧栏。它是独立页面，避免让现有 `project_workspace.js` 继续膨胀；侧栏导航使用户感知为同一应用。

侧栏行为：

- “项目文件”在项目库中选中；
- 项目管理页的“项目文件”跳转项目库；
- 项目库没有当前项目，“片段管理”不猜测或自动打开历史项目；
- “项目概览”和“设置”保持未实现状态，本轮不扩展。
- 侧栏恢复项目管理页改版前的图标：原文件夹、齿轮和文字式收起符号；项目库与项目管理页保持同一套图标，不再使用草图中的新线性图标。

主区域包含“项目库”标题、新建项目按钮、卡片/列表视图切换和刷新。新建项目跳转现有上传页。

卡片视图默认启用，以固定宽度项目项从左向右排列，不拉伸铺满整行；每个项目仅显示紧凑文件夹图标、项目名称和名称下方的灰色更新时间。列表视图显示项目名称、视频/CAD 文件名、片段完成度、当前状态和最近更新时间。两种视图共享同一前端状态和重命名组件。重命名图标默认不常驻，仅在项目名称 hover 或键盘 focus-within 时出现。最后选择的视图写入 `localStorage`，项目数据不写入浏览器存储。

页面只在首次进入、手动刷新和重命名完成后请求项目列表，不持续高频扫描大型项目目录。空项目库提供“新建第一个项目”；API 失败提供重试；不可读取项目禁用打开与重命名。

### 视觉一致性修正

项目库虽然继续作为独立静态页面实现，但视觉上必须属于现有项目管理应用：直接复用项目管理页既有的颜色变量、径向深色背景、228 px 侧栏、88 px 顶栏、按钮、边框、圆角和表格层级。项目库只保留文件夹卡片与卡片/列表切换这些专属内容，不引入第二套蓝色主题或独立应用壳。为避免影响稳定页面，本轮不抽取新的共享 CSS，也不改动项目管理页布局。

项目库和项目管理页都复用上传页及 Viewer 已有的 `mediaflow-theme` 本地设置。侧栏使用太阳/月亮开关：项目管理页放在“当前项目”上方，项目库放在“收起侧栏”上方；展开时显示目标主题文字，收起时只保留图标。深浅色切换必须覆盖页面背景、侧栏、顶栏、项目卡片、表格、按钮、输入控件和弹窗，并在四个页面之间持续同步。

## 测试与验收

所有生产行为按 TDD 实现。测试至少覆盖：

- 空项目库、更新时间排序、摘要字段和无绝对路径；
- 损坏 manifest 隔离；
- 重命名成功及 revision conflict；
- 卡片/列表切换、偏好恢复和统一侧栏导航；
- wheel 中正式网页、配置和 console script；
- doctor 的健康环境、缺少 pycolmap、缺少 OpenGV、FFmpeg 不可用和只读存储目录；
- 源码启动、wheel 安装启动及现有模块入口兼容；
- 上传、片段管理、工作台、渲染和合并全量回归。

最终运行聚焦测试、全量 pytest、`scripts/check_no_project_dependency.py`、wheel 安装 smoke 和 `git diff --check`。本分支不合并 main，待用户界面验收后再决定集成。
