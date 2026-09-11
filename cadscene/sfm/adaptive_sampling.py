from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class AdaptiveFramePlan:
    selected_ordinals: tuple[int, ...]
    reasons: dict[int, tuple[str, ...]]


def select_adaptive_ordinals(
    positions: np.ndarray,
    rotations: np.ndarray,
    sharpness: np.ndarray,
    *,
    fast_translation_m: float = 18.0,
    attitude_change_deg: float = 0.75,
    route_turn_deg: float = 5.0,
    blur_quantile: float = 0.1,
    sharpness_rescue_ratio: float = 1.5,
) -> AdaptiveFramePlan:
    positions = np.asarray(positions, dtype=np.float64)
    rotations = np.asarray(rotations, dtype=np.float64)
    sharpness = np.asarray(sharpness, dtype=np.float64)
    count = len(positions)
    if count < 3 or positions.shape != (count, 3):
        raise ValueError("positions must have shape (N, 3) with N >= 3")
    if rotations.shape != (count, 3, 3):
        raise ValueError("rotations must have shape (N, 3, 3)")
    if sharpness.shape != (count,) or not np.isfinite(sharpness).all():
        raise ValueError("sharpness must contain one finite value per candidate")

    selected = set(range(0, count, 2))
    selected.add(count - 1)
    reason_sets = {ordinal: {"base"} for ordinal in selected}
    reason_sets[0] = {"endpoint"}
    reason_sets[count - 1] = {"endpoint"}
    for left in range(0, count - 2, 2):
        middle, right = left + 1, left + 2
        reasons = []
        if float(np.linalg.norm(positions[right] - positions[left])) >= fast_translation_m:
            reasons.append("fast_translation")
        relative = rotations[left].T @ rotations[right]
        cosine = float(np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0))
        if float(np.degrees(np.arccos(cosine))) >= attitude_change_deg:
            reasons.append("attitude_change")
        incoming = positions[middle, :2] - positions[left, :2]
        outgoing = positions[right, :2] - positions[middle, :2]
        denominator = float(np.linalg.norm(incoming) * np.linalg.norm(outgoing))
        if denominator > 1e-9:
            cosine = float(np.clip(np.dot(incoming, outgoing) / denominator, -1.0, 1.0))
            if float(np.degrees(np.arccos(cosine))) >= route_turn_deg:
                reasons.append("route_turn")
        if reasons:
            selected.add(middle)
            reason_sets.setdefault(middle, set()).update(reasons)

    blur_limit = float(np.quantile(sharpness, blur_quantile))
    for ordinal in tuple(sorted(selected)):
        if sharpness[ordinal] > blur_limit:
            continue
        neighbors = [candidate for candidate in (ordinal - 1, ordinal + 1) if 0 <= candidate < count and candidate not in selected]
        if not neighbors:
            continue
        rescue = max(neighbors, key=lambda value: float(sharpness[value]))
        if sharpness[rescue] >= sharpness[ordinal] * sharpness_rescue_ratio:
            selected.add(rescue)
            reason_sets.setdefault(rescue, set()).add("sharpness_rescue")
    ordered = tuple(sorted(selected))
    return AdaptiveFramePlan(
        selected_ordinals=ordered,
        reasons={ordinal: tuple(sorted(reason_sets[ordinal])) for ordinal in ordered},
    )
