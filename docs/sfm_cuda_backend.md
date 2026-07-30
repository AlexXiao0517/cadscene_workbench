# SfM CUDA 后端

SfM CUDA 是 **Optional（可选）** 加速，不是默认工作流。正式工作流默认使用
`pycolmap + cpu`；只有在用户显式选择 CUDA 且环境检查确认所选后端支持时，才会
尝试 GPU。CUDA 的已确认边界是特征提取和匹配；建图、mapper 和全局束调整（global
BA）不应宣传为 GPU 处理。

无论使用哪种 SfM 后端，都会产生相同的主要产物：

- `camera_trajectory.json`
- `sparse_points.ply`
- `camera_intrinsics.json`
- `sfm_stats.json`
- `sfm_report.md`

后续 alignment、quality、viewer 和 render 不需要按 SfM 后端分支。

## 环境检查

```powershell
python -m cadscene.cli.check_sfm_environment --json
```

检查会分别报告 GPU 硬件、pycolmap CUDA build、官方 COLMAP CLI 和 CLI CUDA
参数。仅发现 NVIDIA GPU，或仅看到 `use_gpu=1`，都不能证明一次任务实际使用了
CUDA。

## 默认与显式选择

工作台的 SfM JobRunner 默认传递 `--backend pycolmap --device cpu`。要尝试 CUDA，
可在高级设置中显式选择，或用命令行指定后端和设备：

```powershell
python -m cadscene.cli.run_sfm `
  --dataset hygs_1min `
  --run-id sfm_cuda_smoke `
  --output-root runs `
  --video data/hygs_1min/hygs_1min.mp4 `
  --start-frame 0 `
  --num-frames 251 `
  --frame-step 5 `
  --backend auto `
  --device cuda `
  --gpu-index 0 `
  --no-mask
```

`auto` 只会在能力可确认时选择 CUDA 后端；否则会使用可用 CPU 后端。默认允许 CPU
回退，并把原因写入 `sfm_stats.json`、SfM 报告和工作流状态。若任务必须拒绝 CPU
回退，增加 `--no-cpu-fallback`。这会把无法确认 CUDA 的情况变成明确失败，而不是
让任务悄然改用 CPU。

## 指定官方 COLMAP

```powershell
python -m cadscene.cli.run_sfm `
  --dataset hygs_1min `
  --run-id sfm_colmap_cuda `
  --output-root runs `
  --video data/hygs_1min/hygs_1min.mp4 `
  --backend colmap_cli `
  --device cuda `
  --gpu-index 0 `
  --colmap-exe "C:\Program Files\COLMAP\COLMAP.bat" `
  --no-mask
```

也可以设置环境变量 `COLMAP_EXE`。命令以参数列表启动且 `shell=False`；Windows
`.bat` 通过受控的 `cmd.exe /d /s /c` 入口执行。

## 基准测试

```powershell
python -m cadscene.cli.benchmark_sfm_backends `
  --dataset hygs_1min `
  --run-id-prefix sfm_backend_bench `
  --output-root runs `
  --video data/hygs_1min/hygs_1min.mp4 `
  --start-frame 0 `
  --num-frames 251 `
  --frame-step 5 `
  --gpu-index 0 `
  --colmap-exe "C:\Program Files\COLMAP\COLMAP.bat"
```

报告写入 `reports/sfm_backend_benchmark.json` 和
`reports/sfm_backend_benchmark.md`，比较耗时、注册率、稀疏点数、重投影误差和实际
GPU 状态，并标记明显质量退化。请把一次基准结果视为该环境的观察，而不是所有数据
或设备上的性能承诺。
