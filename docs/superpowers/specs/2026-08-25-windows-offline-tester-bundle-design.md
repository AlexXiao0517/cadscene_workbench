# Windows 离线测试包设计

## 目标

为非程序员测试人员提供一个 Windows x64 离线 ZIP。解压后双击即可启动 CADScene Workbench，不要求预装 Python、Conda 或 FFmpeg，也不在首次启动时下载依赖。

## 交付结构

```text
CADSceneWorkbench-0.1.0-win64-offline/
  启动CAD视频工作台.cmd
  关闭CAD视频工作台.cmd
  使用说明.txt
  runtime/                  # conda-pack 生成的独立 Python 环境
  pure_rotation_backend/    # 固定版本后端、OpenGV CLI 和版本元数据
  launcher/                 # 启停 PowerShell 脚本与发布清单
  workspace/                # 用户项目、data、runs 的唯一可写根目录
  logs/                     # 启动与服务日志
```

应用 wheel、网页静态资源和配置安装在 `runtime` 中。包内不包含现有项目、`dji` 数据源、测试、Git 元数据、历史日志或算法调试输出。纯旋转后端仅复制运行所需的 `src`、入口脚本、固定 OpenGV 可执行文件和 `backend_version.json`。

## 启停行为

启动脚本以自身目录为根，不依赖当前工作目录。首次运行执行 `conda-unpack`，之后由标记文件跳过。每次启动先运行 `doctor`，再从 8300 起选择空闲端口，以隐藏窗口启动单个服务，等待项目库 HTTP 可用后打开浏览器。服务只监听 `127.0.0.1`，所有数据写入 `workspace`。

状态文件记录 PID、端口、Python 路径和 storage root。关闭脚本只有在 PID 对应命令行同时匹配包内 Python、CADScene 服务入口和 storage root 时才终止进程，避免误杀其他 Python。

## 构建与验证

构建脚本从干净的专用 Conda 环境生成 runtime，安装当前分支 wheel 后执行 `pip check` 和 `doctor`，再使用 ZIP64 创建交付文件。构建阶段对打包路径使用白名单和显式排除规则；发现 `projects`、`data`、`runs`、`.git`、测试目录或日志即失败。

验收包括：打包规则单元测试、wheel/静态资源测试、完整回归、解压到包含空格和中文的路径、离线 doctor、启动项目库、创建空项目存储、关闭并再次启动。场景桥接功能不在本包基线中。

