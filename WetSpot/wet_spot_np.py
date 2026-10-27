# Fusion360API Python module
"""_build_columns (ラスタライズ) の numpy 版カーネル。

Fusion API (``adsk.*``) には一切依存しない。``verts`` / ``tris`` / ``grid``
(``nx, ny, cs, ox, oy`` を持つオブジェクト) のみを受け取るため、Fusion 環境外
(ローカル python + numpy) でも直接ユニットテストできる。

浮動小数点の一致ルール:
``matmul`` / ``dot`` / ``sum`` は使わない。式の形・演算の順序を
``pool_engine._build_columns_python`` と完全に同じにし、要素ごとの ufunc 連鎖
で計算する。これにより ``_build_columns_python`` とビット単位で一致する。
"""

from __future__ import annotations
from typing import Any

# 候補ペア (三角形 x セル) が一括で膨れ上がらないよう、三角形をチャンク分割する
# 際の目安。巨大な平面が数万セルを覆うケースの保険。
_MAX_CANDIDATE_PAIRS_PER_CHUNK = 2_000_000


def build_columns(
    np: Any,
    verts: list[tuple[float, float, float]],
    tris: list[tuple[int, int, int]],
    grid: Any,
) -> tuple:
    """各セルの (top_z, bottom_z, solid, ok) を numpy 配列 (長さ nx*ny) で返す。

    ``_build_columns_python`` が返す ``[(intervals, ok), ...]`` から
    ``_columns_from_intervals`` で導出できる値と完全に一致する。
    """
    nx, ny, cs, ox, oy = grid.nx, grid.ny, grid.cs, grid.ox, grid.oy
    n_cells = nx * ny

    top_z = np.full(n_cells, grid.base_z, dtype=np.float64)
    bottom_z = np.full(n_cells, grid.base_z, dtype=np.float64)
    solid = np.zeros(n_cells, dtype=bool)
    # 交差が 1 つも無いセルは元コードの _intervals([]) と同じく ok=True。
    ok = np.ones(n_cells, dtype=bool)

    if len(tris) == 0:
        return top_z, bottom_z, solid, ok

    verts_np = np.asarray(verts, dtype=np.float64)
    tris_np = np.asarray(tris, dtype=np.int64)

    a = verts_np[tris_np[:, 0]]
    b = verts_np[tris_np[:, 1]]
    c = verts_np[tris_np[:, 2]]
    a0, a1, a2 = a[:, 0], a[:, 1], a[:, 2]
    b0, b1, b2 = b[:, 0], b[:, 1], b[:, 2]
    c0, c1, c2 = c[:, 0], c[:, 1], c[:, 2]

    ux, uy, uz = b0 - a0, b1 - a1, b2 - a2
    vx, vy, vz = c0 - a0, c1 - a1, c2 - a2
    n_x = uy * vz - uz * vy
    n_y = uz * vx - ux * vz
    n_z = ux * vy - uy * vx
    n_len = np.sqrt(n_x * n_x + n_y * n_y + n_z * n_z)

    valid = (n_len > 0.0) & (np.abs(n_z) >= 1e-9 * n_len)
    idx = np.nonzero(valid)[0]
    if idx.size == 0:
        return top_z, bottom_z, solid, ok

    a0, a1, a2 = a0[idx], a1[idx], a2[idx]
    b0, b1, c0, c1 = b0[idx], b1[idx], c0[idx], c1[idx]
    n_x, n_y, n_z = n_x[idx], n_y[idx], n_z[idx]

    # CCW 揃え (元コード: n_z > 0 なら (a,b,c) のまま、それ以外は (a,c,b))
    flip = n_z <= 0.0
    ra0, ra1 = a0, a1
    rb0 = np.where(flip, c0, b0)
    rb1 = np.where(flip, c1, b1)
    rc0 = np.where(flip, b0, c0)
    rc1 = np.where(flip, b1, c1)

    i0 = np.ceil((np.minimum(np.minimum(a0, b0), c0) - ox) / cs - 0.5)
    i1 = np.floor((np.maximum(np.maximum(a0, b0), c0) - ox) / cs - 0.5)
    j0 = np.ceil((np.minimum(np.minimum(a1, b1), c1) - oy) / cs - 0.5)
    j1 = np.floor((np.maximum(np.maximum(a1, b1), c1) - oy) / cs - 0.5)

    i0 = np.maximum(0, i0).astype(np.int64)
    i1 = np.minimum(nx - 1, i1).astype(np.int64)
    j0 = np.maximum(0, j0).astype(np.int64)
    j1 = np.minimum(ny - 1, j1).astype(np.int64)

    bbox_idx = np.nonzero((i0 <= i1) & (j0 <= j1))[0]
    if bbox_idx.size == 0:
        return top_z, bottom_z, solid, ok

    ra0, ra1 = ra0[bbox_idx], ra1[bbox_idx]
    rb0, rb1 = rb0[bbox_idx], rb1[bbox_idx]
    rc0, rc1 = rc0[bbox_idx], rc1[bbox_idx]
    a0, a1, a2 = a0[bbox_idx], a1[bbox_idx], a2[bbox_idx]
    n_x, n_y, n_z = n_x[bbox_idx], n_y[bbox_idx], n_z[bbox_idx]
    i0, i1 = i0[bbox_idx], i1[bbox_idx]
    j0, j1 = j0[bbox_idx], j1[bbox_idx]

    e0x, e0y = rc0 - rb0, rc1 - rb1
    e1x, e1y = ra0 - rc0, ra1 - rc1
    e2x, e2y = rb0 - ra0, rb1 - ra1
    neg_tol = -(np.abs(n_z) * 1e-9)

    width = i1 - i0 + 1
    counts = width * (j1 - j0 + 1)

    cell_ids_parts = []
    z_parts = []

    total_tris = len(counts)
    pos = 0
    while pos < total_tris:
        end = pos
        acc = 0
        while end < total_tris:
            cnt = int(counts[end])
            if acc > 0 and acc + cnt > _MAX_CANDIDATE_PAIRS_PER_CHUNK:
                break
            acc += cnt
            end += 1
        _process_chunk(
            np,
            pos,
            end,
            ra0,
            ra1,
            rb0,
            rb1,
            rc0,
            rc1,
            a0,
            a1,
            a2,
            n_x,
            n_y,
            n_z,
            e0x,
            e0y,
            e1x,
            e1y,
            e2x,
            e2y,
            neg_tol,
            i0,
            j0,
            width,
            counts,
            ox,
            oy,
            cs,
            nx,
            cell_ids_parts,
            z_parts,
        )
        pos = end

    if not cell_ids_parts:
        return top_z, bottom_z, solid, ok

    cell_id = np.concatenate(cell_ids_parts)
    z = np.concatenate(z_parts)

    z_tol = cs * 1e-6
    _reduce_to_columns(np, cell_id, z, z_tol, top_z, bottom_z, solid, ok)
    return top_z, bottom_z, solid, ok


