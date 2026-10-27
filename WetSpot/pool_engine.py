# Fusion360API Python module
"""上から水をかけたときに body に水が溜まる領域を求める。

アルゴリズムの核は Priority-Flood (Wang & Liu 法)。
各ノード v の高さを z(v) とし、外部へ至る全経路のうち「経路上の最大高さ」が
最小になる値を level(v) とすると、level(v) > z(v) のとき v は水没している。
実装は Dijkstra と同型で、2.5D 高さ場と柱ごとの空間区間グラフに使う。

Fusion 360 同梱の Python には numpy が含まれないため、純 Python で実装している。
"""

from __future__ import annotations
from typing import Any

import heapq
import json
import math
import os
import time
from collections import namedtuple
from dataclasses import dataclass, field, replace

import adsk
import adsk.core as core
import adsk.fusion as fusion

from . import interval_flood
from . import numpy_loader
from . import wet_spot_np
from .i18n import tr

_INF = float("inf")

# 既定の XY 分割数 (bbox 最長辺方向)。
_DEF_RES_25D = 200
_DEF_RES_INTERVAL = 200

# 保存済み Fusion モデルの生成物を識別するキー。既存モデルとの互換性のため維持する。
_POOL_ATTRIBUTE_GROUP = "water_pool"
_POOL_ATTRIBUTE_NAME = "generated"
_POOL_ATTRIBUTE_VALUE = "v1"
_POOL_FEATURE_NAME = "Wet Spot Pools"
_DRAIN_ATTRIBUTE_NAME = "drain_points"
_DRAIN_SKETCH_NAME = "Wet Spot Drain Points"

# Fusion マテリアルライブラリ内の「水」。表示名ではなく、Fusion が管理する
# 不変IDで参照するので UI の表示言語には依存しない。
_WATER_MATERIAL_LIBRARY_ID = "C1EEA57C-3F56-45FC-B8CB-A9EC46A9994C"
_WATER_MATERIAL_ID = "PrismMaterial-020"
_WATER_DESIGN_MATERIAL_NAME = "Wet Spot Water"

# Fusion 360 API の BRepBody.volume は cm^3。ボディ名は読み取りやすい mm^3 で表示する。
_CUBIC_MILLIMETERS_PER_CUBIC_CENTIMETER = 1000.0

# 差演算用プリズムの寸法 (cm)。
# 輪郭は求まった水面領域より数セル分だけ外側へ広げ、側面が必ず「水面より高い
# 材料」の中を通るようにする。上端は水面からごく僅かに下げ、水面と元ボディの
# 面が一致して差演算が不安定になるのを避ける。下端は最深部より少しだけ深く。
# 曲面境界のラスタ誤差と差演算の余裕を確保する。既定 200 分割の今回のモデルでは
# 5 セル = 約 2.75 mm で、3 セルでは境界側に格子状の取りこぼしが残った。
_OUTLINE_PAD_CELLS = 5
_PRISM_TOP_DROP = 0.001  # 0.01 mm
_PRISM_BOTTOM_MARGIN = 0.01  # 0.1 mm
# BRepBodyDefinition で作ったプリズムの体積と「輪郭面積×深さ」の相対許容差。
# これを外れたら面の向き等の不正とみなし、押し出しへフォールバックする。
_PRISM_VOLUME_TOLERANCE = 1e-4
# 差演算を省略してよいかの bbox 判定の余裕 (cm)。接触だけのボディは差演算する。
_BOX_MARGIN = 1e-4

# 水がセルの角を経由して下りられる地形では、4近傍だけだと排水経路を塞いで
# 偽の窪みを作る。排水探索だけは 8 近傍を使い、水域の連結・輪郭化は 4近傍の
# ままにして、点接続の自己接触プロファイルを作らない。
_CARDINAL_NEIGHBORS = (
    (1, 0),
    (-1, 0),
    (0, 1),
    (0, -1),
)
_DRAIN_NEIGHBORS = _CARDINAL_NEIGHBORS + (
    (1, 1),
    (1, -1),
    (-1, 1),
    (-1, -1),
)


# --------------------------------------------------------------------------
# 段階別の所要時間計測
# --------------------------------------------------------------------------
# どこが遅いのかを実測せずに最適化すると外すので、主要な段階を常時計測しておく。
# 計測は perf_counter の 2 回呼び出しだけなので、処理時間への影響は無視できる。
_timings = {}
_interval_node_count = 0


class _Stage:
    """with ブロックの所要時間を _timings へ積算する。"""

    __slots__ = ("key", "start")

    def __init__(self, key: str) -> None:
        """計測またはグリッドの初期状態を設定する。"""
        self.key = key

    def __enter__(self) -> _Stage:
        """処理時間の計測を開始する。"""
        self.start = time.perf_counter()
        return self

    def __exit__(self, *exc_info) -> bool:
        """経過時間を記録する。"""
        elapsed = time.perf_counter() - self.start
        _timings[self.key] = _timings.get(self.key, 0.0) + elapsed
        return False


def get_last_timings() -> dict:
    """直近の解析・ボディ生成の段階別所要時間 (秒) を返す。

    キーは最初に計測された順に並ぶ。find_wet_spots の呼び出しで初期化され、
    続く create_pool_bodies の分が追記される。
    """
    return dict(_timings)


def format_last_timings() -> list:
    """get_last_timings の内容を表示用の行リストへ整形する。"""
    if not _timings:
        return []
    total = sum(_timings.values())
    lines = [tr("timing_total", seconds=total)]
    for key, seconds in _timings.items():
        share = (seconds / total * 100.0) if total > 0.0 else 0.0
        lines.append(tr("timing_stage", stage=key, seconds=seconds, share=share))
    if _interval_node_count:
        lines.append(tr("timing_nodes", count=_interval_node_count))
    return lines


def _show_progress(message: str, maximum: int) -> core.ProgressDialog:
    """画面中央にプログレスダイアログを表示する。"""
    dialog = core.Application.get().userInterface.createProgressDialog()
    dialog.isCancelButtonShown = False
    dialog.show("Wet Spot", message, 0, maximum, 0)
    adsk.doEvents()
    return dialog


def _set_progress(dialog: Any, value: int) -> None:
    """進捗値を更新して UI イベントを処理する。"""
    if dialog is None:
        return
    # 処理中は UI が再描画されないため、更新毎にイベントを処理させる。
    dialog.progressValue = value
    adsk.doEvents()


def _pool_body_name(index: int, volume_cm3: float) -> str:
    """水たまりボディ名を、体積を mm^3 で含めた形式にする。"""
    volume_mm3 = volume_cm3 * _CUBIC_MILLIMETERS_PER_CUBIC_CENTIMETER
    return "Wet Spot_{:02d}_{:.3f}mm^3".format(index, volume_mm3)


@dataclass
class PoolRegion:
    """水が溜まる領域 1 つ分。"""

    water_level: float  # 水面の高さ (ローカル系 / 重力の逆方向, cm)
    water_plane_origin: core.Point3D  # 水面上のワールド座標の点
    up: core.Vector3D  # 重力の逆方向 (単位ベクトル, ワールド)
    seed_point: core.Point3D  # 確実に水中にあるワールド座標の点
    lowest_point: core.Point3D  # 最深部 (床) のワールド座標
    max_depth: float  # 最大水深 (cm)
    area: float  # 水面の概算面積 (cm^2)
    volume: float  # 概算体積 (cm^3)
    surface_mesh: tuple = field(default_factory=lambda: ([], []))
    """水面を描画するための (ワールド座標の平坦な float 配列, 三角形インデックス)。"""
    outline: tuple = field(default_factory=tuple)
    """水面上の閉じた外周点列 (ワールド座標)。ソリッド生成に使用する。"""
    prism_bottom: float = 0.0
    """差演算用プリズムの下端 (ローカル系の高さ, cm)。最深部より少しだけ深い。"""
    mode: str = "2.5d"
    seed_points: tuple = field(default_factory=tuple)
    cell_size: float = 0.0  # 解析セル幅 (cm)。0 なら水位の自動補正を行わない。
    min_depth: float = 0.01  # 補正後も維持する最小水深 (cm)。
    level_correction: float = 0.0  # 生成時に下げた水位の合計 (cm)。
    drain_points: tuple = field(default_factory=tuple)
    """水を残さず抜くための水抜き点 (ワールド座標)。先頭は lowest_point。"""


@dataclass(frozen=True)
class PoolCreationFailure:
    """候補番号、候補形状、生成失敗理由を対応づけた診断結果。"""

    index: int
    pool: PoolRegion
    reason: str


_DIAGNOSTIC_GROUP_NAME = "Wet Spot failed candidates v1"


def clear_failed_pool_graphics(comp: fusion.Component) -> None:
    """再読み込み後も名前で識別し、診断用の一時表示だけを削除する。"""
    groups = comp.customGraphicsGroups
    for index in range(groups.count - 1, -1, -1):
        group = groups.item(index)
        if group.name == _DIAGNOSTIC_GROUP_NAME:
            group.deleteMe()


def draw_failed_pools(failures: Any, comp: fusion.Component) -> Any:
    """失敗候補の水面、位置マーカー、画面を向く候補番号を一時表示する。"""
    clear_failed_pool_graphics(comp)
    if not failures:
        core.Application.get().activeViewport.refresh()
        return None
    group = comp.customGraphicsGroups.add()
    group.name = _DIAGNOSTIC_GROUP_NAME
    effect = fusion.CustomGraphicsSolidColorEffect.create(
        core.Color.create(255, 140, 0, 255)
    )
    try:
        for failure in failures:
            pool = failure.pool
            coords, indices = pool.surface_mesh
            if indices:
                mesh = group.addMesh(
                    fusion.CustomGraphicsCoordinates.create(coords), indices, [], []
                )
                mesh.color = effect
                mesh.setOpacity(0.8, True)
            point = pool.water_plane_origin
            radius = max(0.03, min(0.2, pool.max_depth * 0.15))
            marker_coords = []
            for axis in range(3):
                for sign in (-1, 1):
                    xyz = [point.x, point.y, point.z]
                    xyz[axis] += sign * radius
                    marker_coords.extend(xyz)
            marker = group.addLines(
                fusion.CustomGraphicsCoordinates.create(marker_coords),
                [0, 1, 2, 3, 4, 5],
                False,
            )
            marker.color = effect
            marker.weight = 3
            transform = core.Matrix3D.create()
            transform.translation = core.Vector3D.create(point.x, point.y, point.z)
            label = group.addText(
                "[{}]".format(failure.index), "Arial", radius * 3, transform
            )
            label.color = effect
            label.billBoarding = fusion.CustomGraphicsBillBoard.create(point)
        core.Application.get().activeViewport.refresh()
        return group
    except Exception:
        group.deleteMe()
        raise


