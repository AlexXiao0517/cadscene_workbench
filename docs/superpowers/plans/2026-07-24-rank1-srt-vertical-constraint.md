# Stage 6B-1b-d：Rank-1 SRT 垂向低频约束设计与实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**目标：** 在不修改标准 rank>=2 Sim3、不接前端或 Job Runner 的前提下，让 Rank-1 离线求解器以 SRT 相对高度作为垂向低频主约束，同时仅保留受限的 SfM 垂向高频细节。

**架构：** 水平沿程尺度、低频沿程修正和人工姿态求解保持现有语义。新增独立的垂向高低频分解：人工 solve anchors 确定 CAD 高度零点，SRT 相对高度提供低频趋势，SfM 只提供限幅后的高频细节。质量门改用 CAD 正交基分轴统计，validate anchors 继续作为完全独立的最终验收。

**技术栈：** Python 3.11、NumPy、pytest、现有 `cadscene.alignment`/`cadscene.srt`/`cadscene.sfm` 模块；不增加运行依赖。

## 全局约束

- `rel_alt` 优先；缺失时使用 `abs_alt-first_valid_abs_alt`；两者都缺失时 CLI 明确失败。
- `z_srt_target = height_offset + h_srt_relative`。
- `z_srt_low = robust_lowpass(z_srt_target)`。
- `z_sfm_high = z_sfm_base - robust_lowpass(z_sfm_base)`。
- `z_fused = z_srt_low + robust_clamp(z_sfm_high)`。
- 默认垂向窗口为 2.0 秒、最小支持数为 3、SfM 高频幅度上限为 0.5 m，全部集中在 `Rank1Config` 并可由 CLI 配置。
- SRT 空洞不跨越平滑、不在覆盖范围外外推；无效帧保留基础 SfM-CAD Z 并标记低置信。
- SRT 垂向约束只修改相机中心 Z，不修改旋转、FOV、CAD 横向位置或现有沿程算法。
- `orientation_source` 保持 `sfm_plus_manual_prior`。
- 输出声明 `vertical_source=srt_relative_altitude_primary`、`height_datum=manual_solve_anchors`、`absolute_elevation_available=false`。
- solve/validate 帧严格隔离；validate 不参与旋转、平移、高度零点、平滑或阈值估计。
- 不修改标准 Sim3、正式前端、Job Runner、点云、viewer scene 或 road pipeline。

---

## 设计

### SRT 高度统一

CLI 读取同步 CSV 时为每帧生成 `relative_height_m` 和 `height_source`。若存在有效 `rel_alt`，统一减去第一个有效 `rel_alt`；否则对 `abs_alt` 做同样相对化。不得使用经纬度推导 Up，也不得把原始绝对高度当作 CAD Z。

### CAD 正交分轴

令 `cad_up=[0,0,1]`，将变换后的轨迹方向投影到 CAD XY：

```text
along_horizontal = normalize([d_cad.x, d_cad.y, 0])
lateral = normalize(cad_up × along_horizontal)
vertical = cad_up
```

水平投影退化时拒绝。solve 平移候选分别投影到 along/lateral；Z 不再参与水平 spread。高度基准使用：

```text
height_offset_i = manual_camera_z_i - srt_relative_height_i
height_offset = median(height_offset_i)
```

输出 `solve_height_offset_range_m=ptp(offsets)` 和 `solve_height_offset_mad_m=median(abs(offsets-median(offsets)))`。默认门限：along/lateral spread 各 2.0 m，高度 offset range 3.0 m、MAD 1.5 m，全部可配置。

### 垂向融合

在每个连续有效 SRT 片段内，按时间窗口计算滑动中位数：

```text
target = height_offset + srt_relative_height
target_low = median_window(target)
sfm_low = median_window(z_sfm_base)
sfm_detail = clip(z_sfm_base-sfm_low, ±max_sfm_vertical_detail_m)
z_fused = target_low + sfm_detail
```

支持数不足、SRT 无效或位于覆盖范围外时不应用垂向修正。每帧记录 `srt_height_valid`、`srt_relative_height_m`、`vertical_correction_m`、`vertical_smoothing_support`、`sfm_vertical_detail_m`。

### 验收

独立 validate 继续输出总位置误差、水平沿程误差、横向误差、垂向误差和姿态误差。真实数据重点检查后段原约 10.6 m 的垂向误差是否显著下降，同时沿程 RMSE、横向误差、姿态误差不得恶化。

---

### Task 1：SRT 相对高度解析与失败协议

**文件：**
- 修改：`cadscene/cli/align_rank1_srt_to_cad.py`
- 测试：`tests/cli/test_align_rank1_srt_to_cad_cli.py`