def _process_chunk(
    np: Any,
    pos: Any,
    end: Any,
    ra0: Any,
    ra1: Any,
    rb0: Any,
    rb1: Any,
    rc0: Any,
    rc1: Any,
    a0: Any,
    a1: Any,
    a2: Any,
    n_x: Any,
    n_y: Any,
    n_z: Any,
    e0x: Any,
    e0y: Any,
    e1x: Any,
    e1y: Any,
    e2x: Any,
    e2y: Any,
    neg_tol: Any,
    i0: Any,
    j0: Any,
    width: Any,
    counts: Any,
    ox: Any,
    oy: Any,
    cs: Any,
    nx: Any,
    cell_ids_parts: Any,
    z_parts: Any,
) -> tuple:
    """三角形のチャンクとセルの交差を計算する。"""
    sl = slice(pos, end)
    counts_sl = counts[sl]
    total = int(counts_sl.sum())
    if total == 0:
        return

    tri_local = np.repeat(np.arange(end - pos), counts_sl)
    starts = np.cumsum(counts_sl) - counts_sl
    offset = np.arange(total) - np.repeat(starts, counts_sl)
    width_per = np.repeat(width[sl], counts_sl)
    local_i = offset % width_per
    local_j = offset // width_per

    i = np.repeat(i0[sl], counts_sl) + local_i
    j = np.repeat(j0[sl], counts_sl) + local_j

    px = ox + (i.astype(np.float64) + 0.5) * cs
    py = oy + (j.astype(np.float64) + 0.5) * cs

    def gather(arr: Any) -> Any:
        """有効なセルの値を一次元配列へ集める。"""
        return arr[sl][tri_local]

    rb0_c, rb1_c = gather(rb0), gather(rb1)
    rc0_c, rc1_c = gather(rc0), gather(rc1)
    ra0_c, ra1_c = gather(ra0), gather(ra1)
    a0_c, a1_c, a2_c = gather(a0), gather(a1), gather(a2)
    n_x_c, n_y_c, n_z_c = gather(n_x), gather(n_y), gather(n_z)
    e0x_c, e0y_c = gather(e0x), gather(e0y)
    e1x_c, e1y_c = gather(e1x), gather(e1y)
    e2x_c, e2y_c = gather(e2x), gather(e2y)
    neg_tol_c = gather(neg_tol)

    w0 = e0x_c * (py - rb1_c) - e0y_c * (px - rb0_c)
    keep = w0 >= neg_tol_c
    w1 = e1x_c * (py - rc1_c) - e1y_c * (px - rc0_c)
    keep &= w1 >= neg_tol_c
    w2 = e2x_c * (py - ra1_c) - e2y_c * (px - ra0_c)
    keep &= w2 >= neg_tol_c

    if not np.any(keep):
        return

    i = i[keep]
    j = j[keep]
    px = px[keep]
    py = py[keep]
    a0_c, a1_c, a2_c = a0_c[keep], a1_c[keep], a2_c[keep]
    n_x_c, n_y_c, n_z_c = n_x_c[keep], n_y_c[keep], n_z_c[keep]

    z = a2_c - (n_x_c * (px - a0_c) + n_y_c * (py - a1_c)) / n_z_c
    cell_id = j * nx + i

    cell_ids_parts.append(cell_id)
    z_parts.append(z)