def format_creation_failure(failure: PoolCreationFailure) -> str:
    """失敗候補の番号、位置、理由を表示用の文字列にする。"""
    point = failure.pool.water_plane_origin
    return tr(
        "creation_failure",
        index=failure.index,
        reason=failure.reason,
        x=point.x,
        y=point.y,
        z=point.z,
        level=failure.pool.water_level,
        depth=failure.pool.max_depth,
    )


# --------------------------------------------------------------------------
# 公開 API
# --------------------------------------------------------------------------
def _normalize_bodies(bodies: list[fusion.BRepBody]) -> tuple:
    """単一 BRepBody またはその集合を、空でないタプルに正規化する。"""
    if bodies is None:
        raise ValueError(tr("bodies_unspecified"))

    # Fusion の BRepBody は Python の通常の iterable ではないが、型の proxy が
    # 環境ごとに異なるため isinstance では判定しない。faces を持つものを単体とする。
    if hasattr(bodies, "faces"):
        result = (bodies,)
    else:
        try:
            result = tuple(bodies)
        except TypeError as exc:
            raise TypeError(tr("bodies_type_error")) from exc

    if not result:
        raise ValueError(tr("bodies_empty"))
    for index, body in enumerate(result, 1):
        if not hasattr(body, "faces"):
            raise TypeError(tr("body_type_error", index=index))
    return result


def find_wet_spots(
    body: fusion.BRepBody | list[fusion.BRepBody],
    gravity: core.Vector3D = None,
    resolution: int = 0,
    include_overhang: bool = False,
    min_depth: float = 0.01,
    mesh_cache: dict = None,
    show_progress: bool = True,
) -> list[PoolRegion]:
    """対象ボディ群に上から水をかけたときに水が溜まる領域を返す。

    Args:
        body: 対象 BRepBody、またはその反復可能な集合。単一ボディも従来どおり
            指定できる。複数ボディはメッシュを個別にラスタライズしてから
            ソリッド区間の和集合として扱う。
        gravity: 重力方向ベクトル。None なら (0, 0, -1)。
        resolution: bbox 最長辺の分割数。0 なら自動 (両モードとも200)。
        include_overhang: True なら柱の空間区間グラフで庇の下も検出する。
        min_depth: これ未満の最大水深の領域はノイズとして捨てる (cm)。
        mesh_cache: 重力に依存しない生メッシュをボディ単位で保持する dict。
            同じ dict を渡し続けると、重力変更時のメッシュ化を省ける。
            モデルが変わらない間 (コマンド実行中など) だけ使い回すこと。
        show_progress: False ならプログレスダイアログを出さない。
            executePreview 中に doEvents で再入させないために使う。

    Returns:
        PoolRegion のリスト。体積の大きい順。
    """
    source_bodies = _normalize_bodies(body)
    if gravity is None:
        gravity = core.Vector3D.create(0.0, 0.0, -1.0)

    global _interval_node_count
    _timings.clear()
    _interval_node_count = 0

    progress = _show_progress(tr("progress_analysis"), 4) if show_progress else None
    try:
        ex, ey, ez = _basis(gravity)
        with _Stage("mesh"):
            meshes, verts = _get_meshes(source_bodies, ex, ey, ez, mesh_cache)
        _set_progress(progress, 1)
        if not meshes:
            return []

        if resolution <= 0:
            resolution = _DEF_RES_INTERVAL if include_overhang else _DEF_RES_25D

        grid = _Grid(verts, resolution)
        with _Stage("columns"):
            columns = _build_columns_for_meshes(meshes, grid, include_overhang)
        _set_progress(progress, 2)

        if include_overhang:
            with _Stage("pools_interval"):
                raw, _interval_node_count = interval_flood.find_pools(
                    grid, columns, min_depth
                )
                for region in raw:
                    region["outline"] = _interval_pool_outline(region, columns, grid)
                    region["prism_bottom"] = region["floor"][2] - _PRISM_BOTTOM_MARGIN
        else:
            raw = _pools_25d(grid, columns, min_depth)
            if raw:
                # 2.5D の top_z は、横穴の「屋根」を壁として扱ってしまう。
                # 候補の水位面で空気が外周まで連続するかは、完全な柱区間を
                # 使って別途確認する。通常の高速な NumPy 経路は候補発見まで
                # 維持し、候補がある場合だけこの正確な確認を行う。
                with _Stage("side_drain_check"):
                    interval_columns = _build_columns_for_meshes(
                        meshes, grid, include_overhang=True
                    )
                    raw = _exclude_side_draining_pools(raw, interval_columns, grid)
        _set_progress(progress, 3)

        up = core.Vector3D.create(ez[0], ez[1], ez[2])
        with _Stage("to_pool_region"):
            pools = [_to_pool_region(r, ex, ey, ez, up) for r in raw]
            for pool in pools:
                pool.min_depth = min_depth
        _set_progress(progress, 4)
        pools.sort(key=lambda p: p.volume, reverse=True)
        return pools
    finally:
        if progress is not None:
            progress.hide()


def draw_pools(
    pools: list,
    comp: fusion.Component = None,
    color: tuple = (0, 130, 255),
    opacity: float = 0.55,
) -> fusion.CustomGraphicsGroup:
    """水面を CustomGraphics で描画する。モデル・履歴は一切変更しない。

    color の opacity 成分は CustomGraphics では無視されるため、
    透明度はグループの setOpacity() で指定する。
    """
    app: core.Application = core.Application.get()
    if comp is None:
        comp = app.activeProduct.rootComponent

    group: fusion.CustomGraphicsGroup = comp.customGraphicsGroups.add()
    effect = fusion.CustomGraphicsSolidColorEffect.create(
        core.Color.create(color[0], color[1], color[2], 255)
    )
    for pool in pools:
        coords, indices = pool.surface_mesh
        if not indices:
            continue
        cg_coords = fusion.CustomGraphicsCoordinates.create(coords)
        mesh = group.addMesh(cg_coords, indices, [], [])
        mesh.color = effect

    group.setOpacity(opacity, True)
    app.activeViewport.refresh()
    return group


def create_pool_bodies(
    pools: list,
    source_body: fusion.BRepBody | list[fusion.BRepBody],
    comp: fusion.Component = None,
) -> list:
    """水たまりを池ごとに 1 個の直接 B-Rep ソリッドとして作成する。

    以前この関数で生成したボディだけを先に削除するため、同じモデルで
    再実行しても水たまりは重複しない。パラメトリック設計では、Fusion API
    の要件に従い 1 個の Base Feature に直接編集ボディとして追加される。

    Args:
        pools: :func:`find_wet_spots` が返した PoolRegion のリスト。
        source_body: 水たまりを検出した元 BRepBody、またはその反復可能な集合。
            水プリズムから各ボディのクローンを順番に差し引く。
        comp: 作成先コンポーネント。None ならルートコンポーネント。
    Returns:
        永続化された BRepBody のリスト。pool の並び順と一致する。
    """
    app: core.Application = core.Application.get()
    design: fusion.Design = fusion.Design.cast(app.activeProduct)
    if comp is None:
        comp = design.rootComponent

    create_pool_bodies.last_errors = []
    create_pool_bodies.last_failures = []
    if not pools:
        _delete_generated_pool_geometry(comp)
        create_pool_bodies.last_errors = []
        create_pool_bodies.last_successful_pools = []
        app.activeViewport.refresh()
        return []

    source_bodies = _normalize_bodies(source_body)
    create_pool_bodies.last_successful_pools = []

    progress = _show_progress(tr("progress_creation"), len(pools) + 1)
    try:
        temp_mgr = fusion.TemporaryBRepManager.get()
        # 差演算用の元ボディのクローンは池ごとに作らず、ここで 1 回だけ作る。
        with _Stage("boolean"):
            source_copies = []
            for index, body in enumerate(source_bodies, 1):
                source_copy = temp_mgr.copy(body)
                if source_copy is None:
                    raise RuntimeError(tr("clone_failed", index=index))
                source_copies.append(source_copy)
        transient = []
        successful_pools = []
        errors = []
        for index, pool in enumerate(pools, 1):
            try:
                body = _create_pool_temp_body(temp_mgr, comp, pool, source_copies)
                if body is not None:
                    transient.append(body)
                    successful_pools.append(pool)
            except Exception as exc:
                failure = PoolCreationFailure(index, pool, str(exc))
                create_pool_bodies.last_failures.append(failure)
                errors.append(tr("pool_error", index=index, reason=exc))
            _set_progress(progress, index)
        create_pool_bodies.last_errors = errors

        if not transient:
            # 今回の解析結果で生成できる池がないなら、前回の水ボディを残しては
            # ならない。残すと画面上の形状と「生成 0」の報告が食い違う。
            with _Stage("delete_previous"):
                _delete_generated_pool_geometry(comp)
            create_pool_bodies.last_successful_pools = []
            app.activeViewport.refresh()
            return []

        with _Stage("delete_previous"):
            _delete_generated_pool_geometry(comp)

        base_feature = None
        persisted = []
        stage_persist = _Stage("persist")
        stage_persist.__enter__()
        try:
            if design.designType == fusion.DesignTypes.ParametricDesignType:
                base_feature = comp.features.baseFeatures.add()
                base_feature.name = _POOL_FEATURE_NAME
                base_feature.attributes.add(
                    _POOL_ATTRIBUTE_GROUP, _POOL_ATTRIBUTE_NAME, _POOL_ATTRIBUTE_VALUE
                )
                if not base_feature.startEdit():
                    raise RuntimeError(tr("base_feature_edit_failed"))

            for temp_body in transient:
                if base_feature is None:
                    body = comp.bRepBodies.add(temp_body)
                else:
                    body = comp.bRepBodies.add(temp_body, base_feature)
                if body is None:
                    raise RuntimeError(tr("pool_add_failed"))
                body.attributes.add(
                    _POOL_ATTRIBUTE_GROUP, _POOL_ATTRIBUTE_NAME, _POOL_ATTRIBUTE_VALUE
                )
                persisted.append(body)
        except Exception:
            if base_feature is not None and base_feature.isValid:
                base_feature.finishEdit()
                base_feature.deleteMe()
            else:
                for body in persisted:
                    if body.isValid:
                        body.deleteMe()
            raise
        else:
            if base_feature is not None:
                base_feature.finishEdit()
                # 編集中に add されたものは Base Feature の source body。終了後は
                # Feature が公開する result body を使わないと、後続の属性・外観設定が
                # モデル側へ反映されない。
                persisted = [
                    base_feature.bodies.item(index)
                    for index in range(base_feature.bodies.count)
                ]

        finally:
            stage_persist.__exit__()

        material = _get_water_material(app, design)
        for index, body in enumerate(persisted, 1):
            successful_pools[index - 1].volume = body.volume
            body.name = _pool_body_name(index, body.volume)
            body.attributes.add(
                _POOL_ATTRIBUTE_GROUP, _POOL_ATTRIBUTE_NAME, _POOL_ATTRIBUTE_VALUE
            )
            body.material = material
        create_pool_bodies.last_successful_pools = list(successful_pools)
        _set_progress(progress, len(pools) + 1)

        app.activeViewport.refresh()
        return persisted
    finally:
        progress.hide()


