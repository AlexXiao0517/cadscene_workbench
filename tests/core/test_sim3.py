import numpy as np

from cadscene.core.sim3 import Sim3


def test_sim3_apply_inverse_roundtrip():
    transform = Sim3(
        scale=2.0,
        rotation=np.array(
            [
                [0.0, -1.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=float,
        ),
        translation=np.array([10.0, -3.0, 5.0], dtype=float),
    )
    points = np.array([[1.0, 2.0, 3.0], [-4.0, 5.0, 6.0]], dtype=float)

    restored = transform.inverse().apply(transform.apply(points))

    np.testing.assert_allclose(restored, points)


def test_sim3_json_roundtrip_preserves_values():
    transform = Sim3.identity()
    data = transform.to_dict()

    loaded = Sim3.from_dict(data)

    assert loaded.scale == 1.0
    np.testing.assert_allclose(loaded.rotation, np.eye(3))
    np.testing.assert_allclose(loaded.translation, np.zeros(3))

