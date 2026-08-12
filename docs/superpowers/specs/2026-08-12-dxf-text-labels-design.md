# DXF 文字标注与桩号显示设计

## 目标

工作台导入 DXF 后，应完整保留模型空间中的 `TEXT`、`MTEXT` 和块引用附着的 `ATTRIB` 文字，并在 CAD 3D 视图及视频叠加视图中显示。文字需要保留原始 CAD 位置、平面旋转、图层、颜色、字高和对齐语义；大型 CAD 中拖动、旋转或缩放虚拟相机时不得因文字数量线性增加 Three.js 绘制负担。

## 范围

- 支持模型空间顶层 `TEXT` 和 `MTEXT`。
- 支持模型空间 `INSERT` 上附着的可见 `ATTRIB`；不显示 `ATTDEF` 模板文字。
- 不展开块定义中的普通 `TEXT`/`MTEXT`，也不实现复杂 CAD 字体文件、沿曲线排字或三维倾斜文字。
- 所有非空文字都保存在 `design.json`；视图层可以根据视口和缩放级别只显示可读的子集。
- 不改变现有 CAD 线、SfM、Pure Rotation、项目管理、渲染或合并流程。

## DXF 解析与数据契约

新增专用文字解析模块，由 `cadscene.cad.dxf_parser` 调用。模块把三类实体统一为现有 viewer 可读取的 `type: "text"` 实体：

```json
{
  "entity_id": "2A",
  "entity_type": "ATTRIB",
  "type": "text",
  "text": "K12+340",
  "layer": "桩号",
  "aci_color": 2,
  "color": "#ffff00",
  "world_position": [528100.0, 3375200.0, 0.0],
  "world_points": [[528100.0, 3375200.0, 0.0]],
  "cad_rotation": 35.0,
  "cad_height": 2.5,
  "text_lines": 1,
  "horizontal_align": "center",
  "vertical_align": "middle",
  "text_role": "station",
  "attribute_tag": "STA",
  "block_name": "STATION_MARK"
}
```

`TEXT` 和 `ATTRIB` 使用 `get_placement()` 取得真实对齐点，并通过实体 OCS 转到 WCS。旋转由 OCS 中的文字方向转换为 WCS XY 方向后计算，避免只复制 `dxf.rotation` 导致非默认 extrusion 下方向错误。`MTEXT` 使用 WCS 插入点、`ucs().ux` 文字方向、`char_height`、attachment point 和去格式化后的多行纯文本。附着属性的位置已包含块插入的平移、旋转和缩放；当属性使用 layer 0 或 BYBLOCK 颜色时，继承父 `INSERT` 的图层或颜色。

桩号识别只影响显示优先级，不改变内容。文本匹配常见 `K12+340`、`ZK12+340.5`、`YK...` 格式，或位于名称含“桩号 / station / chainage”的图层时，标记为 `text_role: "station"`；其余为 `annotation`。

文字位置参与整体 bbox，从而允许仅含文字的合法 DXF 完成导入。统计新增 `text_count` 和按 `TEXT`、`MTEXT`、`ATTRIB` 分类的 `text_entity_types`；线段统计仍只描述几何线段。

## 浏览器显示与性能

新增无 DOM、可由 Node 直接测试的 `apps/web_camera_viewer/cad_text.js`，负责：

- 规范化文字实体和对齐值；
- 计算文字优先级；
- 对投影到屏幕的候选执行视口裁剪；
- 用屏幕网格保留每个单元中优先级最高的文字；
- 执行活动文字硬上限。

`viewer_legacy.js` 不在场景创建时为全部文字创建资源。它保存完整文字索引，并在观察相机变化时以节流方式重新选择当前标注：

- 只处理投影在视口附近的文字；
- 桩号、高字级和靠近视口中心的文字优先；
- 屏幕网格防止文字互相覆盖；
- 3D 同时活动文字默认最多 240 条；
- 选择更新最多每 120 ms 一次，拖动过程中不会逐事件重建全部资源；
- 共用一个单位 `PlaneGeometry`，仅为活动文字创建材质和 CanvasTexture；
- 使用有界 LRU 纹理缓存，默认最多保留 384 个纹理；离开缓存的纹理显式 `dispose()`。

文字平面保持在 CAD XY 平面上，位置经现有 `worldToScene()` 映射，宽高以 CAD 字高和文本行数计算，旋转沿用 CAD 平面方向。水平和垂直对齐通过平面中心相对插入点的局部偏移实现。Canvas 使用黑色描边与实体/图层颜色填充，以兼顾深色 3D 背景和亮色视频画面。

视频叠加视图复用相同的屏幕选择规则，最多绘制 240 条可见文字；播放中的既有 200 ms 重绘节流保持不变。隐藏标注按钮同时控制 3D 与视频叠加文字。

## 错误处理与兼容性

- 单条文字损坏、空文本或无法求 placement 时跳过该实体并记录统计，不阻断其他 CAD 几何导入。
- 不可见 ATTRIB 不显示。
- 未包含文字的旧 `design.json` 行为不变。
- 现有 `load_cad_bundle()` 继续忽略少于两个点的实体，因此文字不会进入道路中心线、对齐或渲染线段计算。
- 前端缺少 `cad_text.js` API 时应明确初始化失败，避免静默退回无上限的旧文字路径。

## 测试策略

严格按 TDD 实施：

1. 后端测试先构造含旋转/对齐 TEXT、多行 MTEXT、旋转缩放块 ATTRIB、图层与颜色继承的 DXF，并验证测试因文字缺失而失败。
2. 实现最小解析逻辑后验证实体契约、WCS 位置、旋转、字高、对齐、文本内容和统计。
3. Node 单元测试先定义视口裁剪、桩号优先、屏幕网格和 240 条硬上限，再实现 `cad_text.js`。
4. viewer 静态契约测试验证脚本加载顺序、惰性文字资源、有界缓存、节流更新、共享几何和显示开关。
5. 运行 CAD/viewer/工作流相关测试、JavaScript 语法检查和完整 pytest。

## 验收标准

- 导入包含 TEXT、MTEXT 和块属性的 DXF 后，`design.json` 完整包含非空可见文字。
- 工作台 CAD 3D 视图能看到桩号和其他标注，位置、旋转、图层颜色、字高和对齐符合 DXF 语义。
- 视频叠加视图也能显示相同文字并受同一开关控制。
- 任意时刻活动 3D 文字对象不超过 240，纹理缓存不超过 384。
- 虚拟相机交互期间文字更新被节流，不因 DXF 总文字数直接创建等量 Three.js 对象。
- 现有完整测试继续通过。