create_pool_bodies.last_errors = []
create_pool_bodies.last_failures = []
create_pool_bodies.last_successful_pools = []


def get_last_creation_errors() -> list:
    """直近の create_pool_bodies で池単位に発生した失敗理由を返す。"""
    return list(create_pool_bodies.last_errors)


def get_last_creation_failures() -> list:
    """直近の候補番号・候補形状・生成失敗理由を返す。"""
    return list(create_pool_bodies.last_failures)


def get_last_created_pools() -> list:
    """直近の create_pool_bodies でB-Rep化・保存できた候補を返す。"""
    return list(create_pool_bodies.last_successful_pools)


def create_drain_points(
    pools: list[PoolRegion], comp: fusion.Component
) -> fusion.Sketch | None:
    """前回の水抜き点スケッチを削除し、pools の水抜き点をスケッチ点として出力する。

    pools が空なら削除だけを行う。点は pools の並び順で追加する。
    drain_points を持たない pool は最深点 1 点だけを出力する。
    """
    _delete_drain_points(comp)
    if not pools:
        return None
    sketch = comp.sketches.add(comp.xYConstructionPlane)
    sketch.name = _DRAIN_SKETCH_NAME
    sketch.attributes.add(
        _POOL_ATTRIBUTE_GROUP, _DRAIN_ATTRIBUTE_NAME, _POOL_ATTRIBUTE_VALUE
    )
    for pool in pools:
        points = getattr(pool, "drain_points", None) or (pool.lowest_point,)
        for point in points:
            # スケッチ平面外の 3D 点もそのまま配置できる。
            sketch.sketchPoints.add(sketch.modelToSketchSpace(point))
    return sketch


def _delete_drain_points(comp: fusion.Component) -> None:
    """このモジュールが作成した水抜き点スケッチだけを削除する。"""
    sketches = comp.sketches
    for index in range(sketches.count - 1, -1, -1):
        sketch = sketches.item(index)
        marker = sketch.attributes.itemByName(
            _POOL_ATTRIBUTE_GROUP, _DRAIN_ATTRIBUTE_NAME
        )
        if marker and marker.value == _POOL_ATTRIBUTE_VALUE:
            sketch.deleteMe()


class CarrierContactError(RuntimeError):
    """解析用輪郭・底面で切れた水域。水位の格子誤差補正の対象。"""


def _lower_pool_level(pool: PoolRegion, drop: float) -> Any:
    """候補の水位と輪郭を指定量だけ下げる。"""

    def shift(point: Any) -> Any:
        """点を指定した水位差だけ移動する。"""
        return core.Point3D.create(
            point.x - pool.up.x * drop,
            point.y - pool.up.y * drop,
            point.z - pool.up.z * drop,
        )

    coords, indices = pool.surface_mesh
    moved_coords = [
        value - (pool.up.x, pool.up.y, pool.up.z)[i % 3] * drop
        for i, value in enumerate(coords)
    ]
    return replace(
        pool,
        water_level=pool.water_level - drop,
        max_depth=pool.max_depth - drop,
        water_plane_origin=shift(pool.water_plane_origin),
        outline=tuple(shift(point) for point in pool.outline),
        surface_mesh=(moved_coords, list(indices)),
        level_correction=pool.level_correction + drop,
    )


def _create_pool_temp_body(
    temp_mgr: fusion.TemporaryBRepManager,
    comp: fusion.Component,
    pool: PoolRegion,
    source_bodies: list[fusion.BRepBody],
) -> fusion.BRepBody:
    """水位の格子誤差を補正しながら候補の一時ボディを作る。"""
    try:
        return _create_pool_temp_body_once(temp_mgr, comp, pool, source_bodies)
    except CarrierContactError as original_error:
        # セル中心の水位は連続形状の排水高さより僅かに高くなり得る。
        # 深さの大半を失う補正や、解析解像度を超えた補正は行わない。
        limit = min(
            pool.cell_size * 0.5,
            pool.max_depth * 0.25,
            pool.max_depth - pool.min_depth - _PRISM_TOP_DROP,
        )
        if pool.mode != "interval" or limit <= _PRISM_TOP_DROP:
            raise
        trial = _lower_pool_level(pool, limit)
        try:
            best_body = _create_pool_temp_body_once(
                temp_mgr, comp, trial, source_bodies
            )
        except RuntimeError:
            raise original_error from None
        best_pool = trial
        low, high = 0.0, limit
        # 有効なシード水域が残る側から、補正量を4回の二分探索で縮める。
        for _ in range(4):
            drop = (low + high) * 0.5
            trial = _lower_pool_level(pool, drop)
            try:
                body = _create_pool_temp_body_once(temp_mgr, comp, trial, source_bodies)
            except RuntimeError:
                low = drop
            else:
                high = drop
                best_body, best_pool = body, trial
        for name in (
            "water_level",
            "max_depth",
            "water_plane_origin",
            "outline",
            "surface_mesh",
            "level_correction",
        ):
            setattr(pool, name, getattr(best_pool, name))
        return best_body


def _create_pool_temp_body_once(
    temp_mgr: fusion.TemporaryBRepManager,
    comp: fusion.Component,
    pool: PoolRegion,
    source_bodies: tuple,
) -> fusion.BRepBody:
    """輪郭プリズムから各元ボディのクローンを順番に差し引く。

    source_bodies は create_pool_bodies が 1 回だけ作った一時クローン。
    booleanOperation は tool 側を変更しないので、全池で使い回す。
    """
    if len(pool.outline) < 3:
        raise RuntimeError(tr("outline_failed"))

    with _Stage("prism"):
        prism = _create_outline_prism(comp, temp_mgr, pool)
    with _Stage("boolean"):
        for index, source_copy in enumerate(source_bodies, 1):
            # 離れているボディとの差演算はプリズムを変えないので省略する。
            if _boxes_disjoint(prism, source_copy):
                continue
            if not temp_mgr.booleanOperation(
                prism, source_copy, fusion.BooleanTypes.DifferenceBooleanType
            ):
                raise RuntimeError(tr("boolean_failed", index=index))
    with _Stage("lump"):
        kept = _keep_largest_lump(
            temp_mgr,
            prism,
            pool.seed_point,
            getattr(pool, "seed_points", ()),
            require_seed=pool.mode == "interval",
        )
        if pool.mode == "interval":
            _reject_carrier_contact(kept, pool)
        return kept


def _keep_largest_lump(
    temp_mgr: fusion.TemporaryBRepManager,
    body: fusion.BRepBody,
    seed_point: core.Point3D = None,
    seed_points: tuple = (),
    require_seed: bool = False,
) -> fusion.BRepBody:
    """水域シードを含む塊、判定できなければ最大の塊だけを残す。

    _OUTLINE_PAD_CELLS で輪郭を広げた結果、本体の水たまりの周囲に小さな
    かけらが同居することがある。これらは 1 個の BRepBody でありながら
    互いに離れた lump になる。屋根下では上下に別の空間が重なるため、
    まず解析済み水域のシード点を含むものを採用する。
    """
    lumps = body.lumps
    if lumps.count <= 1 and not require_seed:
        return body

    selected_lump = None
    if seed_point is not None or seed_points:
        inside = fusion.PointContainment.PointInsidePointContainment
        on_boundary = fusion.PointContainment.PointOnPointContainment
        candidates = tuple(seed_points) or (seed_point,)
        for point in candidates:
            for index in range(lumps.count):
                lump = lumps.item(index)
                try:
                    containment = lump.pointContainment(point)
                except Exception:
                    continue
                if containment in (inside, on_boundary):
                    selected_lump = lump
                    break
            if selected_lump is not None:
                break

    if selected_lump is None and not require_seed:
        best_volume = -_INF
        for index in range(lumps.count):
            lump = lumps.item(index)
            if lump.volume > best_volume:
                selected_lump = lump
                best_volume = lump.volume

    if selected_lump is None:
        raise RuntimeError(tr("seed_solid_missing"))

    if lumps.count == 1:
        return body

    faces = [
        selected_lump.faces.item(index) for index in range(selected_lump.faces.count)
    ]
    # TemporaryBRepManager.copy は BRepLump を入力としてサポートしない。
    # 選択した lump の面だけを残すことで、対応APIだけを使って同じ body を分離する。
    if not faces or not temp_mgr.deleteFaces(faces, False):
        raise RuntimeError(tr("solid_split_failed"))
    return body


def _pool_local_outline(pool: PoolRegion) -> Any:
    """Return the analysis basis and outline XY coordinates in that basis."""
    ex, ey, ez = _basis(core.Vector3D.create(-pool.up.x, -pool.up.y, -pool.up.z))
    local = [
        (
            p.x * ex[0] + p.y * ex[1] + p.z * ex[2],
            p.x * ey[0] + p.y * ey[1] + p.z * ey[2],
        )
        for p in pool.outline
    ]
    return ex, ey, ez, local


def _reject_carrier_contact(body: fusion.BRepBody, pool: PoolRegion) -> Any:
    """Reject water cut by the artificial outline or its bottom plane."""
    ex, ey, ez, local = _pool_local_outline(pool)
    x0, x1 = min(p[0] for p in local), max(p[0] for p in local)
    tolerance = max((x1 - x0) * 1e-7, 1e-7)
    side_contact = bottom_contact = False
    for index in range(body.vertices.count):
        p = body.vertices.item(index).geometry
        x = p.x * ex[0] + p.y * ex[1] + p.z * ex[2]
        y = p.x * ey[0] + p.y * ey[1] + p.z * ey[2]
        z = p.x * ez[0] + p.y * ez[1] + p.z * ez[2]
        if _point_on_outline(x, y, local, tolerance):
            side_contact = True
        if abs(z - pool.prism_bottom) <= tolerance:
            bottom_contact = True
    if side_contact or bottom_contact:
        reason = (
            tr("boundary_side_and_bottom")
            if side_contact and bottom_contact
            else tr("boundary_side")
            if side_contact
            else tr("boundary_bottom")
        )
        raise CarrierContactError(tr("boundary_contact", reason=reason))