**接口：**
- `SrtSample.relative_height_m: float | None`
- `SrtSample.height_source: str`
- `_load_srt_samples(path)` 在 `rel_alt`/`abs_alt` 全缺失时抛出明确错误。

- [ ] 编写失败测试：`rel_alt` 优先、`abs_alt` 相对化、全缺失时报错。
- [ ] 运行对应测试并确认因缺少新字段/行为而失败。
- [ ] 最小实现高度相对化，不改经纬度 ENU 水平转换。
- [ ] 运行聚焦测试并确认通过。
- [ ] 提交 `feat: normalize SRT relative height for rank-1 alignment`。

### Task 2：CAD 正交分轴与多 solve 高度基准

**文件：**
- 修改：`cadscene/alignment/rank1_constrained.py`
- 修改：`cadscene/alignment/rank1_validation.py`
- 测试：`tests/alignment/test_rank1_constrained.py`
- 测试：`tests/alignment/test_rank1_validation.py`

**接口：**
- `cad_track_basis(d_cad) -> (along_horizontal, lateral, cad_up)`
- `solve_rank1_transform(..., srt_relative_height_by_frame=...)`
- `Rank1Transform` 新增分轴 spread、height offset/range/MAD。

- [ ] 编写失败测试：倾斜 `d_cad` 的垂向分量不得污染 along/lateral；0.345 m offset range 得到 0.1725 m MAD。
- [ ] 运行测试确认失败。
- [ ] 实现 CAD 正交基、分轴平移中值和高度 offset 统计；移除合成三维 translation spread 一票否决。
- [ ] 更新 hold-out 分轴计算但保留总位置误差。
- [ ] 运行 alignment 聚焦测试。
- [ ] 提交 `feat: split rank-1 solve consistency by CAD axes`。

### Task 3：SRT 垂向高低频融合

**文件：**
- 修改：`cadscene/alignment/rank1_constrained.py`
- 测试：`tests/alignment/test_rank1_constrained.py`

**接口：**
- `VerticalCorrection`
- `apply_vertical_srt_constraint(base_positions, frame_times_sec, srt_relative_height_m, srt_height_valid, height_offset_m, config)`

- [ ] 编写失败测试：SRT 低频趋势主导、SfM 长期漂移被移除、SfM 高频细节被保留并限幅。
- [ ] 编写失败测试：高度跳点经中位数抑制，空洞不跨越且覆盖外不外推。
- [ ] 运行测试确认失败。
- [ ] 实现按连续有效片段和时间窗口的双滑动中位数与高频限幅。
- [ ] 运行聚焦测试并确认通过。
- [ ] 提交 `feat: constrain rank-1 vertical drift with SRT height`。

### Task 4：CLI、输出 schema 与诊断

**文件：**
- 修改：`cadscene/cli/align_rank1_srt_to_cad.py`
- 修改：`cadscene/alignment/rank1_constrained.py`
- 测试：`tests/cli/test_align_rank1_srt_to_cad_cli.py`
- 测试：`tests/integration/test_rank1_partial_srt_alignment.py`

**接口：**
- CLI 参数：`--vertical-smoothing-window-sec`、`--min-vertical-smoothing-support`、`--max-sfm-vertical-detail-m`。
- JSON meta 与逐帧字段遵循全局约束。

- [ ] 编写失败测试：CLI 无高度失败；成功产物包含 meta、逐帧垂向字段和分轴质量统计。
- [ ] 运行测试确认失败。
- [ ] 将垂向修正接入现有沿程修正之后，保持旋转四元数完全不变。
- [ ] 更新 JSON/CSV/stats/report 原子输出。
- [ ] 运行 CLI 和 integration 聚焦测试。
- [ ] 提交 `feat: expose rank-1 SRT vertical constraint in CLI`。

### Task 5：真实 smoke 与完整回归

**文件：**
- 不提交真实输入/输出；只在忽略的 `runs/` 下运行。

- [ ] 使用 solve=430/720、validate=0/1175 运行现有严格配对数据。
- [ ] 对比升级前后的 scale、沿程残差、分轴 solve 一致性、validate 总位置/横向/垂向/姿态误差。
- [ ] 检查所有 pose 无 NaN/Inf、四元数单位化、SRT 台阶未复制、旋转未改变。
- [ ] 运行 Rank-1 聚焦、CLI、integration 测试。
- [ ] 运行 `python -m pytest`、独立性检查和 `git diff --check`。
- [ ] 确认 Git 只包含代码、合成测试和本计划，不包含真实数据。
- [ ] 推送 `feature/rank1-srt-vertical-constraint`，不合并任何分支。
