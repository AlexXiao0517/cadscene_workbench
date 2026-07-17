from __future__ import annotations

import struct
from pathlib import Path
from typing import Optional

import numpy as np

from cadscene.core.sim3 import Sim3

PLY_TYPES = {
    "char": ("b", 1),
    "uchar": ("B", 1),
    "uint8": ("B", 1),
    "int8": ("b", 1),
    "short": ("h", 2),
    "ushort": ("H", 2),
    "int16": ("h", 2),
    "uint16": ("H", 2),
    "int": ("i", 4),
    "uint": ("I", 4),
    "int32": ("i", 4),
    "uint32": ("I", 4),
    "float": ("f", 4),
    "float32": ("f", 4),
    "double": ("d", 8),
    "float64": ("d", 8),
}


def _parse_header(raw: bytes) -> tuple[str, int, list[tuple[str, str]], int]:
    marker = b"end_header"
    end = raw.find(marker)
    if end < 0:
        raise ValueError("PLY header 缺少 end_header。")
    header_end = raw.find(b"\n", end)
    header_end = len(raw) if header_end < 0 else header_end + 1
    header = raw[:header_end].decode("ascii", errors="ignore").splitlines()
    if not header or header[0].strip() != "ply":
        raise ValueError("仅支持 PLY 文件。")
    fmt = ""
    vertex_count = 0
    props: list[tuple[str, str]] = []
    in_vertex = False
    for line in header:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "format":
            fmt = parts[1]
        elif parts[:2] == ["element", "vertex"]:
            vertex_count = int(parts[2])
            in_vertex = True
        elif parts[0] == "element":
            in_vertex = False
        elif in_vertex and parts[0] == "property" and len(parts) >= 3:
            if parts[1] == "list":
                raise ValueError("暂不支持 PLY list property。")
            props.append((parts[2], parts[1]))
    return fmt, vertex_count, props, header_end


def _extract_xyz_rgb(data: np.ndarray, names: list[str]) -> tuple[np.ndarray, Optional[np.ndarray]]:
    if not all(name in names for name in ("x", "y", "z")):
        raise ValueError("PLY 必须包含 x/y/z property。")
    if len(data) == 0:
        return np.zeros((0, 3), dtype=np.float64), None
    points = data[:, [names.index("x"), names.index("y"), names.index("z")]].astype(np.float64)
    if all(name in names for name in ("red", "green", "blue")):
        colors = data[:, [names.index("red"), names.index("green"), names.index("blue")]].astype(np.uint8)
        return points, colors
    return points, None


def load_ply_ascii(path: str | Path) -> tuple[np.ndarray, Optional[np.ndarray]]:
    raw = Path(path).read_bytes()
    fmt, _vertex_count, props, header_end = _parse_header(raw)
    if fmt != "ascii":
        raise ValueError(f"load_ply_ascii 仅支持 ascii，当前为 {fmt}。")
    names = [name for name, _typ in props]
    rows = [line.split() for line in raw[header_end:].decode("utf-8", errors="ignore").splitlines() if line.strip()]
    if not rows:
        return np.zeros((0, 3), dtype=np.float64), None
    data = np.asarray([[float(value) for value in row[: len(names)]] for row in rows], dtype=np.float64)
    return _extract_xyz_rgb(data, names)


def load_ply(path: str | Path) -> tuple[np.ndarray, Optional[np.ndarray]]:
    raw = Path(path).read_bytes()
    fmt, vertex_count, props, header_end = _parse_header(raw)
    if fmt == "ascii":
        return load_ply_ascii(path)
    if fmt == "binary_big_endian":
        raise ValueError("暂不支持 binary_big_endian PLY，请先转换为 ascii 或 binary_little_endian。")
    if fmt != "binary_little_endian":
        raise ValueError(f"不支持的 PLY format: {fmt}")
    names = [name for name, _typ in props]
    fmt_chars: list[str] = []
    row_size = 0
    for _name, typ in props:
        if typ not in PLY_TYPES:
            raise ValueError(f"不支持的 PLY property 类型: {typ}")
        char, size = PLY_TYPES[typ]
        fmt_chars.append(char)
        row_size += size
    unpacker = struct.Struct("<" + "".join(fmt_chars))
    body = raw[header_end:]
    rows = []
    for i in range(vertex_count):
        start = i * row_size
        rows.append(unpacker.unpack(body[start : start + row_size]))
    data = np.asarray(rows, dtype=np.float64) if rows else np.zeros((0, len(names)), dtype=np.float64)
    return _extract_xyz_rgb(data, names)


def apply_sim3(points: np.ndarray, sim3: Sim3) -> np.ndarray:
    return sim3.apply(points)


def sample_indices(count: int, max_points: int, mode: str = "uniform", seed: int = 0) -> np.ndarray:
    if max_points <= 0 or count <= max_points:
        return np.arange(count, dtype=np.int64)
    if mode == "random":
        rng = np.random.default_rng(seed)
        return np.sort(rng.choice(count, size=max_points, replace=False))
    step = count / float(max_points)
    return np.asarray([int(i * step) for i in range(max_points)], dtype=np.int64)
