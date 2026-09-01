# Scene Bridge Restart Recovery Design

## Goal

修复场景打通任务在成功发布或重试后因 `operation_id` 更新而无法在服务重启时恢复的问题，并避免已经保存的工作台结果被历史打通状态误报为“结果已失效”。

## Design

- scene bridge 请求使用任务的稳定提交身份定位。新任务写入 `submission_operation_id`，后续发布、重试和恢复只改变 `operation_id`，不改变请求身份。
- 兼容旧项目：当旧任务没有 `submission_operation_id` 时，从现有请求文件中按任务输入指纹和目标片段匹配语义请求，不能匹配时继续按失效处理，不猜测结果。
- 项目管理界面中，若工作台状态已是 `saved`，不再用历史 `stale_input/superseded` bridge job 覆盖该片段的正式保存状态。
- 不修改重叠 SfM、路线种子、CAD 坐标或相邻片段选择算法。

## Verification

- 后端回归覆盖成功发布后 `operation_id` 改变仍能校验当前输入，以及失败重试仍复用同一任务。
- 前端静态回归覆盖 saved workbench 对历史 bridge 错误提示的优先级。
- 运行聚焦测试、完整测试和 `git diff --check`。
