# SfM CUDA 后端

Stage 5A 为 `run_sfm` 增加官方 COLMAP CLI 后端，同时保留 pycolmap。两种后端都输出相同的：

- `camera_trajectory.json`
- `sparse_points.ply`
- `camera_intrinsics.json`
- `sfm_stats.json`
- `sfm_report.md`

后续 alignment、quality、viewer 和 render 无需区分 SfM 后端。

## 环境检查

```powershell
python -m cadscene.cli.check_sfm_environment --json
```

检测结果会分别说明 GPU 硬件、pycolmap CUDA build、官方 COLMAP CLI 和 CLI CUDA 参数是否可用。仅检测到 NVIDIA GPU 不代表 COLMAP 已支持 CUDA。

## 自动选择

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
  --device auto `
  --gpu-index 0 `
  --no-mask
```

`auto` 优先顺序为：已确认 CUDA 的官方 COLMAP CLI、已确认 CUDA 的 pycolmap、官方 COLMAP CLI CPU、pycolmap CPU。CPU 回退会写入 `sfm_stats.json`、报告和工作流状态。

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

也可以设置环境变量 `COLMAP_EXE`。命令以参数列表启动且 `shell=False`；Windows `.bat` 通过受控的 `cmd.exe /d /s /c` 入口执行。

若不允许 CPU 回退，增加 `--no-cpu-fallback`。GPU 状态不会仅根据 `use_gpu=1` 判定，最终以 COLMAP/pycolmap 能力与运行日志共同确认。

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

输出到 `reports/sfm_backend_benchmark.json` 和 `reports/sfm_backend_benchmark.md`。报告比较耗时、注册率、稀疏点数、重投影误差和实际 GPU 状态，并标记明显质量退化。

