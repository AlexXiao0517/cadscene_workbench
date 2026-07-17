from __future__ import annotations

import numpy as np

from cadscene.cad.centerline import CenterlineModel, project_station_lateral


def test_project_station_lateral_on_straight_centerline() -> None:
    center = CenterlineModel.from_points(np.asarray([[0.0, 0.0], [10.0, 0.0]], dtype=np.float64))

    station, lateral, index = project_station_lateral(center, (4.0, 3.0))

    assert station == 4.0
    assert lateral == 3.0
    assert index == 0
