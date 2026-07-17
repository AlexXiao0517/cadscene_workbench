# 需要用户确认的问题

以下问题用于第二阶段创建骨架前确认。第一阶段只生成文档，不开始迁移。

1. 新项目目录名是否确定为 `cadscene_workbench`？

2. 新 Python 包名是否确定为 `cadscene`？

3. 是否完全不保留 `cadvideo` wrapper？

   建议：不保留。否则删除 `project/` 后容易继续混入旧 import。

4. 是否确认用 `runs/` 完全替代旧 `out/`？

   建议：确认。旧 `out/` 只作为历史文档路径，不作为新项目输出。

5. `web_camera_viewer` 是否确认放在 `apps/web_camera_viewer/`？

   建议：确认。它是产品应用层，不应混在 Python package 中。

6. old tracker 是否全部放入 `legacy/old_trackers/`，不进入主线？

   包括 ROI-LK-PnP、visible-window、multi-region、rescue fallback。

7. old `video_path` 是否整体作为 legacy，不再作为主线 camera path？

   建议：确认。只抽取 keyframe/source 过滤和 centerline 工具。

8. 是否以 `hygs_1min + 240f keyframe ablation` 作为 `v0.1-sfm-workbench` demo？

   建议：可以作为闭环演示，但文档需注明 240f 是激进方案，不是质量最优默认密度。

9. 是否先不迁移 `semantic_refine`？

   建议：先不迁移。旧文档记录 hygs_1min 上变差、全程漂移。

10. 是否先不做 CAD-on-tilted-plane / CAD 高程建模？

    建议：先不做自动建模，只保留 road surface diagnostics 只读报告。

11. 第一版 `run_pipeline` 是否只串联已经稳定的 SfM 主线？

    建议：是。即 segmentation、sfm、alignment、quality、viewer_scene、road_surface、keyframe_ablation、render。

12. 是否需要在第二阶段骨架中加入 `scripts/check_no_project_dependency.py`？

    建议：必须加入，并纳入 pytest 或 CI。

13. 是否允许 `legacy/` 中保留旧代码副本，还是只保留 README 和迁移说明？

    建议：第一版只保留 README；如需旧代码副本，必须确保不被主线 import。

14. CAD loader 第一版是否要求从原始 DXF/DWG 重新导出 road JSON？

    建议：v0.1 先消费 dataset config 指向的 CAD assets；原始 CAD 导出作为后续增强。

15. 是否需要兼容旧 viewer 下载出的 track JSON 格式？

    建议：需要。关键字段和 `algorithm_prediction` 语义必须兼容。

16. 是否要求所有报告默认中文？

    建议：确认。Markdown 报告中文，代码标识和字段名英文。

17. 是否要求所有 CSV 都使用 `utf-8-sig`？

    建议：确认，并通过单测检查 BOM。

18. 第一版真实 hygs smoke 命令是否只提供命令，不在智能体环境中完整运行？

    建议：确认。避免因缺少 `out/` 或重依赖环境阻塞第一阶段和骨架阶段。