def _create_outline_prism(
    comp: fusion.Component,
    temp_mgr: fusion.TemporaryBRepManager,
    pool: PoolRegion,
) -> fusion.BRepBody:
    """水面下のプリズムを一時ボディとして作り、水面座標へ配置する。"""
    # find_wet_spots と同じ規則で水面ローカル座標系を再現する。
    ex, ey, ez, local_outline = _pool_local_outline(pool)
    length_dir = core.Vector3D.create(ex[0], ex[1], ex[2])
    width_dir = core.Vector3D.create(ey[0], ey[1], ey[2])

    # 水面をごく僅かに下げた位置から、最深部より少し深い位置まで。
    # 以前はボディ下端まで押し出していたため、差演算後にボディ下方の
    # 空間がまるごと残ってしまっていた。
    top = pool.water_level - _PRISM_TOP_DROP
    depth = top - pool.prism_bottom
    if depth <= 0.0:
        raise RuntimeError(tr("prism_height_failed"))

    prism = _create_prism_by_definition(local_outline, depth)
    if prism is None:
        prism = _create_prism_by_extrude(comp, temp_mgr, local_outline, depth)

    origin = _world_point((0.0, 0.0, top), ex, ey, ez)
    transform = core.Matrix3D.create()
    transform.setWithCoordinateSystem(origin, length_dir, width_dir, pool.up)
    if prism is None or not temp_mgr.transform(prism, transform):
        raise RuntimeError(tr("prism_transform_failed"))
    return prism


def _create_prism_by_definition(local_outline: Any, depth: float) -> Any:
    """BRepBodyDefinition で上面 z=0・下面 z=-depth の角柱を直接作る。

    スケッチや押し出しフィーチャーを使わないので、タイムラインの再計算が
    走らない。作れなかった場合や体積が輪郭面積×深さと合わない場合は
    None を返し、呼び出し側で押し出しへフォールバックさせる。
    """
    points = list(local_outline)
    area = _signed_area(points)
    if area < 0.0:
        # 上面から見て反時計回りに揃え、面の外向き法線と coedge の向きを決める。
        points.reverse()
        area = -area
    count = len(points)
    if count < 3 or area <= 0.0:
        return None

    try:
        definition = fusion.BRepBodyDefinition.create()
        top_points = [core.Point3D.create(x, y, 0.0) for x, y in points]
        bottom_points = [core.Point3D.create(x, y, -depth) for x, y in points]
        top_vertices = [definition.createVertexDefinition(p) for p in top_points]
        bottom_vertices = [definition.createVertexDefinition(p) for p in bottom_points]

        def line_edge(vertices: Any, line_points: Any, start: int, end: int) -> Any:
            """頂点間に直線エッジの定義を追加する。"""
            return definition.createEdgeDefinitionByCurve(
                vertices[start],
                vertices[end],
                core.Line3D.create(line_points[start], line_points[end]),
            )

        following = [(index + 1) % count for index in range(count)]
        top_edges = [
            line_edge(top_vertices, top_points, i, following[i]) for i in range(count)
        ]
        bottom_edges = [
            line_edge(bottom_vertices, bottom_points, i, following[i])
            for i in range(count)
        ]
        # 縦エッジは下から上へ。
        side_edges = [
            definition.createEdgeDefinitionByCurve(
                bottom_vertices[i],
                top_vertices[i],
                core.Line3D.create(bottom_points[i], top_points[i]),
            )
            for i in range(count)
        ]

        shell = definition.lumpDefinitions.add().shellDefinitions.add()

        def add_face(origin: core.Point3D, normal: core.Vector3D, coedges: Any) -> Any:
            """面と輪郭エッジをシェルへ追加する。"""
            normal.normalize()
            face = shell.faceDefinitions.add(core.Plane.create(origin, normal), False)
            loop = face.loopDefinitions.add()
            for edge, opposed in coedges:
                loop.bRepCoEdgeDefinitions.add(edge, opposed)

        # 各面のループは外側から見て反時計回り。
        add_face(
            top_points[0],
            core.Vector3D.create(0.0, 0.0, 1.0),
            [(edge, False) for edge in top_edges],
        )
        add_face(
            bottom_points[0],
            core.Vector3D.create(0.0, 0.0, -1.0),
            [(edge, True) for edge in reversed(bottom_edges)],
        )
        for i in range(count):
            j = following[i]
            dx = points[j][0] - points[i][0]
            dy = points[j][1] - points[i][1]
            add_face(
                bottom_points[i],
                core.Vector3D.create(dy, -dx, 0.0),
                [
                    (bottom_edges[i], False),
                    (side_edges[j], False),
                    (top_edges[i], True),
                    (side_edges[i], True),
                ],
            )
        body = definition.createBody()
    except Exception:
        return None

    if body is None or not body.isSolid or body.lumps.count != 1:
        return None
    expected = area * depth
    if abs(body.volume - expected) > expected * _PRISM_VOLUME_TOLERANCE:
        return None
    return body


def _create_prism_by_extrude(
    comp: fusion.Component, temp_mgr: Any, local_outline: Any, depth: float
) -> Any:
    """一時スケッチを押し出して上面 z=0 の角柱を作り、直後に消去する。"""
    sketch = None
    extrude = None
    prism = None
    try:
        # XY 平面上で作ってから変換するため、任意の重力方向でも補助平面を残さない。
        sketch = comp.sketches.add(comp.xYConstructionPlane)
        local_points = [core.Point3D.create(x, y, 0.0) for x, y in local_outline]
        # 輪郭は直交グリッド由来の多角形なので、フィッテッドスプラインだと
        # 鋭い凹角でオーバーシュートして自己交差し、プロファイルが単一に
        # ならないことがある。実際の形状どおり直線で結ぶ。
        lines = sketch.sketchCurves.sketchLines
        count = len(local_points)
        for index in range(count):
            start = local_points[index]
            end = local_points[(index + 1) % count]
            if lines.addByTwoPoints(start, end) is None:
                raise RuntimeError(tr("outline_line_failed"))
        if sketch.profiles.count != 1:
            raise RuntimeError(tr("outline_profile_failed"))

        extent = fusion.DistanceExtentDefinition.create(
            core.ValueInput.createByReal(depth)
        )
        extrude_input = comp.features.extrudeFeatures.createInput(
            sketch.profiles.item(0), fusion.FeatureOperations.NewBodyFeatureOperation
        )
        extrude_input.setOneSideExtent(
            extent, fusion.ExtentDirections.NegativeExtentDirection
        )
        extrude = comp.features.extrudeFeatures.add(extrude_input)
        if extrude is None or extrude.bodies.count != 1:
            raise RuntimeError(tr("prism_extrude_failed"))
        prism = temp_mgr.copy(extrude.bodies.item(0))
    finally:
        if extrude is not None and extrude.isValid:
            try:
                extrude.deleteMe()
            except Exception:
                pass
        if sketch is not None and sketch.isValid:
            try:
                sketch.deleteMe()
            except Exception:
                pass
    return prism


def _boxes_disjoint(body_a: fusion.BRepBody, body_b: fusion.BRepBody) -> bool:
    """2 ボディの bbox が余裕込みでも離れていれば True。

    bbox が取得できない場合は False を返し、従来どおり差演算させる。
    """
    box_a = getattr(body_a, "boundingBox", None)
    box_b = getattr(body_b, "boundingBox", None)
    if box_a is None or box_b is None:
        return False
    for axis in "xyz":
        if getattr(box_a.minPoint, axis) > getattr(box_b.maxPoint, axis) + _BOX_MARGIN:
            return True
        if getattr(box_b.minPoint, axis) > getattr(box_a.maxPoint, axis) + _BOX_MARGIN:
            return True
    return False


def _prism_depth(source_body: fusion.BRepBody, pool: PoolRegion) -> float:
    """元ボディ全体より下まで届く、安全側のプリズム深さを返す。"""
    box = source_body.boundingBox
    bottom = min(
        x * pool.up.x + y * pool.up.y + z * pool.up.z
        for x in (box.minPoint.x, box.maxPoint.x)
        for y in (box.minPoint.y, box.maxPoint.y)
        for z in (box.minPoint.z, box.maxPoint.z)
    )
    return max(pool.max_depth, pool.water_level - bottom) + max(
        0.01, pool.max_depth * 0.05
    )


def _delete_generated_pool_geometry(comp: fusion.Component) -> None:
    """このモジュールが付与した属性を持つ前回結果だけを削除する。"""
    # パラメトリック設計では Feature ごと消すと、水ボディとタイムライン項目を
    # まとめて置換できる。逆順で走査してインデックスのずれを避ける。
    features = comp.features.baseFeatures
    for index in range(features.count - 1, -1, -1):
        feature = features.item(index)
        marker = feature.attributes.itemByName(
            _POOL_ATTRIBUTE_GROUP, _POOL_ATTRIBUTE_NAME
        )
        if marker and marker.value == _POOL_ATTRIBUTE_VALUE:
            feature.deleteMe()

    # ダイレクト設計の結果、および途中で失敗して Feature に属さなかった
    # ボディを削除する。名前だけでは判定しないのでユーザーのボディは残る。
    bodies = comp.bRepBodies
    for index in range(bodies.count - 1, -1, -1):
        body = bodies.item(index)
        marker = body.attributes.itemByName(_POOL_ATTRIBUTE_GROUP, _POOL_ATTRIBUTE_NAME)
        if marker and marker.value == _POOL_ATTRIBUTE_VALUE:
            body.deleteMe()


def _get_water_material(app: core.Application, design: fusion.Design) -> core.Material:
    """デザイン内の水マテリアルを ID 指定で取得または追加する。"""
    material = design.materials.itemById(_WATER_MATERIAL_ID)
    if material is not None:
        return material

    library = app.materialLibraries.itemById(_WATER_MATERIAL_LIBRARY_ID)
    source = library.materials.itemById(_WATER_MATERIAL_ID) if library else None
    if source is None:
        raise RuntimeError(
            tr("water_material_missing")
            + " (libraryId={}, materialId={})".format(
                _WATER_MATERIAL_LIBRARY_ID, _WATER_MATERIAL_ID
            )
        )

    material = design.materials.addByCopy(source, _WATER_DESIGN_MATERIAL_NAME)
    if material is None:
        raise RuntimeError(tr("water_material_add_failed"))
    return material


# --------------------------------------------------------------------------
# 座標系
# --------------------------------------------------------------------------
def _basis(gravity: core.Vector3D) -> tuple:
    """重力方向から正規直交基底を作る。ez が「上」(重力の逆)。"""
    g = gravity.copy()
    if not g.normalize():
        raise ValueError(tr("gravity_zero_error"))

    ez = (-g.x, -g.y, -g.z)
    helper = (1.0, 0.0, 0.0) if abs(ez[0]) < 0.9 else (0.0, 1.0, 0.0)
    ex = _normalized(_cross(helper, ez))
    ey = _cross(ez, ex)
    return ex, ey, ez


def _cross(a: Any, b: Any) -> tuple:
    """二つのベクトルの外積を求める。"""
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _normalized(v: Any) -> tuple:
    """ベクトルを単位長に正規化する。"""
    n = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    return (v[0] / n, v[1] / n, v[2] / n)


