# CAD 替换后项目恢复兼容设计

## 背景

项目完成初始 CAD/视频分析后会保存分析任务 identity 和不可变的
`_analysis_revisions.*.input_snapshot`。全局替换 CAD 会更新当前 `cad` 资产，
但不会也不应重跑项目分析。现有启动恢复逻辑却使用当前资产校验旧分析任务，
因此把合法任务误判为 identity 不匹配并阻止整个服务启动。

## 设计

恢复分析任务时首先保持现有的当前契约校验。只有当前契约不匹配、需要执行
旧任务迁移时，才查找与当前分析 DAG 精确对应的不可变分析修订快照：

- 修订必须由当前记录的 video analysis job 标识；
- `input_snapshot.request_key` 必须与 `_analysis.request_key` 完全一致；
- 快照必须包含创建原分析任务所需的 CAD、视频和可选 SRT 资产；
- 原分析任务可能使用当前 identity schema，也可能使用旧版无 project scope 的
  legacy schema；两种 fingerprint 和 idempotency key 都使用该快照精确校验，
  而不是替换后的当前资产。

快照缺失、归属不一致或 identity 仍不匹配时继续 fail closed。不会跳过恢复错误，
也不会修改现有项目 manifest、分析输出或 CAD 替换历史。

## 测试

新增恢复回归测试覆盖：

1. 原 CAD 下完成并验证的分析 DAG；
2. 保存不可变分析输入快照；
3. 当前 CAD 被同坐标系新版本替换；
4. `restore_jobs` 可以迁移快照下的当前格式或 legacy identity 并恢复队列；
5. 篡改 identity 时仍拒绝恢复。

完成后运行项目服务聚焦测试、完整测试、依赖扫描和 `git diff --check`，再使用真实
hygs 项目启动 `serve_viewer` 并验证项目 API 返回六个片段。

## 非目标

- 不修改 CAD 替换、SfM、Pure Rotation 或渲染算法；
- 不自动重跑项目分析；
- 不放宽缺少可信历史快照时的 identity 校验；
- 不修改人工验收项目数据来绕过恢复。
