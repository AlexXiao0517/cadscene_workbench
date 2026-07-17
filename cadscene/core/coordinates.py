from __future__ import annotations

from typing import Mapping, Sequence

from .camera import CameraState


def _origin(origin_xy: Sequence[float]) -> tuple[float, float]:
    if len(origin_xy) != 2:
        raise ValueError("origin_xy 必须包含两个数值。")
    return float(origin_xy[0]), float(origin_xy[1])


def _scale(cad_scale: float) -> float:
    scale = float(cad_scale)
    if scale == 0:
        raise ValueError("cad_scale 不能为 0。")
    return scale


def web_camera_to_cad_meters(
    camera: Mapping[str, float],
    origin_xy: Sequence[float],
    cad_scale: float,
) -> tuple[float, float, float]:
    """把前端 cad_world 相机位置转为 CAD meters。"""

    ox, oy = _origin(origin_xy)
    scale = _scale(cad_scale)
    return (
        (float(camera["x"]) - ox) * scale,
        (float(camera["y"]) - oy) * scale,
        float(camera["z"]) * scale,
    )


def cad_meters_to_web_camera(
    xyz_m: Sequence[float],
    origin_xy: Sequence[float],
    cad_scale: float,
) -> dict:
    """把 CAD meters 位置转回前端 cad_world。"""

    ox, oy = _origin(origin_xy)
    scale = _scale(cad_scale)
    return {
        "x": float(xyz_m[0]) / scale + ox,
        "y": float(xyz_m[1]) / scale + oy,
        "z": float(xyz_m[2]) / scale,
    }


def web_camera_to_python_state(
    camera: Mapping[str, float],
    origin_xy: Sequence[float],
    cad_scale: float,
) -> CameraState:
    """按旧 viewer 兼容规则转换相机，前端 pitch 与后端 pitch 反号。"""

    x_m, y_m, z_m = web_camera_to_cad_meters(camera, origin_xy, cad_scale)
    return CameraState(
        camera_x=x_m,
        camera_y=y_m,
        camera_z=z_m,
        yaw_deg=float(camera.get("yaw", 0.0)),
        pitch_deg=-float(camera.get("pitch", 0.0)),
        roll_deg=float(camera.get("roll", 0.0)),
        fov_deg=float(camera.get("fov", 70.0)),
        cad_scale=float(cad_scale),
    )


def python_state_to_web_camera(state: CameraState, origin_xy: Sequence[float]) -> dict:
    """把后端 CameraState 转为旧 viewer 下载/导入兼容格式。"""

    out = cad_meters_to_web_camera(
        (state.camera_x, state.camera_y, state.camera_z),
        origin_xy=origin_xy,
        cad_scale=state.cad_scale,
    )
    out.update(
        {
            "yaw": float(state.yaw_deg),
            "pitch": -float(state.pitch_deg),
            "roll": float(state.roll_deg),
            "fov": float(state.fov_deg),
        }
    )
    return out

