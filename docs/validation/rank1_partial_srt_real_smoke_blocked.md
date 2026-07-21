# Rank-1 Partial-SRT 真实数据 Smoke 阻塞报告

## 结论

本轮未运行正式 Rank-1 constrained solver。严格配对的全视频 SfM、原视频 PTS 与 SRT 时间覆盖均通过只读检查，但同一 dataset/run 下没有人工 `camera_track` 或既有 alignment，因而无法构造一个诚实的 schema v2 solve prior 和独立 validate anchor。

按照资格门规则，没有把旧 schema、算法预测或其他 CAD 工程中的人工轨迹自动标记为 `orientation_confirmed=true`，也没有生成正式 `camera_trajectory_rank1_cad.json`。

## 脱敏输入核验

- 输入视频、SRT、SfM trajectory 与 frame timestamps 已确认属于同一 dataset/run。
- SfM 注册率：236 / 236（100%）。
- trajectory pose 与 `frame_timestamps.csv`：236 / 236 原视频 `source_frame_index` 均有 PTS，顺序一致。
- SfM 时间范围：0.000～39.206 秒。
- SRT 时间范围：0.000～39.268 秒。
- 已注册 SfM 帧全部位于 SRT 时间覆盖内。
- 已知全视频轨迹空间秩：1。
- 已知奇异值：1652.1166、5.2388、0.7119。
- 已知 linearity ratio：0.003147。
- 已知 XY baseline：341.224 m。
- 标准三维 Sim3 因 `rank-below-two`、`near-linear` 正确拒绝。

## 人工姿态先验资格审计

- 同一 dataset/run 的人工关键帧数量：0。
- 可构造 schema v2 prior 数量：0。
- qualified solve 数量：0。
- qualified validate 数量：0。
- solve/validate hold-out split：不满足。
- prior direction 与轨迹轴夹角：不可计算。

本地存在一份来自相同视频内容的人工轨迹，但它属于另一套 CAD 工程。其 CAD 相机中心和姿态不能作为本次 SfM→CAD 的 solve/validate 先验，因此未迁移、未读取为合格 prior，也未参与任何计算。

## 未执行项目

- 未创建或伪造 `camera_track_prior_v2.local.json`；
- 未运行正式 `align_rank1_srt_to_cad`；
- 未生成 scale、inlier ratio 或沿程 residual；
- 未生成 hold-out 位置/姿态误差；
- 未与 sfm_only 编造数值比较；
- 未运行 viewer scene、点云、road surface 或完整 pipeline。

## 解除阻塞所需人工信息

需要在严格配对的同一 CAD 工程中重新检查并保存至少两个不同源帧：

1. 一个 `solve` anchor：确认投影效果、yaw、pitch，优先使用 `camera_forward`；
2. 至少一个时间间隔明显的 `validate` anchor：不得参与求解；
3. 两帧均需明确 `source_frame_index` 与 PTS；
4. 只有人工确实确认 roll 时才能设置 `roll_confirmed=true`；
5. `camera_forward` 与 Rank-1 主轴夹角必须不小于 20°。

完成上述人工确认后，才可在本地生成 schema v2 副本并重新运行真实 smoke。当前证据不足以判断 Rank-1 结果是否优于或接近 sfm_only，也不建议据此进入正式流程接入。
