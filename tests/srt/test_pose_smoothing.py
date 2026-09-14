from copy import deepcopy
import numpy as np
import pytest
from scipy.spatial.transform import Rotation
from cadscene.srt import full_pose


def samples(times, x, yaw=None):
    yaw = np.zeros(len(times)) if yaw is None else yaw
    q = Rotation.from_euler('z', yaw, degrees=True).as_quat()[:, [3, 0, 1, 2]]
    return [dict(frame_index=i, source_pts=float(t), registered=True,
                 center=[float(v), 7., 194. + 0.2*t], cam_from_world_quat_wxyz=r.tolist())
            for i, (t, v, r) in enumerate(zip(times, x, q))]


def smooth(poses, **kwargs):
    assert hasattr(full_pose, 'smooth_pose_samples'), 'full-pose telemetry smoothing is missing'
    return full_pose.smooth_pose_samples(poses, time_base_seconds=1., **kwargs)


def test_smooths_staircase_positions_without_changing_input():
    t = np.arange(121) / 30.
    poses = samples(t, np.floor(t*5)/5*8)
    original = deepcopy(poses)
    result, info = smooth(poses)
    raw = np.array([p['center'] for p in poses])
    filtered = np.array([p['center'] for p in result])
    assert np.max(abs(np.diff(filtered[:, 0]))) < .6
    assert np.std(np.diff(filtered[:, 0], n=2)) < np.std(np.diff(raw[:, 0], n=2)) * .2
    np.testing.assert_allclose(filtered[:, 2], raw[:, 2], atol=1e-9)
    assert poses == original
    assert result[60]['raw_center'] == original[60]['center']
    assert info['smoothed_frame_count'] == 121


def test_preserves_linear_motion_and_rotation_on_irregular_pts():
    t = np.cumsum(np.tile([.02, .04, .03], 30))
    poses = samples(t, t*4, 170+t*6)
    result, _ = smooth(poses)
    np.testing.assert_allclose([p['center'] for p in result], [p['center'] for p in poses], atol=1e-8)
    a = Rotation.from_quat(np.array([p['cam_from_world_quat_wxyz'] for p in poses])[:, [1,2,3,0]])
    b = Rotation.from_quat(np.array([p['cam_from_world_quat_wxyz'] for p in result])[:, [1,2,3,0]])
    assert np.max((a.inv()*b).magnitude()) < 1e-8


def test_rotation_noise_smoothing_crosses_180_without_flip():
    t = np.arange(121)/30.
    poses = samples(t, t, 179+t + .2*(-1.)**np.arange(len(t)))
    result, _ = smooth(poses)
    r = Rotation.from_quat(np.array([p['cam_from_world_quat_wxyz'] for p in result])[:, [1,2,3,0]])
    delta = np.rad2deg((r[:-1].inv()*r[1:]).magnitude())
    assert delta.max() < .1


def test_does_not_smooth_across_invalid_pose_or_change_missing_data():
    poses = samples(np.arange(61)/30., np.r_[np.zeros(30), 0, np.ones(30)*100])
    poses[30]['registered'] = False
    result, _ = smooth(poses)
    assert result[30] == poses[30]
    assert result[29]['center'][0] == pytest.approx(0)
    assert result[31]['center'][0] == pytest.approx(100)


def test_short_runs_unchanged_and_bad_time_rejected():
    poses = samples(np.arange(3)/30., [0, 1, 2])
    result, _ = smooth(poses)
    assert result == poses
    poses[2]['source_pts'] = 0
    with pytest.raises(ValueError, match='increasing'):
        smooth(poses)


def test_absolute_height_smoothed_before_terrain_mode_selection():
    t = np.arange(121)/30.
    poses = samples(t, t)
    for i,p in enumerate(poses):
        p['abs_alt'] = 194. + .2 * (-1.)**i
    result, _ = smooth(poses)
    assert all('smoothed_abs_alt' in p for p in result)
    assert np.max(abs(np.diff([p['smoothed_abs_alt'] for p in result]))) < .1
    assert [p['abs_alt'] for p in result] == [p['abs_alt'] for p in poses]
