from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

import numpy as np

from cadscene.alignment.keyframes import confirmed_keyframes
from cadscene.core.io import read_json

RISK_KEYS = ("anchor_distance", "segment_drift", "correction", "sfm_quality", "visual_residual", "turn_motion")

REASON_BY_SIGNAL = {
    "anchor_distance": "far_from_anchor",
    "segment_drift": "high_segment_drift",
    "correction": "large_anchor_correction",
    "sfm_quality": "low_sfm_observations",
    "visual_residual": "high_visual_residual",
    "turn_motion": "high_turn_motion",
}

REASON_TEXT = {
    "far_from_anchor": "距最近人工关键帧较远",
    "high_segment_drift": "global sim3 与 anchored path 差异较大",
    "large_anchor_correction": "分段锚定修正量较大",
    "low_sfm_observations": "SfM 注册质量或观测质量偏低",
    "unregistered_sfm_frame": "该帧 SfM 未注册",
    "high_visual_residual": "视觉残差较高",
    "high_turn_motion": "局部转弯或路径曲率较大",
}


@dataclass(frozen=True)
class QualityThresholds:
    anchor_distance_frames: float = 120.0
    segment_drift_translation_m: float = 3.0
    correction_translation_m: float = 3.0
    correction_yaw_deg: float = 2.0
    correction_pitch_deg: float = 1.0
    correction_slope_translation: float = 0.05
    correction_slope_yaw: float = 0.05
    min_sfm_observations: float = 80.0
    reproj_error_threshold_px: float = 2.0
    motion_yaw_threshold_deg: float = 25.0
    motion_curvature_threshold_deg: float = 25.0


@dataclass(frozen=True)
class SuggestionConfig:
    max_suggestions: int = 10
    min_suggestion_gap: int = 40
    suggestion_risk_threshold: float = 0.65
    reason_threshold: float = 0.6


@dataclass(frozen=True)
class QualityConfig:
    quality_mode: str = "bootstrap"
    allow_bootstrap_correction: bool = False
    adaptive_percentile: float = 85.0
    max_suggestions: int = 10
    min_suggestion_gap: int = 40
    suggestion_risk_threshold: float = 0.65
    thresholds: QualityThresholds = QualityThresholds()
    fps: float = 25.0
    seg_dir: str | None = None

    def suggestion_config(self) -> SuggestionConfig:
        return SuggestionConfig(
            max_suggestions=int(self.max_suggestions),
            min_suggestion_gap=int(self.min_suggestion_gap),
            suggestion_risk_threshold=float(self.suggestion_risk_threshold),
        )


@dataclass(frozen=True)
class QualityResult:
    timeline: list[dict]
    suggestions: list[dict]
    suggestion_frames: set[int]
    meta: dict
    signal_availability: dict
    weights: dict
    warnings: list[str]


def risk_level(score: float) -> str:
    value = float(score)
    if value < 0.35:
        return "low"
    if value < 0.65:
        return "medium"
    return "high"


def suggest_action(level: str) -> str:
    if level == "high":
        return "建议补关键帧"
    if level == "medium":
        return "建议检查"
    return "暂不处理"


def _clip01(value: float) -> float:
    return float(np.clip(float(value), 0.0, 1.0))


def normalize_weights(weights: Mapping[str, float], available: Mapping[str, bool]) -> dict[str, float]:
    usable = {name: float(weight) for name, weight in weights.items() if available.get(name, False) and float(weight) > 0.0}
    total = sum(usable.values())
    if total <= 1e-12:
        return {}
    return {name: value / total for name, value in usable.items()}


def combined_risk_score(components: Mapping[str, float | None], weights_norm: Mapping[str, float]) -> float:
    return _clip01(sum(float(components[name]) * float(weight) for name, weight in weights_norm.items() if components.get(name) is not None))


def build_reason_codes(components: Mapping[str, float | None], registered: bool = True, reason_threshold: float = 0.6) -> list[str]:
    codes: list[str] = []
    if not registered:
        codes.append("unregistered_sfm_frame")
    for name, value in components.items():
        if value is None:
            continue
        if float(value) >= float(reason_threshold):
            code = REASON_BY_SIGNAL.get(name)
            if code:
                codes.append(code)
    return codes


def reason_text(codes: Sequence[str]) -> list[str]:
    return [REASON_TEXT.get(code, code) for code in codes]


def anchor_distance_risk(frame_index: int, anchor_frames: Sequence[int], threshold_frames: float) -> tuple[float, int | None, int | None]:
    if not anchor_frames:
        return 1.0, None, None
    nearest = min(anchor_frames, key=lambda frame: abs(int(frame) - int(frame_index)))
    distance = abs(int(nearest) - int(frame_index))
    return _clip01(distance / max(1e-6, float(threshold_frames))), int(nearest), int(distance)


