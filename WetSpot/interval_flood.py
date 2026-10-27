"""Water levels in the free vertical intervals of an XY raster.

Each node represents an entire open height interval, not a Z voxel.  The
minimax path from the exterior gives the lowest height at which that interval
can drain.  A connected open cavity with no exterior path is left unfilled.
"""

from __future__ import annotations
from typing import Any

import heapq
import math


def find_pools(
    grid: Any,
    columns: list[tuple[list[tuple[float, float]], bool]],
    min_depth: float,
) -> tuple[list[dict], int]:
    """柱の空隙を通る排水経路から水が残る領域を検出する。"""
    nx, ny = grid.nx, grid.ny
    ceiling = grid.top_z
    floor = grid.base_z
    tolerance = max(grid.cs * 1e-8, 1e-10)
    nodes = []  # (column index, lower, upper)
    by_column = [[] for _ in columns]

    for cell, (solids, reliable) in enumerate(columns):
        if not reliable:
            continue
        lower = floor
        for solid_low, solid_high in solids:
            if solid_low > lower + tolerance:
                by_column[cell].append(len(nodes))
                nodes.append((cell, lower, min(solid_low, ceiling)))
            lower = max(lower, solid_high)
        if lower < ceiling - tolerance:
            by_column[cell].append(len(nodes))
            nodes.append((cell, lower, ceiling))

    edges = [[] for _ in nodes]
    for cell in range(nx * ny):
        i, j = cell % nx, cell // nx
        for neighbor in ((cell + 1,) if i + 1 < nx else ()) + (
            (cell + nx,) if j + 1 < ny else ()
        ):
            left, right = by_column[cell], by_column[neighbor]
            ia = ib = 0
            while ia < len(left) and ib < len(right):
                a, b = left[ia], right[ib]
                _ca, low_a, high_a = nodes[a]
                _cb, low_b, high_b = nodes[b]
                passage = max(low_a, low_b)
                if min(high_a, high_b) > passage + tolerance:
                    edges[a].append((b, passage))
                    edges[b].append((a, passage))
                if high_a <= high_b:
                    ia += 1
                if high_b <= high_a:
                    ib += 1

    levels = [math.inf] * len(nodes)
    heap = []

    def seed(node: int, height: float) -> None:
        """排水探索の起点を優先度キューへ追加する。"""
        if height < levels[node]:
            levels[node] = height
            heapq.heappush(heap, (height, node))

    for node, (cell, low, high) in enumerate(nodes):
        i, j = cell % nx, cell // nx
        if i == 0 or j == 0 or i == nx - 1 or j == ny - 1:
            seed(node, low)
        if high >= ceiling - tolerance:
            seed(node, ceiling)
        if low <= floor + tolerance:
            seed(node, floor)

    while heap:
        height, node = heapq.heappop(heap)
        if height != levels[node]:
            continue
        for other, passage in edges[node]:
            candidate = max(height, passage)
            if candidate < levels[other]:
                levels[other] = candidate
                heapq.heappush(heap, (candidate, other))

    wet_top = [min(levels[n], item[2]) for n, item in enumerate(nodes)]
    wet = [
        math.isfinite(levels[n]) and wet_top[n] - item[1] > tolerance
        for n, item in enumerate(nodes)
    ]
    seen = set()
    pools = []
    for first in range(len(nodes)):
        if not wet[first] or first in seen:
            continue
        component = []
        stack = [first]
        seen.add(first)
        while stack:
            node = stack.pop()
            component.append(node)
            for other, _passage in edges[node]:
                if other in seen or not wet[other]:
                    continue
                if (
                    min(wet_top[node], wet_top[other])
                    <= max(nodes[node][1], nodes[other][1]) + tolerance
                ):
                    continue
                seen.add(other)
                stack.append(other)

        level = max(wet_top[n] for n in component)
        lowest = min(nodes[n][1] for n in component)
        if level - lowest <= min_depth:
            continue
        deepest = min(component, key=lambda n: nodes[n][1])
        volume = sum(wet_top[n] - nodes[n][1] for n in component) * grid.cs**2
        roofed_volume = (
            sum(
                wet_top[n] - nodes[n][1]
                for n in component
                if nodes[n][2] < ceiling - tolerance
            )
            * grid.cs**2
        )
        exposed = {
            nodes[n][0]
            for n in component
            if nodes[n][2] >= level - tolerance and levels[n] < nodes[n][2] - tolerance
        }
        seeds = []
        for node in sorted(
            component, key=lambda n: wet_top[n] - nodes[n][1], reverse=True
        )[:8]:
            cell, low, _high = nodes[node]
            seeds.append(
                (grid.cx(cell % nx), grid.cy(cell // nx), (low + wet_top[node]) * 0.5)
            )
        drains = []
        for node in _component_drains(
            component, deepest, nodes, edges, wet_top, tolerance, min_depth
        ):
            drain_cell, low, _high = nodes[node]
            drains.append((grid.cx(drain_cell % nx), grid.cy(drain_cell // nx), low))
        cell = nodes[deepest][0]
        pools.append(
            {
                "water_level": level,
                "max_depth": level - lowest,
                "area": len(exposed) * grid.cs**2,
                "volume": volume,
                "roofed_volume": roofed_volume,
                "floor": (grid.cx(cell % nx), grid.cy(cell // nx), lowest),
                "drains": drains,
                "seeds": seeds,
                "footprint_cells": sorted({nodes[n][0] for n in component}),
                "quad_centers": [
                    (grid.cx(c % nx), grid.cy(c // nx), level) for c in exposed
                ],
                "mode": "interval",
                "cs": grid.cs,
            }
        )
    return pools, len(nodes)


def _component_drains(
    component: list[int],
    deepest: int,
    nodes: list[tuple[int, float, float]],
    edges: list[list[tuple[int, float]]],
    wet_top: list[float],
    tolerance: float,
    min_depth: float,
) -> list[int]:
    """Return drain nodes that empty a pool component; the first is deepest.

    A single drain at the deepest node leaves water in sub-basins separated
    by a saddle.  Flood the component from the drains only, then add the
    deepest node of every remaining wet part deeper than min_depth, and
    repeat.  Parts found in the same round do not affect each other's level.
    """
    members = set(component)
    drains = [deepest]
    while True:
        levels = {}
        heap = []
        for node in drains:
            levels[node] = nodes[node][1]
            heapq.heappush(heap, (nodes[node][1], node))
        while heap:
            height, node = heapq.heappop(heap)
            if height != levels[node]:
                continue
            for other, passage in edges[node]:
                if other not in members:
                    continue
                candidate = max(height, passage)
                if candidate < levels.get(other, math.inf):
                    levels[other] = candidate
                    heapq.heappush(heap, (candidate, other))

        top = {n: min(levels.get(n, math.inf), wet_top[n]) for n in component}
        residual = {n for n in component if top[n] - nodes[n][1] > tolerance}
        added = []
        while residual:
            first = residual.pop()
            part = [first]
            stack = [first]
            while stack:
                node = stack.pop()
                for other, _passage in edges[node]:
                    if other not in residual:
                        continue
                    if (
                        min(top[node], top[other])
                        <= max(nodes[node][1], nodes[other][1]) + tolerance
                    ):
                        continue
                    residual.discard(other)
                    part.append(other)
                    stack.append(other)
            bottom = min(part, key=lambda n: nodes[n][1])
            if max(top[n] for n in part) - nodes[bottom][1] > min_depth:
                added.append(bottom)
        if not added:
            return drains
        drains.extend(added)
