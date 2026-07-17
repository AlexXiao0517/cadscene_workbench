# 纯旋转视频对齐设计

## 背景

当无人机位置固定、仅旋转视角时，人工关键帧的 CAD 位置相同。相机中心没有平移基线，SfM 到 CAD 的绝对尺度不可由相机轨迹观测，普通 Sim3 尺度估计会退化为零。

## 设计

- 有有效 CAD 位置基线时，继续使用现有 oriented-keyframe Sim3 与 segment anchoring。
- 所有人工锚点位置重合时，进入 `rotation_only` 模式。
- `rotation_only` 仍通过 SfM 相机旋转和人工姿态估计全局旋转。
- Sim3 scale 使用 `1.0` 作为不可观测回退值，仅用于保持输出协议和点云形状。
- aligned camera path 的位置由人工锚点位置插值；位置完全相同时整段保持固定。
- alignment metrics、JSON 和中文报告记录 `alignment_mode=rotation_only` 与 `scale_observable=false`。

## 限制

路线姿态和 CAD overlay 可以正常生成，但点云相对 CAD 的绝对比例无法仅凭纯旋转视频确定。后续若提供已知距离或不同位置锚点，可恢复可观测尺度。

## 测试

- 纯旋转锚点不再产生零尺度异常。
- 中间帧位置保持人工标定位置。
- 普通有位移场景继续恢复已知 Sim3。
- workflow 允许启动纯旋转路线拟合。
