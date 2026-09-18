---
name: terrain-model-planner
description: Use when the user wants to turn a real place, coordinates, or a geographic area into a printable topographic/terrain model, choose a scale, check printer fit, split it into modules, or open the TerrainTiles builder.
---

# TerrainTiles

Use TerrainTiles for real-world terrain-to-print workflows.

1. If the user already supplies a WGS84 bounding box, call `plan_terrain_model`.
2. If the user names a place but no bounded area is known, obtain or ask for a sufficiently specific area before planning; do not invent precise boundaries.
3. Use MDT for terrain/ground surface and MDS when the user needs the surface including above-ground structures/vegetation represented by the elevation source.
4. Never claim DGT high-resolution data is currently available merely because it is preferred. Availability is validated during generation and TerrainTiles has automatic fallbacks.
5. Use `render_terrain_builder` when the user needs the map, interactive area selection, detailed settings, generation/validation, or STL/3MF export.
6. Keep scale semantics explicit: `2500` means 1:2500.
7. Do not claim a file has been generated unless the builder/export workflow actually completed.