def _world_point(
    p: tuple[float, float, float],
    ex: tuple[float, float, float],
    ey: tuple[float, float, float],
    ez: tuple[float, float, float],
) -> core.Point3D:
    """ローカル座標 (x, y, z) をワールドの Point3D へ戻す。"""
    x, y, z = p
    return core.Point3D.create(
        x * ex[0] + y * ey[0] + z * ez[0],
        x * ex[1] + y * ey[1] + z * ez[1],
        x * ex[2] + y * ey[2] + z * ez[2],
    )


# --------------------------------------------------------------------------
# メッシュ取得
# --------------------------------------------------------------------------
def _calc_mesh(ent: Any) -> Any:
    """ボディの三角形メッシュを取得する。"""
    calc = ent.meshManager.createMeshCalculator()
    calc.setQuality(fusion.TriangleMeshQualityOptions.HighQualityTriangleMesh)
    return calc.calculate()


def _body_key(body: fusion.BRepBody) -> tuple:
    """同じボディの同じ occurrence だけが一致する、メッシュキャッシュのキー。"""
    occurrence = body.assemblyContext
    native = body.nativeObject or body
    return (occurrence.fullPathName if occurrence else None, native.entityToken)


def _raw_meshes(body: fusion.BRepBody) -> list:
    """重力に依存しない生メッシュ [(平坦な座標列, 頂点インデックス列), ...] を返す。

    body 全体の meshManager は面が丸ごと欠落したメッシュを返すことがある
    (実測で傾斜面 1 枚が欠けた)。メッシュに穴があるとその柱の交差数が奇数になり、
    排水口とみなされて窪みの水が丸ごと抜けてしまうため、面ごとにメッシュ化して
    連結し、全面が必ず含まれるようにする。
    """
    raw = []

    def collect(mesh: Any) -> None:
        """メッシュの頂点と三角形を収集する。"""
        if mesh is None:
            return
        # nodeCoordinates は Point3D を頂点数ぶん生成し、さらに .x/.y/.z で
        # 3 回ずつ C++ 境界を跨ぐ。平坦な float 配列を 1 回で受け取る方が桁違いに速い。
        raw.append((mesh.nodeCoordinatesAsDouble, mesh.nodeIndices))

    for face in body.faces:
        collect(_calc_mesh(face))

    if not any(len(idx) >= 3 for _, idx in raw):  # 面単位が使えない場合のフォールバック
        collect(_calc_mesh(body))
    return raw


def _get_mesh(
    body: fusion.BRepBody,
    ex: tuple[float, float, float],
    ey: tuple[float, float, float],
    ez: tuple[float, float, float],
    mesh_cache: dict = None,
) -> Any:
    """ボディの三角形メッシュをローカル系 (ez が上) で返す。

    メッシュ化 (処理時間の大半) は重力に依存しないため、mesh_cache があれば
    生メッシュをボディ単位で使い回し、基底への座標変換だけをやり直す。
    """
    if mesh_cache is None:
        raw = _raw_meshes(body)
    else:
        key = _body_key(body)
        raw = mesh_cache.get(key)
        if raw is None:
            raw = mesh_cache[key] = _raw_meshes(body)

    verts = []
    tris = []
    ex0, ex1, ex2 = ex
    ey0, ey1, ey2 = ey
    ez0, ez1, ez2 = ez

    for coords, idx in raw:
        base = len(verts)
        # 乗算・加算の順序は変えていないので、得られる値は従来と同一。
        append = verts.append
        for k in range(0, len(coords) - 2, 3):
            x = coords[k]
            y = coords[k + 1]
            z = coords[k + 2]
            append(
                (
                    x * ex0 + y * ex1 + z * ex2,
                    x * ey0 + y * ey1 + z * ey2,
                    x * ez0 + y * ez1 + z * ez2,
                )
            )
        if base:
            idx = [base + v for v in idx]
        end = len(idx) - len(idx) % 3
        tris.extend(zip(idx[0:end:3], idx[1:end:3], idx[2:end:3]))
    return verts, tris


def _get_meshes(
    bodies: tuple,
    ex: tuple[float, float, float],
    ey: tuple[float, float, float],
    ez: tuple[float, float, float],
    mesh_cache: dict = None,
) -> Any:
    """各ボディのメッシュと、全体グリッド用に連結した頂点列を返す。

    三角形はボディ境界を越えて連結しない。後段でボディごとに内外判定してから
    区間を和集合化するため、重なり合う部品でも偶奇規則が壊れない。
    """
    meshes = []
    all_verts = []
    for index, body in enumerate(bodies, 1):
        verts, tris = _get_mesh(body, ex, ey, ez, mesh_cache)
        if not tris:
            raise RuntimeError(tr("mesh_failed", index=index))
        meshes.append((verts, tris))
        all_verts.extend(verts)
    return meshes, all_verts


# --------------------------------------------------------------------------
# グリッド
# --------------------------------------------------------------------------
class _Grid:
    def __init__(
        self,
        verts: list[tuple[float, float, float]],
        resolution: int,
        pad_cells: int = 2,
    ) -> None:
        """計測またはグリッドの初期状態を設定する。"""
        xs = [v[0] for v in verts]
        ys = [v[1] for v in verts]
        zs = [v[2] for v in verts]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        self.min_z, self.max_z = min(zs), max(zs)

        span = max(max_x - min_x, max_y - min_y, 1e-6)
        self.cs = span / max(1, resolution)
        cs = self.cs

        self.pad = pad_cells
        self.nx = int(math.ceil((max_x - min_x) / cs)) + 2 * pad_cells + 1
        self.ny = int(math.ceil((max_y - min_y) / cs)) + 2 * pad_cells + 1
        # CAD の平面とセル中心が正確に一致して内外判定が不安定になるのを避ける微小オフセット
        self.ox = min_x - pad_cells * cs + cs * 0.0137
        self.oy = min_y - pad_cells * cs + cs * 0.0271

        self.base_z = self.min_z - cs
        self.top_z = self.max_z + cs

    def cx(self, i: int) -> float:
        """列番号からセル中心の X 座標を求める。"""
        return self.ox + (i + 0.5) * self.cs

    def cy(self, j: int) -> float:
        """行番号からセル中心の Y 座標を求める。"""
        return self.oy + (j + 0.5) * self.cs


# --------------------------------------------------------------------------
# 柱モデル (共通の土台)
# --------------------------------------------------------------------------
# 2.5D の下流 (_pools_25d / _prism_bottom) が実際に必要とするのは top_z /
# bottom_z / solid / ok の 4 配列だけ。
# バックエンド (純 Python / numpy) を問わずこの型だけを見ればよいようにする。
_Columns = namedtuple("_Columns", "top_z bottom_z solid ok")


def _columns_from_intervals(raw: Any, grid: _Grid) -> _Columns:
    """_build_columns_python の戻り値 ([(intervals, ok), ...]) を _Columns へ変換する。"""
    n = len(raw)
    top_z = [grid.base_z] * n
    bottom_z = [grid.base_z] * n
    solid = bytearray(n)
    ok = bytearray(n)
    for k, (intervals, is_ok) in enumerate(raw):
        if is_ok:
            ok[k] = 1
        if intervals:
            solid[k] = 1
            bottom_z[k] = intervals[0][0]
            top_z[k] = intervals[-1][1]
    return _Columns(top_z, bottom_z, solid, ok)


def _build_columns(
    verts: list[tuple[float, float, float]],
    tris: list[tuple[int, int, int]],
    grid: _Grid,
    include_overhang: bool = False,
) -> _Columns | list:
    """柱情報を返す。

    include_overhang=True の場合は区間グラフが消費する
    ``[(intervals, ok), ...]`` を返す (区間解析は numpy 化の対象外)。
    それ以外は numpy が使えれば numpy 版カーネルを使い、失敗時は静かに純 Python
    版へフォールバックして、どちらの場合も ``_Columns`` (top_z/bottom_z/solid/ok)
    を返す。
    """
    if include_overhang:
        return _build_columns_python(verts, tris, grid)

    # numpy の取得・pip install (初回のみ発生しうる) は columns 段階の実測を
    # 汚さないよう、別枠で計測する。
    with _Stage("numpy_load"):
        np = numpy_loader.load_numpy()
    if np is not None:
        try:
            top_z, bottom_z, solid, ok = wet_spot_np.build_columns(
                np, verts, tris, grid
            )
            return _Columns(top_z, bottom_z, solid, ok)
        except Exception:  # noqa: BLE001 - どんな失敗でも純 Python へ逃がす
            pass

    raw = _build_columns_python(verts, tris, grid)
    return _columns_from_intervals(raw, grid)


def _build_columns_for_meshes(
    meshes: Any, grid: _Grid, include_overhang: bool = False
) -> _Columns | list:
    """ボディ別メッシュから、ソリッドの和集合となる柱情報を作る。

    メッシュを連結して交差高さを偶奇で組にすると、部品同士が接触・重複した
    箇所で内外が反転し得る。各ボディについて閉じたソリッド区間を先に作り、
    その区間同士を併合することで BRep の和演算なしに同じ意味を得る。
    """
    if len(meshes) == 1:
        verts, tris = meshes[0]
        return _build_columns(verts, tris, grid, include_overhang)

    if include_overhang:
        columns_by_body = [
            _build_columns_python(verts, tris, grid) for verts, tris in meshes
        ]
        return _merge_interval_columns(columns_by_body, grid)

    # numpy のロードは複数ボディでも 1 回だけにする。各ボディのカーネル出力を
    # 合成するので、単純なメッシュ連結を numpy に渡す必要はない。
    with _Stage("numpy_load"):
        np = numpy_loader.load_numpy()
    columns_by_body = []
    for verts, tris in meshes:
        if np is not None:
            try:
                top_z, bottom_z, solid, ok = wet_spot_np.build_columns(
                    np, verts, tris, grid
                )
                columns_by_body.append(_Columns(top_z, bottom_z, solid, ok))
                continue
            except Exception:  # noqa: BLE001 - 個別に純 Python へフォールバック
                pass
        columns_by_body.append(
            _columns_from_intervals(_build_columns_python(verts, tris, grid), grid)
        )
    return _merge_columns(columns_by_body, grid)


def _merge_columns(columns_by_body: Any, grid: _Grid) -> _Columns:
    """2.5D 用のボディ別列を、ソリッド和集合の最上面・最下面へ合成する。"""
    n = grid.nx * grid.ny
    top_z = [grid.base_z] * n
    bottom_z = [grid.base_z] * n
    solid = bytearray(n)
    ok = bytearray(b"\x01") * n

    for columns in columns_by_body:
        for k in range(n):
            if not columns.ok[k]:
                ok[k] = 0
            if not columns.solid[k]:
                continue
            if not solid[k]:
                bottom_z[k] = columns.bottom_z[k]
                top_z[k] = columns.top_z[k]
                solid[k] = 1
            else:
                bottom_z[k] = min(bottom_z[k], columns.bottom_z[k])
                top_z[k] = max(top_z[k], columns.top_z[k])
    return _Columns(top_z, bottom_z, solid, ok)


