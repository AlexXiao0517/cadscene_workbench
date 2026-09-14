"""Fixed construction-region selection and conservative per-frame visibility."""
from dataclasses import dataclass

import numpy as np

from cadscene.cad.projection import world_from_camera_rotation


@dataclass(frozen=True)
class CadRenderRegion:
    bounds: tuple[float, float, float, float]

    def __post_init__(self):
        b = np.asarray(self.bounds, dtype=float)
        if b.shape != (4,) or not np.isfinite(b).all() or np.any(b[2:] <= b[:2]):
            raise ValueError("CAD region must be finite xmin,ymin,xmax,ymax with positive area")
        object.__setattr__(self, "bounds", tuple(float(v) for v in b))

    def contains_points(self, points):
        xy = np.asarray(points)[:, :2]
        return ((xy >= self.bounds[:2]) & (xy <= self.bounds[2:])).all(axis=1)

    def clip_segments(self, starts, ends):
        a, b = np.asarray(starts, dtype=float), np.asarray(ends, dtype=float)
        if a.shape != b.shape or a.ndim != 2 or a.shape[1] not in (2, 3):
            raise ValueError("region segments must have matching Nx2 or Nx3 shapes")
        delta = b - a
        lo, hi = np.zeros(len(a)), np.ones(len(a))
        keep = np.isfinite(a).all(axis=1) & np.isfinite(b).all(axis=1)
        for axis in range(2):
            parallel = delta[:, axis] == 0
            keep &= ~parallel | ((a[:, axis] >= self.bounds[axis]) & (a[:, axis] <= self.bounds[axis+2]))
            divisor = np.where(parallel, 1., delta[:, axis])
            first = (self.bounds[axis] - a[:, axis]) / divisor
            second = (self.bounds[axis+2] - a[:, axis]) / divisor
            lo = np.maximum(lo, np.where(parallel, -np.inf, np.minimum(first, second)))
            hi = np.minimum(hi, np.where(parallel, np.inf, np.maximum(first, second)))
        keep &= hi >= lo
        return a[keep] + lo[keep, None]*delta[keep], a[keep] + hi[keep, None]*delta[keep], keep


def region_from_track(camera_states, camera_model, *, ground_z=0., margin_m=100., lookahead_m=1000., bounds=None):
    """Heuristic site envelope, fixed for the entire clip (not a per-frame cutoff)."""
    if bounds is not None:
        return CadRenderRegion(bounds)
    if not np.isfinite([ground_z, margin_m, lookahead_m]).all() or margin_m < 0 or lookahead_m <= 0:
        raise ValueError("region margin and lookahead must be finite and nonnegative/positive")
    rays = np.array([[(x-camera_model.cx_px)/camera_model.focal_px,
                      (y-camera_model.cy_px)/camera_model.focal_px, 1.]
                     for x in (0, camera_model.width/2, camera_model.width)
                     for y in (0, camera_model.height/2, camera_model.height)])
    points = []
    for state in camera_states:
        center = np.array([state.camera_x, state.camera_y, state.camera_z])
        world_rays = rays @ world_from_camera_rotation(state).T
        if not np.isfinite(center).all() or not np.isfinite(world_rays).all():
            raise ValueError("region requires finite camera poses")
        horizontal = np.linalg.norm(world_rays[:, :2], axis=1)
        cap = lookahead_m / np.maximum(horizontal, 1e-12)
        z = world_rays[:, 2]
        t = np.divide(ground_z-center[2], z, out=np.full(len(z), np.inf), where=np.abs(z)>1e-12)
        t = np.where(t > 0, np.minimum(t, cap), cap)
        points.extend((center[None, :2], center[:2]+world_rays[:, :2]*t[:, None]))
    if not points:
        raise ValueError("cannot infer CAD region without camera poses")
    xy = np.concatenate(points)
    return CadRenderRegion((*np.min(xy, axis=0)-margin_m, *np.max(xy, axis=0)+margin_m))


class SegmentVisibilityIndex:
    """Spatial blocks with conservative pinhole frustum rejection; no far plane."""
    def __init__(self, starts, ends, *, leaf_size=128):
        a, b = np.asarray(starts), np.asarray(ends)
        if a.shape != b.shape or a.ndim != 2 or a.shape[1] != 3 or leaf_size < 1:
            raise ValueError("visibility index requires matching Nx3 segments and positive leaf size")
        if not np.isfinite(a).all() or not np.isfinite(b).all():
            raise ValueError("visibility index requires finite geometry")
        self.count = len(a)
        centers = (a+b)*.5
        self.blocks, lows, highs = [], [], []
        def split(ids):
            if len(ids) <= leaf_size:
                self.blocks.append(ids)
                lows.append(np.minimum(a[ids], b[ids]).min(axis=0))
                highs.append(np.maximum(a[ids], b[ids]).max(axis=0))
                return
            axis = np.argmax(np.ptp(centers[ids], axis=0))
            middle = len(ids)//2
            ordered = ids[np.argpartition(centers[ids, axis], middle)]
            split(ordered[:middle])
            split(ordered[middle:])
        if len(a):
            split(np.arange(len(a)))
        self.low = np.asarray(lows).reshape(-1, 3)
        self.high = np.asarray(highs).reshape(-1, 3)

    def query(self, state, camera, *, padding_px=4):
        if not self.count or camera.k1 != 0 or camera.k2 != 0:
            return np.arange(self.count)
        p = max(0., float(padding_px))
        f, cx, cy = camera.focal_px, camera.cx_px, camera.cy_px
        normals = np.array([[f,0,cx+p],[-f,0,camera.width+p-cx],
                            [0,f,cy+p],[0,-f,camera.height+p-cy],[0,0,1.]])
        normals = normals @ world_from_camera_rotation(state).T
        center = np.array([state.camera_x, state.camera_y, state.camera_z])
        offsets = -normals @ center
        offsets[-1] -= .05
        support = np.maximum(self.low[:, None, :]*normals, self.high[:, None, :]*normals).sum(axis=2)
        visible = (support+offsets >= -1e-8).all(axis=1)
        selected = [ids for ids, keep in zip(self.blocks, visible) if keep]
        return np.sort(np.concatenate(selected)) if selected else np.empty(0, dtype=int)
