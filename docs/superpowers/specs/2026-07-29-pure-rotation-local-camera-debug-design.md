# Pure-Rotation Local-Camera Debug Design

## Goal

Make the Pure-Rotation workflow read as recovery followed by camera debugging, while preserving the distinction between OpenGV relative orientation, manual CAD anchoring, world-up azimuth adjustment, and camera-local pose correction.

## Workflow

- The Pure-Rotation step bar is `上传数据 → 旋转轨迹恢复 → 调试 → 渲染导出`.
- Only the numeric step badge is circular.
- A successful Pure-Rotation recovery automatically advances to `调试`.
- Recovery displays Raw orientation only.

## Camera conventions

`R_base` is `rotation_cad_from_camera`; its columns are camera right, camera down, and camera forward expressed in CAD world coordinates.

- Translation Gizmo uses world space.
- Pose Gizmo uses camera-local space.
- Local pitch rotates about camera right `+X`.
- Local yaw rotates about camera up `-Y`.
- Local roll rotates about camera forward `+Z`.
- Local correction is composed on the right:

  `R_final = R_base @ DeltaR_local`

- World vertical adjustment remains a separate left composition:

  `R_final = R_world_z @ R_base`

- Matrix/quaternion representations are authoritative. Euler values are display/input deltas only.
- OpenGV relative orientation must not be presented as an observable absolute CAD orientation. Entering debug establishes or restores a manual anchor at the current PTS.

## Debug controls

- Debug opens in fixed-camera initial setup with world-space translation enabled.
- Rename `全局固定相机放置` to `固定相机初始设置`.
- Saving or restoring the fixed-camera setup never changes edit mode.
- Pose-debug interaction automatically switches to local rotation; remove the `使用旋转 Gizmo` button.
- Move add/update, delete, previous, next, and undo correction actions to the frame control strip.
- Hide suggestion actions in Pure-Rotation only; retain them for existing SfM workflows.

## Persistence and interpolation

- Save authoritative manual rotation matrices rather than reconstructing them from world Euler display values.
- Correction residuals are local:

  `DeltaR_local(k) = R_base(k).T @ R_manual(k)`

- Interpolate local residual quaternions with hemisphere-safe SLERP within each segment.
- Apply interpolated residuals on the right:

  `R_final(t) = R_base(t) @ DeltaR_local_interp(t)`

- Camera center remains fixed; no translation is estimated.

## Tests

- Static UI contracts cover labels, single-circle step styling, control placement, hidden suggestions, and mode switching.
- JavaScript math tests cover local yaw/pitch/roll axes, world-up composition, SO(3), and fixed center.
- Python correction tests cover local residual endpoints and SLERP.
- Workflow tests cover automatic recovery-to-debug transition and legacy workflow isolation.
