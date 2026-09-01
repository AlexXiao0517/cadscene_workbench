# 上传工作流与轨迹路由

正式项目上传门户会先保存并检查输入，再由视频分析生成片段、运动模式和保守的工作流
推荐。激活分析 revision 后，推荐值成为片段的默认最终工作流；项目片段管理页允许人工
覆盖。路线选择不会替代人工关键帧、SfM-CAD 对齐或质量检查，也不会把普通 SRT 元数据
当作精确位置、姿态或 CAD 高程真值。

## 入口与输入

启动本地服务后，从门户创建项目：

```text
http://127.0.0.1:8300/apps/workflow_portal/index.html
```

- **视频**：必传；正式项目上传界面仅支持 `.mp4`。
- **CAD**：必传；正式项目上传界面仅支持 `.dxf`。只有图纸解析为可用 assets 后，
  项目分析才能发布。
- **SRT 遥测**：选传。上传后先做解析和能力检测；只有完整 DJI 云台姿态路线可在完成坐标/FOV 确认后直接执行，不上传不影响稳定的无 SRT 流程。

门户先创建 Project API 项目并并行上传视频、CAD 和可选 SRT，再启动 CAD/video 分析，
等待候选 analysis revision 完成并激活，最后进入项目片段管理。上传页没有纯旋转模式
复选框，也不会在分析前要求用户声明运动类型。

Project API 的底层上传校验器和兼容 Workflow API 仍能接收一些额外扩展名，用于迁移、
历史数据或维护调用；这些入口未被正式上传界面开放，也没有形成当前用户流程的格式
承诺。日常项目准备应只使用 MP4、DXF 和可选 SRT。

## 当前路由状态

| 路由或能力 | 触发条件 | 状态 | 当前行为 |
| --- | --- | --- | --- |
| `sfm_only` | 未上传/无法解析 SRT 或 SRT 覆盖不足，且不是已验证纯旋转；或人工覆盖为 SfM | **Stable** | 当前唯一稳定的 JobRunner 端到端路径：SfM、人工关键帧、路线拟合、质量和渲染。 |
| `pure_rotation` | 无 SRT 路由优先级，且自动分析验证完整短视频具有持续、无矛盾的强旋转证据；也可在项目页人工覆盖 | **Supported** | 运行固定版本外部 OpenGV 旋转恢复，再人工全局放置、局部姿态校正并渲染；固定相机中心，不恢复平移或尺度。自动推荐精度仍需人工复核。 |
| partial-SRT core | 独立命令行使用 | **Experimental CLI** | 可做 PTS 时间同步、局部 ENU、稳健 Sim3 和融合辅助；尚未接入正式 JobRunner。 |
| `srt_sfm_fused` portal route | SRT 有足够 GPS 与高度，未满足完整相机姿态 | **Interface only** | 上传、分析和提示可用；HTTP 服务拒绝启动正式工作流阶段。 |
| `srt_full_pose` | SRT 有足够 GPS、高度与完整云台相机姿态，并已确认当前 CAD 投影与水平 FOV | **Supported with guard** | 按精确 source PTS 把 WGS84 位置投影到已确认的 CGCS2000/CAD 本地米制坐标，直接生成相机轨迹；跳过 SfM 和稀疏点云。 |

SRT 检测异常会回退 `sfm_only`，并把解析警告（例如时长不匹配）保留在分析报告中。
字段和阈值见 [SRT 能力检测](srt_capability_detection.md)。

当前自动纯旋转验证是有意保守的：只对单一逻辑片段且不足 60 秒的完整源视频评估，要求
至少三个旋转窗口、旋转窗口占比不低于 60%、没有通用运动/未知窗口，并满足高置信度、
单应内点率和残差门槛。不满足时推荐 `sfm_only` 并标记需要复核，而不是要求用户回到
上传页重新选择模式。项目页的“最终工作流”选择器用于纠正推荐，不是触发自动检测的
前提。

项目页保留 `sfm_only`、`pure_rotation`、`srt_sfm_fused` 与 `srt_full_pose` 的真实模式，
不再把全姿态片段回显为 SfM。`srt_sfm_fused` 仍只显示能力提示；`srt_full_pose` 会显示
当前 CAD 指纹绑定的候选投影、CAD 包围盒与投影轨迹预览。用户必须显式确认一个候选，
并为每个片段保存单一的 `horizontal_fov_deg`；配置 revision 或 CAD 版本改变后，旧确认
和旧轨迹会 stale，不能继续发布。

## Full-pose 坐标与相机约束

- 坐标候选按当前 CAD 数值范围与 SRT 投影后落图证据生成；经度接近 120°时可推荐
  EPSG:4549（3°带中央经线 120°），但 120°只属于当前项目确认，不是通用默认。
- FOV 始终指水平视场角，只保存一个数值；首版不提供水平/垂直类型选择，也不支持
  逐帧变焦或鱼眼内参。
- 姿态固定采用 DJI absolute-NED 云台 yaw/pitch/roll 约定，不运行自由姿态/尺度反算。
- 轨迹进入 CAD 本地米制坐标后标记 `metric_scale_locked=true`；对齐保持单位旋转和
  `scale=1.0`，人工锚点只允许估计固定 XYZ 平移和 yaw/pitch/roll 零偏。
- 水平坐标确认不等于高程基准确认。优先使用相对高度并叠加用户 `cad_z_offset_m`；
  绝对高度未经测量基准验证时只产生警告；诊断分别输出 `horizontal_validation`
  与 `vertical_validation`，不能用水平投影置信度替代高程结论。
- full-pose viewer scene 允许空点云，并跳过依赖稀疏几何的 road-surface 分析。

## 稳定路径中的关键帧

对 `sfm_only`，先完成至少两个已确认的人工关键帧并运行初步路线拟合。随后使用
“生成关键帧计划”按间隔创建待标定帧；待标定帧不会自动成为人工锚点。逐帧保存计划
中的人工标定后，选择“完成关键帧标定”进行最终路线拟合，才可进入质量检测。

如果重建内参或几何不可靠，已确认且彼此一致的人工关键帧 FOV 优先于重建 FOV。FOV
是人工核对的一部分，不是 SRT 或 SfM 自动结果的精度承诺。

## 存储与恢复

`--root` 提供静态页面，`--storage-root` 提供正式项目的 `projects/` 以及兼容流程的
`data/`、`runs/`。Project API 的恢复权威是 `<storage-root>/projects/<project_id>/`
中的五类 manifest、不可变输出和作业尝试。`data/` 与 `runs/` 只属于兼容 dataset/run
工作流或被项目 adapter 引用的历史工件；它们不是新项目状态的替代品。`srt_full_pose`
发布轨迹、坐标诊断、相机路径和报告；`srt_sfm_fused` 这类 Interface-only 路线不能
作为已执行且可恢复的正式任务。
