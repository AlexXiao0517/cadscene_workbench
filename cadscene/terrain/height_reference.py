"""Pair SRT absolute camera heights with terrain, without inventing a datum shift."""

from dataclasses import replace
from math import isfinite
from typing import Sequence

import numpy as np

from .context import TerrainContext
from .tpkg import TerrainControlSet, sampled_control_points


def select_height_reference(
    context: TerrainContext,
    controls: TerrainControlSet | None,
    absolute_heights: Sequence[float | None],
) -> TerrainContext:
    if controls is None or context.mode == "relative":
        return replace(context, mode="relative", cad_fallback_ground_m=0.0)
    if not absolute_heights or not all(
        value is not None and isfinite(float(value)) for value in absolute_heights
    ):
        return replace(
            context, mode="relative", cad_fallback_ground_m=0.0,
            camera_height_datum_valid=False,
            camera_height_datum_source="relative_fallback_missing_abs_alt",
            warnings=(*context.warnings,
                "SRT 绝对高度缺失或无效：整条路线回退相对高度，CAD 使用 Z=0；未混用 TPKG 高程。"),
        )
    samples, _ = sampled_control_points(controls, step_m=2.0)
    if not len(samples):
        return replace(context, mode="relative", cad_fallback_ground_m=0.0)
    return replace(
        context, reference_ground_m=None,
        cad_fallback_ground_m=float(np.median(samples[:, 2])),
        camera_height_datum_valid=False,
        camera_height_datum_source="srt_abs_alt_unverified",
        warnings=(*context.warnings,
            "沿用已验证实验策略：相机使用 SRT abs_alt，CAD 使用 TPKG 原始高程；"
            "未施加自动高程偏移或椭球高转换。两者垂直基准一致性未经测量确认，可继续微调。"),
    )
