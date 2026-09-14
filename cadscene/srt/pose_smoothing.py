"""Time-local telemetry smoothing before user keyframe residuals are fitted."""
from copy import deepcopy

import numpy as np
from scipy.spatial.transform import Rotation


def smooth_pose_samples(poses, *, time_base_seconds: float, window_sec: float = 1.0):
    """Fit centred local quadratics to XYZ and SO(3) logs; never cross gaps.

    Local time regressions preserve constant velocity, including asymmetric
    endpoint windows. Rotations are fitted about the current sample (not Euler
    angles), so sign-equivalent quaternions and +/-180 crossings are harmless.
    Raw telemetry is retained separately and is never overwritten in-place.
    """
    if not np.isfinite(time_base_seconds) or time_base_seconds <= 0:
        raise ValueError('time base must be positive and finite')
    if not np.isfinite(window_sec) or window_sec <= 0:
        raise ValueError('smoothing window must be positive and finite')
    output = deepcopy(poses)
    times = np.asarray([p['source_pts'] for p in poses], dtype=float) * time_base_seconds
    if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
        raise ValueError('pose times must be finite and strictly increasing')
    groups, current = [], []
    for i, pose in enumerate(poses):
        if not pose.get('registered', False):
            if current:
                groups.append(current)
            current = []
            continue
        if current and (times[i] - times[current[-1]] > 1.5 or
                        pose['frame_index'] != poses[current[-1]]['frame_index'] + 1):
            groups.append(current)
            current = []
        current.append(i)
    if current:
        groups.append(current)
    changed, corrections, angles = 0, [], []
    for group in groups:
        if len(group) < 5:
            continue
        t = times[group]
        xyz = np.asarray([poses[i]['center'] for i in group], dtype=float)
        quats = np.asarray([poses[i]['cam_from_world_quat_wxyz'] for i in group], dtype=float)
        if not np.isfinite(xyz).all() or not np.isfinite(quats).all():
            raise ValueError('registered poses must contain finite position and rotation')
        rotations = Rotation.from_quat(quats[:, [1, 2, 3, 0]])
        absolute = np.asarray([poses[i].get('abs_alt', np.nan) for i in group], dtype=float)
        for j, i in enumerate(group):
            left = np.searchsorted(t, t[j] - window_sec/2 - 1e-9)
            right = np.searchsorted(t, t[j] + window_sec/2 + 1e-9, side='right')
            if right - left < 5:
                continue
            dt = (t[left:right] - t[j]) / window_sec
            design = np.column_stack((np.ones(len(dt)), dt, dt*dt))
            # Fit offsets rather than large absolute projected coordinates.
            delta = np.linalg.lstsq(design, xyz[left:right] - xyz[j], rcond=None)[0][0]
            local = (rotations[j].inv() * rotations[left:right]).as_rotvec()
            angular_delta = np.linalg.lstsq(design, local, rcond=None)[0][0]
            smoothed = rotations[j] * Rotation.from_rotvec(angular_delta)
            output[i]['raw_center'] = deepcopy(poses[i]['center'])
            output[i]['raw_cam_from_world_quat_wxyz'] = deepcopy(poses[i]['cam_from_world_quat_wxyz'])
            output[i]['center'] = (xyz[j] + delta).tolist()
            output[i]['cam_from_world_quat_wxyz'] = smoothed.as_quat()[[3, 0, 1, 2]].tolist()
            if np.isfinite(absolute[left:right]).all():
                output[i]['smoothed_abs_alt'] = float(absolute[j] + np.linalg.lstsq(
                    design, absolute[left:right] - absolute[j], rcond=None)[0][0])
            changed += 1
            corrections.append(float(np.linalg.norm(delta)))
            angles.append(float(np.rad2deg(np.linalg.norm(angular_delta))))
    return output, dict(method='centered_local_quadratic_so3_v1', window_sec=window_sec,
                        smoothed_frame_count=changed, max_position_correction_m=max(corrections, default=0.),
                        max_rotation_correction_deg=max(angles, default=0.),
                        applied_before_keyframe_fitting=True)
