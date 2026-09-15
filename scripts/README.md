# scripts

本目录保存开发、验收、诊断和 Windows 打包辅助脚本，不是最终用户的主要入口。
`check_no_project_dependency.py` 用于确保新项目主线不依赖旧 `project/` 或 `cadvideo`。
正式项目从 `cadscene-workbench doctor`、`cadscene-workbench serve` 和 8300 端口的项目库开始。
只有当前文档明确要求时才直接运行脚本，并先执行对应文件的 `--help`；历史计划中的脚本名、
耗时和输出路径不构成现行契约。
