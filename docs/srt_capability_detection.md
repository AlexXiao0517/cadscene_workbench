# SRT 能力检测

SRT 是可选输入。系统解析 DJI 风格字幕中的位置、高度和姿态字段，以保守地选择
工作流；**检测标签本身不是高精度轨迹、相机姿态或 CAD 高程真值**。完整姿态和固定
轨迹分支都只有在确认当前 CAD 投影与水平 FOV 后才可执行。

## 识别字段

- 位置：`latitude`、`longitude`（兼容 `lat`、`lon`、`lng` 和常见别名）。
- 高度：`altitude`、`height`、`relalt`、`absalt` 等别名。
- 相机姿态：`gimbal_yaw`、`gimbal_pitch`、`gimbal_roll`，也识别 `GBYaw`、
  `CameraYaw` 等别名。
- 飞行器姿态：`drone_*` 或 `aircraft_*`。它不是云台/相机姿态，不能单独判定完整
  相机姿态。

## 保守判定规则

检测只接受有限的、可转换为数值的记录，且至少需要两条记录。位置经纬度和高度各自
的有效覆盖率都必须达到 **80%**，才会识别为 `srt_fixed_track_visual_pose`。GPS 或高度
不足、SRT 无法解析或没有有效记录时，系统回退 `sfm_only` 并写入警告。

`srt_full_pose` 的门槛更高：除轨迹条件外，云台 yaw/pitch/roll 各自覆盖率和 GPS、
高度、三个云台角共同出现的覆盖率都必须达到 **80%**。仅有飞行器姿态、姿态字段
零散出现或来源不明时，最多识别为固定轨迹 + 视觉姿态，不会提升为完整姿态。

若提供视频时长，检测会对比 SRT 末尾时间；偏差超过 1 秒或视频时长的 10%（取较大
者）会产生警告，但不会据此升级或降级一个已满足字段门槛的分支。

## 输出、状态与边界

结果保存为 `srt_analysis.json` 和 `srt_analysis_report.md`，包含检测模式、字段覆盖率、
完整姿态共同覆盖率、云台/飞行器姿态来源和警告。结果只说明“元数据字段足以进入哪种
接口分支”，不说明坐标系、时间同步、测量精度、云台安装误差或与 CAD 的一致性。

当前状态如下：

| 能力 | 状态 | 边界 |
| --- | --- | --- |
| `sfm_only` | **Stable** | 唯一稳定的端到端正式工作流。 |
| partial-SRT core | **Experimental CLI** | 历史 ENU/稳健 Sim3 融合核心仍可独立使用，但不是新项目路线。 |
| `srt_fixed_track_visual_pose` | **Supported with guard** | GPS、`rel_alt`、精确 frame map、当前 CAD 投影和水平 FOV 均确认后，锁定 SRT→CAD 位置并仅从视频估计姿态。 |
| `srt_sfm_fused` | **Legacy read-only** | 新项目不再推荐或创建，只读取历史 manifest/产物。 |
| `srt_full_pose` | **Supported with guard** | 完整云台姿态、精确 frame map、当前 CAD 投影和水平 FOV 均确认后，可直接生成尺度锁定的 CAD 米制轨迹并跳过 SfM。 |

固定轨迹路线不运行旧的 SRT+SfM 融合。它按精确 source PTS 采样 SRT，经已确认的
CGCS2000 参数进入 CAD local metres，Z 使用 `rel_alt`；视觉算法只估计姿态。视觉失败
不会把位置改成 SfM 结果，缺姿态帧仍保留可见位置轨迹。

全姿态执行前还会检查候选投影的轨迹落图比例。中央经线（例如当前项目可能推荐的
120°）是绑定 CAD 指纹的显式确认项，不能跨图纸复用。FOV 只接受用户输入的单一水平
角度。相对高度通过 `cad_z_offset_m` 接到 CAD 高程；绝对高度缺少共同测量基准时不会
自动宣称为 CAD Z 真值。
