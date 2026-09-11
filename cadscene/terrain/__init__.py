"""Terrain elevation controls used by CAD/SRT workflows."""

from .drape import (
    DrapedCadSegments,
    TerrainConflict,
    detect_terrain_conflicts,
    drape_cad_segments,
)
from .tpkg import (
    TerrainControlSet,
    TerrainCoverage,
    TerrainSourceSummary,
    TerrainValidationError,
    inspect_tpkg,
    load_tpkg,
    merge_terrain_controls,
    route_coverage,
)
from .context import TerrainContext, build_terrain_context, write_terrain_context

__all__ = [
    "DrapedCadSegments",
    "TerrainConflict",
    "TerrainControlSet",
    "TerrainCoverage",
    "TerrainSourceSummary",
    "TerrainValidationError",
    "detect_terrain_conflicts",
    "drape_cad_segments",
    "inspect_tpkg",
    "load_tpkg",
    "merge_terrain_controls",
    "route_coverage",
    "TerrainContext",
    "build_terrain_context",
    "write_terrain_context",
]
