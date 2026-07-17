from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pytest

from cadscene.sfm.pointcloud import load_ply


def test_ascii_ply_reads_xyz_and_rgb_0_255(tmp_path: Path) -> None:
    path = tmp_path / "points_ascii.ply"
    path.write_text(
        "\n".join(
            [
                "ply",
                "format ascii 1.0",
                "element vertex 1",
                "property float x",
                "property float y",
                "property float z",
                "property uchar red",
                "property uchar green",
                "property uchar blue",
                "end_header",
                "1 2 3 10 20 255",
            ]
        ),
        encoding="utf-8",
    )

    points, colors = load_ply(path)

    np.testing.assert_allclose(points, [[1.0, 2.0, 3.0]])
    assert colors.dtype == np.uint8
    assert colors.tolist() == [[10, 20, 255]]


def test_binary_little_endian_ply_reads_xyz_and_rgb(tmp_path: Path) -> None:
    path = tmp_path / "points_binary.ply"
    header = "\n".join(
        [
            "ply",
            "format binary_little_endian 1.0",
            "element vertex 2",
            "property float x",
            "property float y",
            "property float z",
            "property uchar red",
            "property uchar green",
            "property uchar blue",
            "end_header",
            "",
        ]
    ).encode("ascii")
    body = struct.pack("<fffBBBfffBBB", 1.0, 2.0, 3.0, 1, 2, 3, 4.0, 5.0, 6.0, 250, 251, 252)
    path.write_bytes(header + body)

    points, colors = load_ply(path)

    np.testing.assert_allclose(points, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    assert colors.tolist() == [[1, 2, 3], [250, 251, 252]]


def test_binary_big_endian_has_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "points_big.ply"
    path.write_bytes(
        "\n".join(
            [
                "ply",
                "format binary_big_endian 1.0",
                "element vertex 0",
                "property float x",
                "property float y",
                "property float z",
                "end_header",
                "",
            ]
        ).encode("ascii")
    )

    with pytest.raises(ValueError, match="binary_big_endian"):
        load_ply(path)
