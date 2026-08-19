# 当前项目工作流文档更新设计

## 目标

把面向用户和维护者的现行文档更新到 `main@a53fbd1` 的真实行为，覆盖
项目管线、工程信息标牌、全局 CAD 替换、服务重启恢复和正式存储布局。
中英文 README 保持一致，历史 `superpowers/specs`、`superpowers/plans`
和 SOP 保留为历史记录，不回写为当前运行手册。

## 文档分层

- `README.md`、`README_EN.md`：面向首次使用者，说明正式入口、完整流程、
  当前能力和限制。
- `docs/README.md`：文档导航和能力状态总表。
- `docs/design/system-architecture.md`：项目领域、队列、工作台、标牌、渲染、
  合并、CAD 替换和恢复边界。
- `docs/technical/developer-guide.md`：开发环境、正式启动参数、目录结构、测试
  和安全操作约束。
- `docs/technical/api-and-artifacts.md`：当前 Project API、五类 manifest、不可变
  输出和 source PTS/frame-map 契约。
- `docs/technical/troubleshooting.md`：旧项目恢复、会话失效、任务 pending、
  CAD 替换失败和标牌渲染问题的排查路径。
- `CHANGELOG.md`、`docs/roadmap.md`：区分已经交付、实验性、暂时隐藏和未来工作。

## 事实边界

- `sfm_only` 是稳定主路径；`pure_rotation` 仍为实验性路径。
- 上传后进入项目片段管理，片段轨迹、渲染和最终合并由持久化项目队列管理。
- CAD 原图中的 `TEXT`、`MTEXT` 和块属性文字可在 CAD 视图显示。
- Stage 9 当前用户界面只开放 CAD 锚定工程标牌。视频目标跟踪后端及历史数据
  兼容保留，但创建入口暂时隐藏，不作为当前可验收功能宣传。
- 工程标牌由屏幕空间卡片、折线引线和锚点组成；编辑仅使对应片段渲染失效。
- 全局 CAD 替换仅对已有有效工作台输出的项目开放，要求用户确认坐标系相同。
  成功后保留轨迹和工作台输出，只使渲染与合并结果失效；失败时旧 CAD 继续活动。
- 服务重启按不可变输入快照验证替换前分析任务，再绑定当前项目契约；不重跑分析，
  也不修改历史输出。
- 正式渲染和合并以 source decoded-frame integer PTS、精确 time base 和
  `render_frame_map.json` 为权威，不使用固定 FPS 帧号推导。

## 校验

文档修改后执行 Markdown 链接检查、命令/路径关键词检查、
`python -m pytest -p no:cacheprovider`、
`python scripts/check_no_project_dependency.py` 和 `git diff --check`。
不修改源代码、测试逻辑或运行数据。
