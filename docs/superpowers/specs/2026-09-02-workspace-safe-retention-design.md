# 工作空间安全自动回收设计

## 目标

在不改变项目恢复、轨迹、关键帧、标注、正式渲染和合并契约的前提下，自动回收已经可以重建或已经有不可变发布副本的中间文件，避免一次完整流程留下多份视频、SfM 图片和数据库。

## 回收边界

| 生命周期 | 自动回收 | 必须保留 |
| --- | --- | --- |
| SfM 成功写出正式结果后 | `images/`、`masks/`、`database.db`、`sparse/`、`sparse_text_export/` | `camera_trajectory.json`、`sparse_points.ply`、内参、统计、报告和 manifest |
| 任务失败、取消、输入过期、被替代或中断后 | 大型媒体、帧目录、数据库和其他二进制中间文件 | 命令、进度、日志和不超过上限的文本诊断 |
| 片段渲染或场景打通已原子发布后 | attempt 中重复的渲染/候选输出 | `render_outputs/`、`scene_bridges/` 中的不可变发布结果及任务审计文件 |
| 工作台只读输入发布 | 同卷优先硬链接，失败时原子复制 | 原始 clip/CAD 与现有 URL、manifest 契约 |
| 轨迹成功、clip/solve export、关键帧、工作台保存、标注、合并输出 | 不自动删除 | 全部权威输入和输出 |
| 轨迹 run 被新版本替换 | 事务成功后删除 `.stale` 备份 | 新 run；事务失败时恢复旧 run |

第一版不按时间删除历史 revision，不提供手动清理按钮，也不触碰用户源视频、`dji/`、源码、Git 或发布包。

## 结构

新增 `cadscene.projects.retention` 作为唯一删除策略入口：

- `prune_sfm_workspace(stage_dir)` 只匹配固定的 SfM 临时子路径；
- `reclaim_terminal_attempt(...)` 只处理终态 attempt，并为每次处理写 `retention_report.json`；
- 每次删除前解析并校验目标仍位于指定 stage/attempt 根目录；符号链接或 Windows reparse point 不递归跟随；
- 回收为 best-effort，任何删除失败只记录到报告，不得把已成功任务改成失败。

`run_sfm --cleanup-workspace` 在正式输出与 manifest 成功落盘后回收 COLMAP 临时工作区。项目工作流和项目 adapter 显式传入该参数，独立调试 CLI 默认仍保留中间文件。

`LocalJobExecutor` 在子进程结束、日志句柄关闭且任务终态已经持久化后，请求 `ProjectService` 回收 attempt。服务再次校验 attempt 的项目、job 和序号身份；成功任务只有 `clip_render` 与 `scene_bridge` 且所有发布路径存在并位于 attempt 外时才允许回收。

工作台视频/CAD 资源仍以临时文件加 `os.replace` 原子发布；临时文件优先由源文件建立硬链接，同卷或权限不支持时退回 `copy2`，不改变浏览器地址或文件内容。

## 审计与故障行为

`retention_report.json` 记录策略版本、任务状态、回收字节数、删除相对路径和错误。大型 `adapter.log` 只保留尾部，其他小型 `.json/.log/.txt/.md/.csv/.yaml/.yml` 诊断保留。回收函数幂等，重复执行不报错。

进程崩溃或机器断电时，未完成回收不会影响权威输出；下次任务终态处理可再次执行。只有正式发布已经通过现有校验和原子提交后，才处理成功 attempt 的重复副本。

## 验证

- 单元测试覆盖严格路径边界、SfM 精确白名单、失败 attempt 诊断保留、成功轨迹不清理、成功渲染发布后清理、删除失败不改变任务结果和幂等性；
- CLI 测试覆盖 opt-in 行为与项目命令自动传参；
- 工作台测试覆盖硬链接优先、复制 fallback 和 `.stale` 事务清理；
- 聚焦测试后运行全量 pytest、依赖检查和 `git diff --check`。