def sfm_quality_risk(registered: bool, observation_count: float | None, reproj_error_px: float | None, th: QualityThresholds) -> float:
    if not registered:
        return 1.0
    risks: list[float] = []
    if observation_count is not None:
        risks.append(_clip01((th.min_sfm_observations - float(observation_count)) / max(1e-6, th.min_sfm_observations)))
    if reproj_error_px is not None:
        risks.append(_clip01(float(reproj_error_px) / max(1e-6, th.reproj_error_threshold_px)))
    return max(risks) if risks else 0.0


def _angle_diff(a: float, b: float) -> float:
    return abs((float(a) - float(b) + 180.0) % 360.0 - 180.0)


def _default_weights(mode: str, allow_bootstrap_correction: bool, safe_segment_drift: bool) -> dict[str, float]:
    if mode == "qa":
        return {
            "segment_drift": 0.25,
            "correction": 0.25,
            "visual_residual": 0.20,
            "sfm_quality": 0.15,
            "anchor_distance": 0.10,
            "turn_motion": 0.05,
        }
    return {
        "anchor_distance": 0.30,
        "sfm_quality": 0.25,
        "turn_motion": 0.15,
        "visual_residual": 0.20,
        "segment_drift": 0.10 if safe_segment_drift else 0.0,
        "correction": 0.10 if allow_bootstrap_correction else 0.0,
    }


def _read_csv_rows(path: str | Path) -> list[dict]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _float(row: Mapping[str, object], name: str, default: float = 0.0) -> float:
    value = row.get(name, default)
    if value in ("", None):
        return float(default)
    return float(value)


def load_sfm_camera_path(path: str | Path) -> list[dict]:
    out: list[dict] = []
    for row in _read_csv_rows(path):
        if str(row.get("status", "ok")).lower() != "ok":
            continue
        out.append(
            {
                "frame_index": int(float(row["frame_index"])),
                "camera_x": _float(row, "camera_x"),
                "camera_y": _float(row, "camera_y"),
                "camera_z": _float(row, "camera_z"),
                "yaw": _float(row, "yaw"),
                "pitch": _float(row, "pitch"),
                "roll": _float(row, "roll"),
                "fov": _float(row, "fov", 70.0),
            }
        )
    return out


def _trajectory_quality_map(trajectory_json: Mapping[str, object] | None) -> tuple[dict[int, dict], float, bool]:
    if not trajectory_json:
        return {}, 25.0, False
    fps = float(trajectory_json.get("fps", 25.0) or 25.0)
    out: dict[int, dict] = {}
    has_quality = False
    for pose in trajectory_json.get("poses", []):
        frame = int(pose.get("frame_index", 0))
        obs = pose.get("num_observations", pose.get("observation_count"))
        reproj = pose.get("reprojection_error", pose.get("reproj_error_px"))
        if obs is not None or reproj is not None or "registered" in pose:
            has_quality = True
        out[frame] = {
            "registered": bool(pose.get("registered", True)),
            "num_observations": None if obs is None else float(obs),
            "reprojection_error": None if reproj is None else float(reproj),
        }
    return out, fps, has_quality


def _interp_residual(frames: np.ndarray, values: np.ndarray, frame_index: int) -> np.ndarray:
    if len(frames) == 0:
        return np.zeros(values.shape[1] if values.ndim == 2 else 1, dtype=np.float64)
    if frame_index <= frames[0]:
        return values[0].copy()
    if frame_index >= frames[-1]:
        return values[-1].copy()
    j = int(np.searchsorted(frames, frame_index, side="right"))
    i = j - 1
    alpha = (float(frame_index) - float(frames[i])) / max(1e-9, float(frames[j] - frames[i]))
    return values[i] * (1.0 - alpha) + values[j] * alpha