def _reduce_to_columns(
    np: Any,
    cell_id: Any,
    z: Any,
    z_tol: float,
    top_z: Any,
    bottom_z: Any,
    solid: Any,
    ok: Any,
) -> tuple:
    """交差 z をセルごとにソートし、_intervals() と同じ規則で束ねる。

    大多数のセルは「隣接差が z_tol 以下のペアが無い」ため、そのままベクトル化
    できる (デデュープしても値が変わらない)。ごく少数の衝突セルだけ、元の
    逐次貪欲デデュープを Python ループで再現する。
    """
    order = np.lexsort((z, cell_id))
    sorted_cell = cell_id[order]
    sorted_z = z[order]

    unique_cells, start_idx, counts = np.unique(
        sorted_cell, return_index=True, return_counts=True
    )
    last_idx = start_idx + counts - 1
    even = (counts % 2) == 0
    top_idx = np.where(even, last_idx, np.maximum(last_idx - 1, start_idx))

    solid_mask = counts >= 2
    solid_cells = unique_cells[solid_mask]
    bottom_z[solid_cells] = sorted_z[start_idx[solid_mask]]
    top_z[solid_cells] = sorted_z[top_idx[solid_mask]]
    solid[unique_cells] = counts >= 2
    ok[unique_cells] = even

    if len(sorted_z) <= 1:
        return

    diff = np.diff(sorted_z)
    same_cell = sorted_cell[1:] == sorted_cell[:-1]
    collide = same_cell & (diff <= z_tol)
    if not np.any(collide):
        return

    collision_cells = np.unique(sorted_cell[np.nonzero(collide)[0]])
    starts_map = dict(zip(unique_cells.tolist(), start_idx.tolist()))
    counts_map = dict(zip(unique_cells.tolist(), counts.tolist()))

    for cell in collision_cells.tolist():
        s = starts_map[cell]
        cnt = counts_map[cell]
        zlist = sorted_z[s : s + cnt].tolist()
        zs = [zlist[0]]
        for value in zlist[1:]:
            if value - zs[-1] > z_tol:
                zs.append(value)
        n = len(zs)
        is_ok = (n % 2) == 0
        is_solid = n >= 2
        ok[cell] = is_ok
        solid[cell] = is_solid
        if is_solid:
            bottom_z[cell] = zs[0]
            top_z[cell] = zs[-1] if is_ok else zs[-2]
