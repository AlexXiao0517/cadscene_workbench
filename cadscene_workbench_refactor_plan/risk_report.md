# 风险报告

本文档列出从旧 `project/` 重构到新 `cadscene_workbench/` 的主要风险和缓解策略。

## 坐标系混乱

风险：

- SfM 世界、CAD meters、前端 `cad_world`、旧 Python `CameraState` 混用。
- 同一字段名 `x/y/z/pitch` 在不同空间含义不同。

缓解：

- 所有转换集中到 `cadscene/core/coordinates.py`。
- 类型名中显式区分 `CadMetersPose`、`WebCameraPose`、`SfmPose`。
- 测试覆盖 roundtrip 和 pitch sign。

## cad_scale / origin_xy 误用

风险：

- 旧代码多处手写 `(x-origin)*scale`。
- 新模块如果各自处理，会出现缩放和原点不一致。

缓解：

- `cad_scale` 和 `origin_xy` 只从 dataset config 进入。
- 禁止在业务模块中直接重复转换公式。
- Artifact manifest 记录本次 run 的 resolved `cad_scale` / `origin_xy`。

## pitch 前端/后端反号

风险：

- 旧代码中 web pitch 与 python pitch 明确反号。
- viewer、alignment、diagnostics 任一处忘记反号都会导致视觉和报告相反。

缓解：

- `core/coordinates.py` 提供唯一转换函数。
- `tests/core/test_coordinates.py`、`tests/viewer/test_export_scene.py`、`tests/diagnostics/test_pose_residual.py` 都覆盖。

## SfM 世界 / CAD 米 / 前端 cad_world 混淆

风险：

- 点云应从 SfM 世界经 global sim3 到 CAD meters，再转前端 cad_world。
- anchored camera path 已是 CAD meters + python pitch，不能再套 sim3。

缓解：

- viewer scene schema 标注字段坐标空间。
- `viewer/export_scene.py` 明确只对点云和 global track 用 global sim3。
- anchored path 从 `sfm_camera_path.csv` 读取后只做前端转换。

## 点云 global sim3 和 anchored path 混用

风险：

- 旧文档明确：静态点云只用 global sim3，不能套分段锚定 correction。
- 如果将 anchored correction 用于点云，会伪造几何并误导 road diagnostics。

缓解：

- `pointcloud.py` 不依赖 `AnchoredAlign`。
- viewer scene 和 road diagnostics 测试验证点云只用 global sim3。

## output layout 变化导致旧脚本失效

风险：

- 旧 CLI 默认写 `out/`，新项目写 `runs/`。
- 旧 docs 中大量 `out/hygs...` 命令不能直接照搬。

缓解：

- 第一版文档明确 `out/` 仅为旧项目历史路径。
- 新 CLI 使用 `--output-root runs`。
- 提供 smoke 命令模板和迁移后的 run path。

## base / difusser 环境混淆

风险：

- segmentation/SfM 重阶段依赖 torch/transformers/pycolmap。
- alignment/quality/viewer/diagnostics 轻阶段只需 numpy/opencv/matplotlib。
- 用户可能用 base 跑 segmentation，或用 difusser 跑轻阶段但依赖不一致。

缓解：

- CLI `--help` 和 README 标注环境要求。
- 重依赖惰性导入，导入 `cadscene` 不要求 torch/pycolmap。
- manifest 记录 `python_executable` 和关键包版本。

## 前端 URL 相对路径错误

风险：

- 旧 viewer 依赖 `../data`、`../out`。
- 新结构使用 `apps/web_camera_viewer/` 和 `runs/`，相对路径层级变化。

缓解：

- `paths.js` 改为 runs-aware。
- `serve_viewer` 从项目根提供静态服务。
- 文档明确不要用 `file://`。
- CLI 输出 viewer URL 时使用相对项目根路径。

## 删除 project/ 后新项目仍引用旧路径

风险：

- 旧 `sfm_align` 反向依赖 `pipeline.pose_align_viewer` 和 `video_path`。
- 直接复制会留下 `from cadvideo...`。

缓解：

- 第一批先迁 `core/`、`cad/`、`sfm/trajectory.py`、`alignment/keyframes.py`。
- 增加 `scripts/check_no_project_dependency.py`。
- CI 或 pytest 必跑独立性检查。

## 大文件被误复制进新项目

风险：

- 视频、PLY、旧 out/archive 产物可能很大。
- 复制到 repo 会导致项目臃肿。

缓解：

- `runs/` 默认不入库。
- `00_inputs/` 只记录引用和 hash，不复制大文件。
- `.gitignore` 忽略 videos、PLY、mp4、runs。

## 测试依赖真实视频导致不可跑

风险：

- 真实 hygs 数据大且环境依赖重。
- 如果核心测试依赖真实视频，后续开发速度会变慢。

缓解：

- 核心算法使用合成 trajectory、PLY、CSV、JSON。
- 真实 hygs 只作为 smoke 命令，由用户手动跑。
- `run_pipeline --dry-run` 必须可在无真实大视频时通过。

## 240f demo 被误解为默认质量最优

风险：

- 旧文档记录 240f 工作量减少大，但风险高，建议帧更多。
- 若把 240f 包装为默认最优，会误导产品定位。

缓解：

- v0.1 demo 可以用 `hygs_1min + 240f` 展示闭环能力。
- 文档明确 240f 是激进演示，120f 更适合候选初始密度。

## legacy 污染主线

风险：

- 为快速迁移而保留旧函数名、旧 import、旧 tracker helper。
- 主线会再次臃肿。

缓解：

- legacy 目录不被主线 import。
- 主线模块职责单一。
- 每个 legacy 目录 README 写明替代主线和不建议继续使用。

## CSV 编码不一致

风险：

- 旧 tests 已覆盖 `utf-8-sig`。
- 中文 CSV 若不用 BOM，Excel 打开可能乱码。

缓解：

- `core/io.py` 统一 `write_csv_utf8_sig`。
- 所有 CSV 单测检查 BOM。

