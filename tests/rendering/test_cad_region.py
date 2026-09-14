import numpy as np
import pytest
from cadscene.core.camera import CameraState
from cadscene.rendering.calibration import CalibratedCameraModel
from cadscene.rendering import cad_region as region


def test_clip_crossing_segment_and_interpolate_height():
    area=region.CadRenderRegion((0,0,10,10))
    a,b,keep=area.clip_segments(np.array([[-10,5,0],[1,20,3],[1,1,7.]]),
                                np.array([[20,5,30],[8,20,3],[2,2,8.]]))
    assert keep.tolist()==[True,False,True]
    np.testing.assert_allclose(a,[[0,5,10],[1,1,7]])
    np.testing.assert_allclose(b,[[10,5,20],[2,2,8]])


def test_region_contains_boundary_points_and_rejects_outside():
    area=region.CadRenderRegion((0,0,10,10))
    assert area.contains_points(np.array([[0,0],[10,10],[11,5]])).tolist()==[True,True,False]


@pytest.mark.parametrize('bounds',[(0,0,0,10),(5,0,1,10),(0,0,float('nan'),10),(0,1,2)])
def test_invalid_region_fails(bounds):
    with pytest.raises(ValueError):
        region.CadRenderRegion(bounds)


def test_auto_region_uses_whole_track_but_not_unbounded_horizon():
    camera=CalibratedCameraModel(100,80,50,50,40,0,0)
    poses=[CameraState(0,0,60,0,20,0,90),CameraState(0,500,60,0,20,0,90)]
    area=region.region_from_track(poses,camera,margin_m=100,lookahead_m=1000)
    assert area.contains_points(np.array([[0,0],[0,500],[0,800]])).all()
    assert not area.contains_points(np.array([[0,20000]])).any()
    assert area.bounds[3]<=1600.000001


def test_explicit_region_overrides_auto():
    camera=CalibratedCameraModel(100,80,50,50,40,0,0)
    area=region.region_from_track([],camera,bounds=(-5,-6,7,8))
    assert area.bounds==(-5,-6,7,8)


def test_index_keeps_far_and_crossing_segments_but_rejects_invisible_blocks():
    a=np.array([[-1,10,0],[-50,800,0],[-10000,10,0],[200,10,0],[0,-10,0.]])
    b=np.array([[1,10,0],[50,800,0],[10000,10,0],[201,10,0],[1,-10,0.]])
    index=region.SegmentVisibilityIndex(a,b,leaf_size=1)
    camera=CalibratedCameraModel(100,80,50,50,40,0,0)
    assert index.query(CameraState(),camera,padding_px=4).tolist()==[0,1,2]


def test_distortion_falls_back_to_regional_candidates():
    a=np.array([[0,-10,0],[200,10,0.]])
    index=region.SegmentVisibilityIndex(a,a+1,leaf_size=1)
    camera=CalibratedCameraModel(100,80,50,50,40,-.2,.01)
    assert index.query(CameraState(),camera).tolist()==[0,1]


def test_empty_index_has_no_candidates():
    index=region.SegmentVisibilityIndex(np.empty((0,3)),np.empty((0,3)))
    assert len(index.query(CameraState(),CalibratedCameraModel(100,80,50,50,40,0,0)))==0


def test_indexed_render_matches_unindexed_region_pixels():
    from cadscene.rendering.calibrated_overlay import render_calibrated_frame
    rng = np.random.default_rng(2026)
    a = rng.uniform([-100,-30,-30], [100,200,40], (500,3))
    b = a + rng.uniform(-20,20,(500,3))
    colors = rng.integers(0,256,(500,3),dtype=np.uint8)
    widths = rng.integers(1,8,500,dtype=np.int32)
    index = region.SegmentVisibilityIndex(a,b,leaf_size=8)
    camera = CalibratedCameraModel(100,80,50,50,40,0,0)
    frame = np.full((80,100,3),90,dtype=np.uint8)
    for yaw in (0,45,160):
        state = CameraState(0,0,10,yaw,20,7,90)
        reference = render_calibrated_frame(frame,state,a,b,colors,widths,camera)
        actual = render_calibrated_frame(frame,state,a,b,colors,widths,camera,visibility_index=index)
        np.testing.assert_array_equal(actual,reference)


def test_production_region_preserves_original_terrain_segment_slope(tmp_path):
    from cadscene.rendering.calibrated_overlay import _drape_region
    controls = tmp_path/'controls.npz'
    np.savez(controls, points_xyz=[[-10.,5.,0.],[10.,5.,20.]],
             segment_starts_xyz=np.empty((0,3)), segment_ends_xyz=np.empty((0,3)))
    a,b,colors = _drape_region(np.array([[-10.,5.]]),np.array([[10.,5.]]),
        np.array([[12,34,56]],dtype=np.uint8), controls, 'terrain', fallback_z=0.,
        region=region.CadRenderRegion((0,0,8,10)))
    np.testing.assert_allclose(a,[[0,5,10]])
    np.testing.assert_allclose(b,[[8,5,18]])
    assert colors.tolist()==[[12,34,56]]

