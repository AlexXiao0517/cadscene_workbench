# 工作台轨迹进度同步设计

## 目标

项目片段管理界面与工作台必须展示同一个轨迹任务的相同状态、阶段和进度。用户从管理界面启动批量轨迹反算后，在任务运行期间进入对应片段工作台，工作台应自动接管该活动任务的监看，不要求再次点击“开始运行”。

取消任务后，持久任务历史继续保留 `cancelled`，但片段重新呈现为可处理状态并计入“待处理”；用户再次批量提交或在工作台启动时，才以新 attempt 重新入队，不自动重跑。

## 权威状态与数据流

- `/api/projects/<project_id>/snapshot` 中当前 clip 的 `job_id`、`status`、`stage`、`progress` 是两端唯一权威状态。
- 工作台的 runtime 接口只用于追加实时日志，不再覆盖项目快照给出的可见进度。
- `workflow_start` 工作台会话启动后立即读取项目快照：若当前 clip 已有活动 trajectory job，则自动绑定该 job 并进入现有轮询闭环。
- 活动任务成功后沿用现有 `trajectory-ready` 会话升级流程；失败、取消或输入失效时停止监看并恢复工作台可操作状态。

## 取消与重新处理

- 工作台和项目管理界面的取消按钮调用同一 job cancel API。
- 后端 job 保持 `cancelled`，保留 attempt、日志和审计信息。
- 项目管理界面把当前 trajectory job 为 `cancelled` 的片段显示为“待处理”，并计入待处理数量；失败任务仍显示“失败”。
- 批量轨迹提交遇到同输入的 cancelled trajectory job 时，显式创建下一 attempt 并重新排队，不能把已取消的旧终态当作成功入队结果返回。

## 兼容与边界

- 不改变 SfM、Pure Rotation、场景打通或进度计算算法。
- 不影响其他 clip 的并行只读操作，也不自动取消或重启正在运行的任务。
- queued、preparing、running、validating、success、failed 等既有状态语义保持不变。
- 只将用户明确取消的 trajectory job 映射回待处理；失败和输入失效仍保留各自诊断状态。

## 测试

1. 活动 trajectory job 存在时打开 `workflow_start` 工作台，会自动绑定同一 job。
2. 工作台进度严格采用 snapshot 的 clip fraction，runtime 只更新日志。
3. 工作台取消调用同一 cancel API，并停止当前监看。
4. cancelled trajectory 在项目管理界面显示为待处理并使待处理数量加一。
5. cancelled trajectory 重新批量提交后产生新 attempt 并重新进入 active 状态。
6. 聚焦测试和全量回归均通过。