def _merge_interval_columns(columns_by_body: Any, grid: _Grid) -> list:
    """完全モード用のボディ別区間をセルごとに和集合化する。"""
    n = grid.nx * grid.ny
    z_tol = grid.cs * 1e-6
    merged = []
    for k in range(n):
        intervals = []
        is_ok = True
        for columns in columns_by_body:
            body_intervals, body_ok = columns[k]
            intervals.extend(body_intervals)
            is_ok = is_ok and body_ok
        merged.append((_union_intervals(intervals, z_tol), is_ok))
    return merged


def _union_intervals(intervals: Any, z_tol: float) -> Any:
    """重なり・接触する閉区間を 1 本に併合して返す。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = [list(ordered[0])]
    for start, end in ordered[1:]:
        previous = out[-1]
        if start <= previous[1] + z_tol:
            if end > previous[1]:
                previous[1] = end
        else:
            out.append([start, end])
    return [tuple(interval) for interval in out]


def _build_columns_python(
    verts: list[tuple[float, float, float]],
    tris: list[tuple[int, int, int]],
    grid: _Grid,
) -> Any:
    """各 (x, y) セルについて (ソリッド区間 [(z_bottom, z_top), ...], 信頼フラグ) を返す。"""
    nx, ny, cs, ox, oy = grid.nx, grid.ny, grid.cs, grid.ox, grid.oy
    crossings = [[] for _ in range(nx * ny)]

    # セル中心の座標は三角形に依らないので、グリッド全体で一度だけ作って使い回す。
    # 式は下のループにあったものと同一なので、得られる値もビット単位で同じ。
    px_of = [ox + (i + 0.5) * cs for i in range(nx)]
    py_of = [oy + (j + 0.5) * cs for j in range(ny)]

    # 三角形あたりの固定費が支配的なので、組み込み関数もローカルへ束縛する。
    ceil = math.ceil
    floor = math.floor
    sqrt = math.sqrt
    fmin = min
    fmax = max

    for ia, ib, ic in tris:
        # タプル添字を毎回引かずに済むよう、頂点を 1 回で展開する。
        a0, a1, a2 = verts[ia]
        b0, b1, b2 = verts[ib]
        c0, c1, c2 = verts[ic]

        ux, uy, uz = b0 - a0, b1 - a1, b2 - a2
        vx, vy, vz = c0 - a0, c1 - a1, c2 - a2
        n_x = uy * vz - uz * vy
        n_y = uz * vx - ux * vz
        n_z = ux * vy - uy * vx
        # 垂直な面 (XY への射影面積がゼロ) は垂直レイと交差しない。
        # 三角形の大きさに依らない判定にするため法線長に対する相対しきい値を使う。
        n_len = sqrt(n_x * n_x + n_y * n_y + n_z * n_z)
        if n_len <= 0.0 or abs(n_z) < 1e-9 * n_len:
            continue

        # グリッド外の三角形はここで捨てる。以降の準備計算より先に判定して、
        # 捨てる三角形に余計なコストを払わないようにする。
        # int(math.ceil(x)) の int() は不要 (math.ceil は int を返す)。
        i0 = fmax(0, ceil((fmin(a0, b0, c0) - ox) / cs - 0.5))
        i1 = fmin(nx - 1, floor((fmax(a0, b0, c0) - ox) / cs - 0.5))
        j0 = fmax(0, ceil((fmin(a1, b1, c1) - oy) / cs - 0.5))
        j1 = fmin(ny - 1, floor((fmax(a1, b1, c1) - oy) / cs - 0.5))
        if i0 > i1 or j0 > j1:
            continue

        # 内外判定用に CCW へ揃える (平面式は元の法線のままで正しい)
        if n_z > 0:
            ra0, ra1, rb0, rb1, rc0, rc1 = a0, a1, b0, b1, c0, c1
        else:
            ra0, ra1, rb0, rb1, rc0, rc1 = a0, a1, c0, c1, b0, b1
        # 共有辺でセル中心を取りこぼすと床に穴が空き、盆地が丸ごと排水されてしまう。
        # 境界は両側の三角形で拾っておき、生じた重複交差は _intervals() で潰す。
        neg_tol = -(abs(n_z) * 1e-9)

        # 内側ループで毎回計算し直していた「三角形ごとに一定の値」を先に出しておく。
        # 同じ値を使い回すだけなので結果は変わらない。
        e0x, e0y = rc0 - rb0, rc1 - rb1
        e1x, e1y = ra0 - rc0, ra1 - rc1
        e2x, e2y = rb0 - ra0, rb1 - ra1

        for j in range(j0, j1 + 1):
            py = py_of[j]
            row = j * nx
            # 行の中で一定の項も外へ出す。
            dy_b, dy_c, dy_a = py - rb1, py - rc1, py - ra1
            ny_dy = n_y * (py - a1)
            for i in range(i0, i1 + 1):
                px = px_of[i]
                w0 = e0x * dy_b - e0y * (px - rb0)
                if w0 < neg_tol:
                    continue
                w1 = e1x * dy_c - e1y * (px - rc0)
                if w1 < neg_tol:
                    continue
                w2 = e2x * dy_a - e2y * (px - ra0)
                if w2 < neg_tol:
                    continue
                z = a2 - (n_x * (px - a0) + ny_dy) / n_z
                crossings[row + i].append(z)

    z_tol = cs * 1e-6
    return [_intervals(cl, z_tol) for cl in crossings]


def _intervals(crossing_list: Any, z_tol: float) -> Any:
    """交差 z のリストをソリッド区間に組み上げる。

    三角形の巻き方向 (winding) に依存しないよう even-odd 規則を使う。
    閉じたソリッドなら垂直レイの交差数は必ず偶数になるので、z 昇順に
    2 つずつ組にすれば中空ボディ (外殻 + 内殻) でも正しく区間が得られる。

    Returns:
        (区間リスト, 信頼フラグ)。交差数が奇数の柱は数値的に壊れているので
        信頼フラグを False にし、呼び出し側で排水口扱いしないようにする。
    """
    if not crossing_list:
        return [], True
    crossing_list.sort()

    # 共有辺・共有頂点を複数の三角形で拾った重複を 1 つに畳む。
    zs = [crossing_list[0]]
    for z in crossing_list:
        if z - zs[-1] > z_tol:
            zs.append(z)

    ok = len(zs) % 2 == 0
    out = []
    for k in range(0, len(zs) - 1, 2):
        if zs[k + 1] > zs[k]:
            out.append((zs[k], zs[k + 1]))
    return out, ok


# --------------------------------------------------------------------------
# Priority-Flood (2.5D 高さ場)
# --------------------------------------------------------------------------
def _pools_25d(grid: _Grid, columns: _Columns, min_depth: float) -> list[dict]:
    """高さ場の排水水位から水が残る領域を検出する。"""
    nx, ny, cs = grid.nx, grid.ny, grid.cs
    n = nx * ny

    stage_prepare = _Stage("flood_prepare")
    stage_prepare.__enter__()

    height = [grid.base_z] * n
    solid = bytearray(n)
    reliable = bytearray(n)
    col_top_z, _col_bottom_z, col_solid, col_ok = columns
    for k in range(n):
        if col_ok[k]:
            reliable[k] = 1
        if col_solid[k]:
            height[k] = col_top_z[k]  # 最上面
            solid[k] = 1

    # ラスタライズ由来の孤立した穴を埋める。
    # 床に 1 セルでも穴があると、そこが排水口になり盆地が丸ごと消えてしまう。
    # 外周を除く内部セルだけが対象なので、行・列で回して k の除算を省く。
    # filled は走査前の複製なので、走査順を変えても結果は変わらない。
    filled = bytes(solid)
    for j in range(1, ny - 1):
        row = j * nx
        for k in range(row + 1, row + nx - 1):
            if filled[k]:
                continue
            up_k = k + nx
            down_k = k - nx
            if not (
                filled[k + 1] and filled[k - 1] and filled[up_k] and filled[down_k]
            ):
                continue
            height[k] = min(height[k + 1], height[k - 1], height[up_k], height[down_k])
            solid[k] = 1

    level = [_INF] * n
    heap = []

    def seed(k: int) -> None:
        """排水探索の起点を優先度キューへ追加する。"""
        if level[k] == _INF:
            level[k] = height[k]
            heapq.heappush(heap, (height[k], k))

    # 外周セル + 空の柱 (床が無いので水は素通りする = 排水口)
    for i in range(nx):
        seed(i)
        seed((ny - 1) * nx + i)
    for j in range(ny):
        seed(j * nx)
        seed(j * nx + nx - 1)
    for k in range(n):
        # 信頼できない柱 (交差数が奇数) は排水口とみなさない。
        # 水位も与えないので、障壁にも排水口にもならず素通りするだけになる。
        if not solid[k] and reliable[k]:
            seed(k)

    stage_prepare.__exit__()
    stage_flood = _Stage("flood_dijkstra")
    stage_flood.__enter__()

    while heap:
        lv, k = heapq.heappop(heap)
        i = k % nx
        j = k // nx
        for di, dj in _DRAIN_NEIGHBORS:
            ii, jj = i + di, j + dj
            if 0 <= ii < nx and 0 <= jj < ny:
                kk = jj * nx + ii
                if level[kk] == _INF:
                    lv2 = lv if lv > height[kk] else height[kk]
                    level[kk] = lv2
                    heapq.heappush(heap, (lv2, kk))

    stage_flood.__exit__()
    stage_components = _Stage("components")
    stage_components.__enter__()

    wet = bytearray(n)
    for k in range(n):
        if solid[k] and level[k] > height[k]:
            wet[k] = 1

    cell_area = cs * cs
    pools = []
    seen = bytearray(n)
    for k0 in range(n):
        if not wet[k0] or seen[k0]:
            continue
        # 連結成分 (4 近傍)
        comp = []
        seen[k0] = 1
        stack = [k0]
        while stack:
            k = stack.pop()
            comp.append(k)
            i = k % nx
            j = k // nx
            for di, dj in _CARDINAL_NEIGHBORS:
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny:
                    kk = jj * nx + ii
                    if wet[kk] and not seen[kk]:
                        seen[kk] = 1
                        stack.append(kk)

        water_level = max(level[k] for k in comp)
        deepest = min(comp, key=lambda k: height[k])
        max_depth = water_level - height[deepest]
        if max_depth <= min_depth:
            continue

        volume = sum((water_level - height[k]) for k in comp) * cell_area
        drains = _drain_cells_25d(comp, deepest, height, grid, min_depth)
        quads = [(grid.cx(k % nx), grid.cy(k // nx), water_level) for k in comp]

        stage_components.__exit__()
        stage_outline = _Stage("outline")
        stage_outline.__enter__()

        # 画像2の赤線に相当する「一回り大きい」輪郭。水面より高い材料の上にだけ
        # 広げるので、プリズムの側面は必ずボディの中を通る。
        footprint = _dilate_cells(
            comp, grid, solid, height, water_level, _OUTLINE_PAD_CELLS
        )
        outline = _pool_outline(footprint, grid, protected_cells=comp)
        pools.append(
            {
                "water_level": water_level,
                "prism_bottom": _prism_bottom(columns, footprint, height[deepest]),
                "max_depth": max_depth,
                "area": len(comp) * cell_area,
                "volume": volume,
                "floor": (
                    grid.cx(deepest % nx),
                    grid.cy(deepest // nx),
                    height[deepest],
                ),
                "drains": [
                    (grid.cx(k % nx), grid.cy(k // nx), height[k]) for k in drains
                ],
                "quad_centers": quads,
                "outline": outline,
                "mode": "2.5d",
                "cs": cs,
            }
        )
        stage_outline.__exit__()
        stage_components.__enter__()
    stage_components.__exit__()
    return pools


def _drain_cells_25d(
    comp: list[int], deepest: int, height: list[float], grid: _Grid, min_depth: float
) -> list[int]:
    """水域を残さず抜くための水抜きセルを返す。先頭は deepest。

    最深点だけに穴を開けても、鞍部で仕切られた別のくぼみには水が残る。
    水抜きセルを排水口とした Priority-Flood を水域内だけで繰り返し、
    min_depth を超えて水が残る連結成分ごとに、その最深セルを追加する。
    同じ回に見つかる残水成分は互いの水位に影響しないので、同時に追加してよい。
    """
    nx, ny = grid.nx, grid.ny
    members = set(comp)
    drains = [deepest]
    while True:
        level = {}
        heap = []
        for k in drains:
            level[k] = height[k]
            heapq.heappush(heap, (height[k], k))
        while heap:
            lv, k = heapq.heappop(heap)
            i = k % nx
            j = k // nx
            for di, dj in _DRAIN_NEIGHBORS:
                ii, jj = i + di, j + dj
                if 0 <= ii < nx and 0 <= jj < ny:
                    kk = jj * nx + ii
                    if kk in members and kk not in level:
                        lv2 = lv if lv > height[kk] else height[kk]
                        level[kk] = lv2
                        heapq.heappush(heap, (lv2, kk))

        residual = {k for k in comp if level.get(k, height[k]) > height[k]}
        added = []
        while residual:
            k0 = residual.pop()
            part = [k0]
            stack = [k0]
            while stack:
                k = stack.pop()
                i = k % nx
                j = k // nx
                for di, dj in _CARDINAL_NEIGHBORS:
                    ii, jj = i + di, j + dj
                    if 0 <= ii < nx and 0 <= jj < ny:
                        kk = jj * nx + ii
                        if kk in residual:
                            residual.discard(kk)
                            part.append(kk)
                            stack.append(kk)
            bottom = min(part, key=lambda k: height[k])
            if max(level[k] for k in part) - height[bottom] > min_depth:
                added.append(bottom)
        if not added:
            return drains
        drains.extend(added)


def _dilate_cells(
    cells: Any, grid: _Grid, solid: Any, height: float, water_level: float, pad: Any
) -> Any:
    """wet セル集合を pad セル分だけ外側へ広げる。

    広げる先は「ソリッドがあり、かつ水面以上の高さを持つセル」に限る。
    ボディの外へはみ出さず、隣接する別の窪みへも流れ込まないようにするため。
    """
    allowed = bytearray(grid.nx * grid.ny)
    for k in range(len(allowed)):
        if solid[k] and height[k] >= water_level:
            allowed[k] = 1
    return _dilate_masked_cells(cells, grid, allowed, pad)


def _dilate_masked_cells(cells: Any, grid: _Grid, allowed: Any, pad: Any) -> Any:
    """セル集合を、allowed が真のセルだけへ4近傍で広げる。"""
    nx, ny = grid.nx, grid.ny
    out = set(cells)
    frontier = list(cells)
    for _ in range(max(0, int(pad))):
        nxt = []
        for k in frontier:
            i, j = k % nx, k // nx
            for di, dj in _CARDINAL_NEIGHBORS:
                ii, jj = i + di, j + dj
                if not 0 <= ii < nx:
                    continue
                if not 0 <= jj < ny:
                    continue
                kk = jj * nx + ii
                if kk in out or not allowed[kk]:
                    continue
                out.add(kk)
                nxt.append(kk)
        if not nxt:
            break
        frontier = nxt
    return out


def _exclude_side_draining_pools(pools: Any, interval_columns: Any, grid: _Grid) -> Any:
    """水位面で横方向に外へ開く 2.5D 候補を取り除く。"""
    return [
        pool for pool in pools if not _pool_has_side_drain(pool, interval_columns, grid)
    ]


def _pool_has_side_drain(pool: PoolRegion, interval_columns: Any, grid: _Grid) -> bool:
    """候補の水位面にある空気が、横穴を通って外周へ達するかを返す。

    2.5D の Priority-Flood は各列の最上面だけを使うため、例えば横向きの
    円筒穴では上側の肉を越えられない壁として扱う。一方、実際の水位より
    わずかに下の水平断面では、その屋根の下が空いていることがある。この
    空気領域を 8 近傍で外周まで探索し、到達可能なら水は溜まらない。
    """
    nx, ny = grid.nx, grid.ny
    n = nx * ny
    z = pool["water_level"] - max(grid.cs * 1e-6, 1e-9)
    tolerance = max(grid.cs * 1e-9, 1e-12)

    air = bytearray(n)
    for k, (intervals, is_ok) in enumerate(interval_columns):
        # 交差数が不正な列を空気と断定すると、メッシュ誤差で実在の水たまりを
        # 消してしまう。排水を確実に示せる正常列だけを通路にする。
        if not is_ok:
            continue
        if not any(low - tolerance <= z <= high + tolerance for low, high in intervals):
            air[k] = 1

    seen = bytearray(n)
    stack = []
    for x, y, _z in pool["quad_centers"]:
        i = int(math.floor((x - grid.ox) / grid.cs))
        j = int(math.floor((y - grid.oy) / grid.cs))
        if not (0 <= i < nx and 0 <= j < ny):
            continue
        k = j * nx + i
        if air[k] and not seen[k]:
            seen[k] = 1
            stack.append(k)

    while stack:
        k = stack.pop()
        i, j = k % nx, k // nx
        if i == 0 or i == nx - 1 or j == 0 or j == ny - 1:
            return True
        for di, dj in _DRAIN_NEIGHBORS:
            ii, jj = i + di, j + dj
            if not (0 <= ii < nx and 0 <= jj < ny):
                continue
            kk = jj * nx + ii
            if air[kk] and not seen[kk]:
                seen[kk] = 1
                stack.append(kk)
    return False


def _prism_bottom(columns: Any, footprint: Any, floor_z: float) -> Any:
    """差演算用プリズムの下端の高さを返す。

    最深部より _PRISM_BOTTOM_MARGIN だけ深い位置を基本とし、プリズムが
    ボディの下面を突き抜けないよう、輪郭内で最も高い「ボディ下面」で制限する。
    突き抜けると差演算後にボディ下方の空間がゴミとして残ってしまう。
    """
    under = None
    col_bottom_z, col_solid = columns.bottom_z, columns.solid
    for k in footprint:
        if not col_solid[k]:
            continue
        z = col_bottom_z[k]
        if under is None or z > under:
            under = z

    bottom = floor_z - _PRISM_BOTTOM_MARGIN
    if under is not None:
        safe = under + _PRISM_BOTTOM_MARGIN * 0.5
        if bottom < safe:
            # 薄肉で余裕が無い場合でも、床には必ず届かせる。
            bottom = min(safe, floor_z - _PRISM_BOTTOM_MARGIN * 0.1)
    return bottom


def _interval_pool_outline(region: Any, columns: Any, grid: _Grid) -> Any:
    """屋根下も含む水域を、同じ水位にある材料の中へだけ拡張する。"""
    cells = set(region["footprint_cells"])
    level = region["water_level"] - _PRISM_TOP_DROP
    tolerance = max(grid.cs * 1e-8, 1e-10)
    allowed = bytearray(grid.nx * grid.ny)
    for k, (intervals, reliable) in enumerate(columns):
        if reliable and any(
            low - tolerance <= level <= high + tolerance for low, high in intervals
        ):
            allowed[k] = 1
    footprint = _dilate_masked_cells(cells, grid, allowed, _OUTLINE_PAD_CELLS)
    outline = _pool_outline(footprint, grid, protected_cells=cells)
    # 複数の投影外周や点接続で一部が欠けた場合は、切れた水域を生成しない。
    if not outline or not _polygon_encloses_cells(outline, cells, grid):
        return []  # 生成段階でこの候補だけを失敗として報告する。
    return outline


def _point_on_outline(x: Any, y: Any, outline: Any, tolerance: float) -> bool:
    """輪郭線分までの距離で、凹形状・斜辺にも対応した側面接触を判定する。"""
    for (ax, ay), (bx, by) in zip(outline, outline[1:] + outline[:1]):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = (
            max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
            if length2
            else 0.0
        )
        if (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= tolerance**2:
            return True
    return False


def _carrier_outline(grid: _Grid) -> Any:
    """A four-edge carrier outside the sampled model footprint."""
    x0, y0 = grid.ox, grid.oy
    x1 = x0 + grid.nx * grid.cs
    y1 = y0 + grid.ny * grid.cs
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _pool_outline(cells: Any, grid: _Grid, protected_cells: Any = None) -> Any:
    """4近傍セル外周を、保護セルを欠かさない輪郭へ変換する。"""
    edges = {}
    for index in cells:
        i, j = index % grid.nx, index // grid.nx
        # セルの外周を反時計回りに登録し、隣接セルとの共有辺は打ち消す。
        corners = ((i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1))
        for start, end in zip(corners, corners[1:] + corners[:1]):
            if (end, start) in edges:
                del edges[(end, start)]
            else:
                edges[(start, end)] = True

    loops = []
    while edges:
        start, end = next(iter(edges))
        del edges[(start, end)]
        loop = [start]
        current = end
        while current != start:
            loop.append(current)
            next_edges = [edge for edge in edges if edge[0] == current]
            if not next_edges:
                return []
            edge = next_edges[0]
            del edges[edge]
            current = edge[1]
        loops.append(loop)

    if not loops:
        return []
    # 凹部の輪郭は最大面積の外周だけを使う。内部の高い島は後段で元ボディを
    # 差し引くため、ここで穴として扱う必要はない。
    loop = max(loops, key=lambda points: abs(_signed_area(points)))
    points = [(grid.ox + i * grid.cs, grid.oy + j * grid.cs) for i, j in loop]
    raw = _remove_collinear_points(points)
    if protected_cells is None:
        protected_cells = cells

    # 格子外周をそのまま使うと階段状のプリズム側面が露出する。RDP は直線で
    # この段差を抑えられるが、凹部を横切って水域を削ることがあるため、検出済み
    # の水面セルをすべて含む場合だけ採用する。
    smoothed = _simplify_closed_polygon(raw, grid.cs * 0.75)
    if len(smoothed) >= 3 and _polygon_encloses_cells(smoothed, protected_cells, grid):
        return smoothed
    return raw


def _signed_area(points: list[tuple[float, float]]) -> float:
    """閉じた輪郭の符号付き面積を求める。"""
    return (
        sum(
            x0 * y1 - x1 * y0
            for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1])
        )
        * 0.5
    )


def _remove_collinear_points(points: Any) -> Any:
    """閉じた直交輪郭から、形状を変えない中間点だけを取り除く。"""
    if len(points) <= 3:
        return points

    out = []
    count = len(points)
    for index, current in enumerate(points):
        previous = points[(index - 1) % count]
        following = points[(index + 1) % count]
        vx0, vy0 = current[0] - previous[0], current[1] - previous[1]
        vx1, vy1 = following[0] - current[0], following[1] - current[1]
        if vx0 * vy1 == vy0 * vx1 and vx0 * vx1 + vy0 * vy1 > 0.0:
            continue
        out.append(current)
    return out if len(out) >= 3 else points


def _simplify_closed_polygon(points: Any, tolerance: float) -> Any:
    """RDPで閉じた輪郭を簡略化する。包含性は呼び出し側で検証する。"""
    if len(points) <= 4:
        return points

    first = points[0]
    split = max(
        range(1, len(points)),
        key=lambda index: (points[index][0] - first[0]) ** 2
        + (points[index][1] - first[1]) ** 2,
    )

    def simplify_open(path: Any) -> Any:
        """開いた輪郭の不要な点を減らす。"""
        if len(path) <= 2:
            return path
        ax, ay = path[0]
        bx, by = path[-1]
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        max_distance = -1.0
        max_index = 0
        for index, (px, py) in enumerate(path[1:-1], 1):
            if denominator == 0.0:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dx * (ay - py) - (ax - px) * dy) / math.sqrt(denominator)
            if distance > max_distance:
                max_distance, max_index = distance, index
        if max_distance <= tolerance:
            return [path[0], path[-1]]
        return simplify_open(path[: max_index + 1])[:-1] + simplify_open(
            path[max_index:]
        )

    forward = simplify_open(points[: split + 1])
    backward = simplify_open(points[split:] + [points[0]])
    return forward[:-1] + backward[:-1]


def _polygon_encloses_cells(points: Any, cells: Any, grid: _Grid) -> Any:
    """セル全体が輪郭内または境界上にあるかを、4頂点で検証する。"""
    tolerance = grid.cs * 1e-9
    for index in cells:
        i, j = index % grid.nx, index // grid.nx
        x0 = grid.ox + i * grid.cs
        y0 = grid.oy + j * grid.cs
        for point in (
            (x0, y0),
            (x0 + grid.cs, y0),
            (x0 + grid.cs, y0 + grid.cs),
            (x0, y0 + grid.cs),
        ):
            if not _point_in_or_on_polygon(point, points, tolerance):
                return False
    return True


def _point_in_or_on_polygon(point: Any, polygon: Any, tolerance: float) -> bool:
    """点が単純多角形の内部または境界上にあるかを返す。"""
    x, y = point
    inside = False
    for start, end in zip(polygon, polygon[1:] + polygon[:1]):
        x0, y0 = start
        x1, y1 = end
        dx, dy = x1 - x0, y1 - y0
        cross = dx * (y - y0) - dy * (x - x0)
        if (
            abs(cross) <= tolerance * max(1.0, abs(dx), abs(dy))
            and min(x0, x1) - tolerance <= x <= max(x0, x1) + tolerance
            and min(y0, y1) - tolerance <= y <= max(y0, y1) + tolerance
        ):
            return True
        if (y0 > y) != (y1 > y):
            hit_x = x0 + (y - y0) * dx / dy
            if hit_x > x:
                inside = not inside
    return inside


# ワールド座標への変換
# --------------------------------------------------------------------------
def _to_pool_region(
    raw: dict,
    ex: tuple[float, float, float],
    ey: tuple[float, float, float],
    ez: tuple[float, float, float],
    up: core.Vector3D,
) -> PoolRegion:
    """解析結果をワールド座標の水たまり領域へ変換する。"""
    cs = raw["cs"]
    half = cs * 0.5
    level = raw["water_level"]
    fx, fy, fz = raw["floor"]
    sx, sy, sz = raw.get("seed", (fx, fy, (fz + level) * 0.5))
    seeds = raw.get("seeds", ())
    if seeds:
        sx, sy, sz = seeds[0]

    # 水面セル 1 個につき Point3D を 4 個作ると、セル数が多いモデルで
    # API 呼び出しだけで数万回に達する。座標変換は純粋な回転なので、
    # _world_point と同じ順序の式でそのまま展開する (値は従来と同一)。
    ex0, ex1, ex2 = ex
    ey0, ey1, ey2 = ey
    ez0, ez1, ez2 = ez
    offsets = ((-half, -half), (half, -half), (half, half), (-half, half))

    coords = []
    indices = []
    coords_extend = coords.extend
    indices_extend = indices.extend
    base = 0
    for cx, cy, cz in raw["quad_centers"]:
        zx = cz * ez0
        zy = cz * ez1
        zz = cz * ez2
        for dx, dy in offsets:
            x = cx + dx
            y = cy + dy
            coords_extend(
                (x * ex0 + y * ey0 + zx, x * ex1 + y * ey1 + zy, x * ex2 + y * ey2 + zz)
            )
        indices_extend((base, base + 1, base + 2, base, base + 2, base + 3))
        base += 4

    outline = tuple(_world_point((x, y, level), ex, ey, ez) for x, y in raw["outline"])
    drains = raw.get("drains") or (raw["floor"],)

    return PoolRegion(
        water_level=level,
        water_plane_origin=_world_point((fx, fy, level), ex, ey, ez),
        up=up,
        seed_point=_world_point((sx, sy, sz), ex, ey, ez),
        seed_points=tuple(_world_point(point, ex, ey, ez) for point in seeds),
        lowest_point=_world_point((fx, fy, fz), ex, ey, ez),
        max_depth=raw["max_depth"],
        area=raw["area"],
        volume=raw["volume"],
        surface_mesh=(coords, indices),
        outline=outline,
        prism_bottom=raw.get("prism_bottom", level - raw["max_depth"]),
        mode=raw["mode"],
        cell_size=raw["cs"],
        drain_points=tuple(_world_point(point, ex, ey, ez) for point in drains),
    )


# --------------------------------------------------------------------------
# 切り分け用の診断
# --------------------------------------------------------------------------
def _boundary_edges(
    verts: list[tuple[float, float, float]], tris: list[tuple[int, int, int]]
) -> int:
    """メッシュの境界辺 (2 枚の三角形で共有されていない辺) の本数を数える。

    0 なら水密。0 でなければメッシュに穴があり、その位置の柱が排水口として
    誤検出されるため、水たまりが丸ごと消える。
    """
    ids = []
    key = {}
    for p in verts:
        k = (round(p[0], 6), round(p[1], 6), round(p[2], 6))
        i = key.get(k)
        if i is None:
            i = len(key)
            key[k] = i
        ids.append(i)

    counts = {}
    for a, b, c in tris:
        for x, y in ((a, b), (b, c), (c, a)):
            e = (ids[x], ids[y]) if ids[x] < ids[y] else (ids[y], ids[x])
            counts[e] = counts.get(e, 0) + 1
    return sum(1 for n in counts.values() if n != 2)


def debug_report(
    body: fusion.BRepBody,
    gravity: core.Vector3D = None,
    resolution: int = 0,
) -> str:
    """検出結果がおかしいときに、どの段階で壊れているかを調べるレポートを返す。

    メッシュ取得 → 柱 (ソリッド区間) 抽出 → Priority-Flood の順に数値を出すので、
    「柱が 0 本」ならラスタライズ側、「柱はあるが pools が 0」なら浸水判定側と切り分けられる。
    """
    if gravity is None:
        gravity = core.Vector3D.create(0.0, 0.0, -1.0)

    ex, ey, ez = _basis(gravity)
    verts, tris = _get_mesh(body, ex, ey, ez)
    lines = ["verts: {} / tris: {}".format(len(verts), len(tris))]
    if not tris:
        lines.append(tr("debug_no_mesh"))
        return "\n".join(lines)

    lines.append("faces: {} / isSolid: {}".format(body.faces.count, body.isSolid))
    lines.append(tr("debug_boundary_edges", count=_boundary_edges(verts, tris)))

    # 手元で完全に再現できるようメッシュをそのまま書き出す。
    dump = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mesh_dump.json")
    with open(dump, "w") as f:
        json.dump({"verts": verts, "tris": tris}, f)
    lines.append("mesh dump: {}".format(dump))

    if resolution <= 0:
        resolution = _DEF_RES_25D
    grid = _Grid(verts, resolution)
    # 診断用途なので numpy 経路を経由させず、常に純 Python 版で切り分ける。
    raw_columns = _build_columns_python(verts, tris, grid)

    n = grid.nx * grid.ny
    n_solid = sum(1 for ivs, _ok in raw_columns if ivs)
    n_bad = sum(1 for _ivs, ok in raw_columns if not ok)
    lines.append(
        "grid: nx={} ny={} cells={} cs={:.6f}".format(grid.nx, grid.ny, n, grid.cs)
    )
    lines.append("z range: {:.4f} .. {:.4f}".format(grid.min_z, grid.max_z))
    lines.append("solid columns: {} / {}".format(n_solid, n))
    lines.append(tr("debug_unreliable_columns", count=n_bad))
    if n_solid == 0:
        lines.append(tr("debug_no_columns"))

    def _dump(tag: str, raw: Any) -> Any:
        """診断用の中間結果を文字列へ整形する。"""
        raw.sort(key=lambda r: r["volume"], reverse=True)
        lines.append("{} pools (min_depth=0): {}".format(tag, len(raw)))
        for i, r in enumerate(raw[:5]):
            lines.append(
                "  [{}] level={:.4f} depth={:.4f} vol={:.4f}".format(
                    i, r["water_level"], r["max_depth"], r["volume"]
                )
            )

    columns = _columns_from_intervals(raw_columns, grid)
    _dump("2.5D", _pools_25d(grid, columns, 0.0))

    # 2.5D は「最上面の高さ場」しか見ないので、庇や天井の下の窪みは原理的に見えない。
    grid_full = _Grid(verts, _DEF_RES_INTERVAL)
    columns_full = _build_columns_python(verts, tris, grid_full)
    interval_pools, nodes = interval_flood.find_pools(grid_full, columns_full, 0.0)
    _dump(tr("debug_interval_graph", count=nodes), interval_pools)
    return "\n".join(lines)