def _residual_arrays(alignment_json: Mapping[str, object] | None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    residuals = (alignment_json or {}).get("residuals") or {}
    frames = np.asarray(residuals.get("frames") or [], dtype=np.float64)
    pos = np.asarray(residuals.get("position_m") or [], dtype=np.float64)
    ang = np.asarray(residuals.get("angle_deg") or [], dtype=np.float64)
    if pos.ndim != 2:
        pos = np.zeros((0, 3), dtype=np.float64)
    if ang.ndim != 2:
        ang = np.zeros((0, 3), dtype=np.float64)
    return frames, pos, ang


def _adaptive_threshold(default: float, values: Sequence[float], percentile: float) -> float:
    if percentile <= 0 or not values:
        return float(default)
    return max(float(default), float(np.percentile(np.asarray(values, dtype=np.float64), float(percentile))))


def _turn_motion_risks(rows: Sequence[Mapping[str, object]], th: QualityThresholds) -> list[float]:
    risks: list[float] = []
    for i, row in enumerate(rows):
        prev_row = rows[max(0, i - 1)]
        next_row = rows[min(len(rows) - 1, i + 1)]
        yaw_change = max(_angle_diff(row.get("yaw", 0.0), prev_row.get("yaw", 0.0)), _angle_diff(next_row.get("yaw", 0.0), row.get("yaw", 0.0)))
        dx1 = float(row.get("camera_x", 0.0)) - float(prev_row.get("camera_x", 0.0))
        dy1 = float(row.get("camera_y", 0.0)) - float(prev_row.get("camera_y", 0.0))
        dx2 = float(next_row.get("camera_x", 0.0)) - float(row.get("camera_x", 0.0))
        dy2 = float(next_row.get("camera_y", 0.0)) - float(row.get("camera_y", 0.0))
        a1 = math.degrees(math.atan2(dy1, dx1)) if abs(dx1) + abs(dy1) > 1e-9 else 0.0
        a2 = math.degrees(math.atan2(dy2, dx2)) if abs(dx2) + abs(dy2) > 1e-9 else a1
        curvature = _angle_diff(a2, a1)
        risks.append(_clip01(max(yaw_change / max(1e-6, th.motion_yaw_threshold_deg), curvature / max(1e-6, th.motion_curvature_threshold_deg))))
    return risks


def select_suggestions(timeline: Sequence[dict], cfg: SuggestionConfig) -> list[dict]:
    candidates = [row for row in timeline if float(row.get("risk_score", 0.0)) >= float(cfg.suggestion_risk_threshold)]
    candidates.sort(key=lambda row: float(row.get("risk_score", 0.0)), reverse=True)
    chosen: list[dict] = []
    for row in candidates:
        frame = int(row["frame_index"])
        if any(abs(frame - int(other["frame_index"])) < int(cfg.min_suggestion_gap) for other in chosen):
            continue
        chosen.append(row)
        if len(chosen) >= int(cfg.max_suggestions):
            break
    chosen.sort(key=lambda row: int(row["frame_index"]))
    return chosen


def _suggestion_entry(row: Mapping[str, object], fps: float) -> dict:
    codes = [code for code in str(row.get("reason_codes", "")).split("|") if code]
    return {
        "frame_index": int(row["frame_index"]),
        "timestamp_sec": round(int(row["frame_index"]) / max(1e-6, fps), 3),
        "priority": row.get("risk_level", "high"),
        "risk_score": float(row.get("risk_score", 0.0)),
        "risk_level": row.get("risk_level", "high"),
        "reason": "；".join(reason_text(codes)),
        "reason_codes": codes,
        "nearest_anchor_frame": row.get("nearest_anchor_frame", ""),
        "suggest_action": "建议在前端跳转到该帧，轻推微调后添加关键帧",
    }


def build_keyframe_suggestions_payload(suggestions: Sequence[dict], meta: Mapping[str, object]) -> dict:
    return {"meta": dict(meta), "suggestions": list(suggestions)}


def augment_camera_track_with_quality(track: Mapping[str, object], timeline: Sequence[Mapping[str, object]], suggestion_frames: set[int], meta: Mapping[str, object]) -> dict:
    by_frame = {int(row["frame_index"]): row for row in timeline}
    out = dict(track)
    keyframes: list[dict] = []
    for item in track.get("keyframes", []):
        copied = dict(item)
        frame = int(copied.get("frame", 0))
        row = by_frame.get(frame)
        if row:
            copied["quality"] = {
                "risk_score": float(row.get("risk_score", 0.0)),
                "risk_level": row.get("risk_level", "low"),
                "suggest_action": "建议补关键帧" if frame in suggestion_frames else row.get("suggest_action", ""),
                "reason_codes": [code for code in str(row.get("reason_codes", "")).split("|") if code],
            }
        keyframes.append(copied)
    out["keyframes"] = keyframes
    out["meta"] = {**dict(out.get("meta") or {}), **dict(meta)}
    return out


def evaluate_quality(
    *,
    sfm_camera_path_rows: Sequence[Mapping[str, object]],
    alignment_json: Mapping[str, object] | None,
    web_camera_track: Mapping[str, object],
    trajectory_json: Mapping[str, object] | None,
    config: QualityConfig,
    progress_callback: Callable[[int, int], None] | None = None,
) -> QualityResult:
    mode = config.quality_mode
    if mode not in {"bootstrap", "qa"}:
        raise ValueError("quality_mode 必须是 bootstrap 或 qa。")
    rows = sorted([dict(row) for row in sfm_camera_path_rows], key=lambda row: int(row["frame_index"]))
    anchor_frames = [int(item.get("frame", 0)) for item in confirmed_keyframes(dict(web_camera_track))]
    trajectory_by_frame, fps_from_traj, has_sfm_quality = _trajectory_quality_map(trajectory_json)
    fps = fps_from_traj or float(config.fps)
    frames_res, pos_res, ang_res = _residual_arrays(alignment_json)
    safe_segment_drift = bool(alignment_json and (alignment_json.get("schema_version") == "cadscene_alignment_v1"))
    correction_enabled = mode == "qa" or bool(config.allow_bootstrap_correction)
    segment_enabled = mode == "qa" or safe_segment_drift
    has_residuals = len(frames_res) > 0 and len(pos_res) > 0
    warnings: list[str] = []
    if mode == "bootstrap" and not config.allow_bootstrap_correction:
        warnings.append("bootstrap 模式默认禁用 correction，避免用已有人工锚点修正量泄漏推荐流程。")
    if not config.seg_dir:
        warnings.append("未提供 seg_dir，visual_residual 标记为 unavailable。")
    residual_mags = [float(np.linalg.norm(item)) for item in pos_res] if len(pos_res) else []
    residual_yaws = [abs(float(item[0])) for item in ang_res] if len(ang_res) else []
    residual_pitches = [abs(float(item[1])) for item in ang_res] if len(ang_res) else []
    correction_threshold = _adaptive_threshold(config.thresholds.correction_translation_m, residual_mags, config.adaptive_percentile) if correction_enabled else config.thresholds.correction_translation_m
    correction_yaw_threshold = _adaptive_threshold(config.thresholds.correction_yaw_deg, residual_yaws, config.adaptive_percentile) if correction_enabled else config.thresholds.correction_yaw_deg
    correction_pitch_threshold = _adaptive_threshold(config.thresholds.correction_pitch_deg, residual_pitches, config.adaptive_percentile) if correction_enabled else config.thresholds.correction_pitch_deg
    drift_threshold = _adaptive_threshold(config.thresholds.segment_drift_translation_m, residual_mags, config.adaptive_percentile) if segment_enabled else config.thresholds.segment_drift_translation_m
    turn_risks = _turn_motion_risks(rows, config.thresholds)
    base_weights = _default_weights(mode, config.allow_bootstrap_correction, safe_segment_drift)
    timeline: list[dict] = []
    for i, row in enumerate(rows):
        frame = int(row["frame_index"])
        anchor_risk, nearest_anchor, nearest_distance = anchor_distance_risk(frame, anchor_frames, config.thresholds.anchor_distance_frames)
        traj_q = trajectory_by_frame.get(frame)
        sfm_available = bool(has_sfm_quality and traj_q is not None)
        if sfm_available:
            sfm_risk = sfm_quality_risk(bool(traj_q.get("registered", True)), traj_q.get("num_observations"), traj_q.get("reprojection_error"), config.thresholds)
            registered = bool(traj_q.get("registered", True))
        else:
            sfm_risk = None
            registered = True
        res_pos = _interp_residual(frames_res, pos_res, frame) if has_residuals else np.zeros(3, dtype=np.float64)
        res_ang = _interp_residual(frames_res, ang_res, frame) if has_residuals else np.zeros(3, dtype=np.float64)
        segment_risk = _clip01(float(np.linalg.norm(res_pos)) / max(1e-6, drift_threshold)) if segment_enabled and has_residuals else None
        correction_mag = max(
            float(np.linalg.norm(res_pos)) / max(1e-6, correction_threshold),
            abs(float(res_ang[0])) / max(1e-6, correction_yaw_threshold),
            abs(float(res_ang[1])) / max(1e-6, correction_pitch_threshold),
        )
        correction_risk = _clip01(correction_mag) if correction_enabled and has_residuals else None
        visual_risk = None
        components = {
            "anchor_distance": anchor_risk,
            "segment_drift": segment_risk,
            "correction": correction_risk,
            "sfm_quality": sfm_risk,
            "visual_residual": visual_risk,
            "turn_motion": turn_risks[i],
        }
        available = {name: value is not None for name, value in components.items()}
        weights_norm = normalize_weights(base_weights, available)
        score = combined_risk_score(components, weights_norm)
        level = risk_level(score)
        codes = build_reason_codes(components, registered=registered, reason_threshold=0.6)
        available_names = [name for name, ok in available.items() if ok]
        unavailable_names = [name for name, ok in available.items() if not ok]
        timeline.append(
            {
                "frame_index": frame,
                "timestamp_sec": round(frame / max(1e-6, fps), 3),
                "risk_score": round(score, 6),
                "risk_level": level,
                "suggest_action": suggest_action(level),
                "anchor_distance_risk": round(anchor_risk, 6),
                "segment_drift_risk": round(segment_risk, 6) if segment_risk is not None else "unavailable",
                "correction_risk": round(correction_risk, 6) if correction_risk is not None else "unavailable",
                "sfm_quality_risk": round(sfm_risk, 6) if sfm_risk is not None else "unavailable",
                "visual_residual_risk": "unavailable",
                "turn_motion_risk": round(turn_risks[i], 6),
                "reason_codes": "|".join(codes),
                "nearest_anchor_frame": "" if nearest_anchor is None else nearest_anchor,
                "nearest_anchor_distance": "" if nearest_distance is None else nearest_distance,
                "available_signals": "|".join(available_names),
                "unavailable_signals": "|".join(unavailable_names),
            }
        )
        if progress_callback is not None:
            progress_callback(i + 1, len(rows))
    suggestions_rows = select_suggestions(timeline, config.suggestion_config())
    suggestions = [_suggestion_entry(row, fps) for row in suggestions_rows]
    meta = {
        "quality_mode": mode,
        "confirmed_anchor_count": len(anchor_frames),
        "confirmed_anchor_frames": anchor_frames,
        "leakage_guard_enabled": True,
        "generated_by": "cadscene.evaluate_quality",
    }
    return QualityResult(
        timeline=timeline,
        suggestions=suggestions,
        suggestion_frames={int(row["frame_index"]) for row in suggestions_rows},
        meta=meta,
        signal_availability={
            "anchor_distance": True,
            "segment_drift": bool(segment_enabled and has_residuals),
            "correction": bool(correction_enabled and has_residuals),
            "sfm_quality": bool(has_sfm_quality),
            "visual_residual": False,
            "turn_motion": True,
        },
        weights=base_weights,
        warnings=warnings,
    )


def quality_report(
    *,
    result: QualityResult,
    inputs: Mapping[str, object],
    algorithm_prediction_count: int,
    adaptive_percentile: float,
) -> str:
    levels = [row["risk_level"] for row in result.timeline]
    lines = [
        "# SfM-CAD 对齐质量报告",
        "",
        "## 输入文件",
        "",
    ]
    for name, value in inputs.items():
        lines.append(f"- {name}: `{value}`")
    lines += [
        "",
        "## 模式说明",
        "",
        f"- quality_mode: {result.meta['quality_mode']}",
        "- 当前模块只读评估，不修改任何相机位姿。",
    ]
    if result.meta["quality_mode"] == "qa":
        lines.append("- QA 模式用于检查已有关键帧对齐质量，不代表初始自动推荐流程。")
    else:
        lines.append("- bootstrap 模式用于初始推荐关键帧，并启用 leakage guard。")
    lines += [
        "",
        "## 锚点与预测",
        "",
        f"- confirmed_anchor_count: {result.meta['confirmed_anchor_count']}",
        f"- confirmed_anchor_frames: {result.meta['confirmed_anchor_frames']}",
        f"- algorithm_prediction_count: {algorithm_prediction_count}",
        "",
        "## 信号与权重",
        "",
    ]
    for name, weight in result.weights.items():
        status = "启用" if result.signal_availability.get(name) and float(weight) > 0 else "禁用"
        lines.append(f"- {name}: weight={weight:.3f}, {status}")
    lines += [
        "",
        f"- adaptive_percentile: {adaptive_percentile}",
        "",
        "## 风险分布",
        "",
        f"- high: {levels.count('high')}",
        f"- medium: {levels.count('medium')}",
        f"- low: {levels.count('low')}",
        "",
        "## 建议帧列表",
        "",
    ]
    if not result.suggestions:
        lines.append("- 暂无超过阈值的建议帧。")
    else:
        lines.append("建议帧按 frame_index 升序输出。")
        for item in result.suggestions:
            lines.append(f"- frame {item['frame_index']}: risk={item['risk_score']:.3f}, reasons={','.join(item['reason_codes'])}")
    lines += ["", "## Warning", ""]
    lines.extend([f"- {warning}" for warning in result.warnings] or ["- 无"])
    lines += ["", "## 声明", "", "本阶段不做 automatic refine，不做 semantic refine，不修改 alignment 或 camera path。", ""]
    return "\n".join(lines)
