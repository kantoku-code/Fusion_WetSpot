# Fusion 360 API Python script
"""選択したソリッドに水をかけたときの水たまりを生成する。"""

from __future__ import annotations

import math
import traceback
from pathlib import Path
from typing import Any, Iterator

import adsk.core as core
import adsk.fusion as fusion

from . import pool_engine
from .i18n import tr

# Fusion に登録するコマンドの一意な ID。
_COMMAND_ID = "kantoku_wetspot"
# コマンドボタンを配置する Fusion のアドイン用パネル ID。
_PANEL_ID = "SolidCreatePanel"
# 再描画時に古い矢印・水面を特定して削除するためのグループ名。
_PREVIEW_NAME = "Wet Spot gravity preview v1"

# コマンド入力の ID。各イベントハンドラが入力を参照する際にも使う。
_BODY_ID = "wet_spot_bodies"  # 解析対象のソリッドボディ。
_GRAVITY_ID = "wet_spot_gravity"  # 重力方向を指定する軸または直線。
_REVERSE_ID = "wet_spot_reverse"  # 指定した重力方向の反転。
_MODE_ID = "wet_spot_mode"  # 正確・高速の解析モード。
_DRAIN_ID = "wet_spot_drain"  # 水抜き点の出力設定。
_LIVE_ID = "wet_spot_live"  # 水面のライブプレビュー設定。

# 生成した水たまりをまとめるコンポーネントの表示名。
_CONTAINER_NAME = "Wet Spot"
# Fusion のイベントに登録したハンドラを保持し、GC による解除を防ぐ。
_handlers = []
# 実行中のコマンドごとのルートとハンドラを保持し、終了時に解放する。
_sessions = []


def _is_generated(body: fusion.BRepBody) -> bool:
    """Wet Spot が生成したボディか判定する。"""
    native = body.nativeObject or body
    marker = native.attributes.itemByName(
        pool_engine._POOL_ATTRIBUTE_GROUP, "generated"
    )
    return marker is not None and marker.value == "v1"


def _usable_body(body: fusion.BRepBody, visible_only: bool = False) -> bool:
    """解析対象にできるソリッドボディか判定する。"""
    return (
        body is not None
        and body.isValid
        and body.isSolid
        and (not visible_only or body.isVisible)
        and not _is_generated(body)
    )


def _same_body(left: Any, right: Any) -> bool:
    """同じボディの同じ occurrence だけを同一視する。"""
    left_occ = left.assemblyContext
    right_occ = right.assemblyContext
    left_path = left_occ.fullPathName if left_occ else None
    right_path = right_occ.fullPathName if right_occ else None
    if left_path != right_path:
        return False
    return (left.nativeObject or left) == (right.nativeObject or right)


def _selected_bodies(
    selection_input: core.SelectionCommandInput,
) -> list[fusion.BRepBody]:
    """選択入力から重複のない解析対象ボディを取得する。"""
    bodies = []
    for index in range(selection_input.selectionCount):
        body = selection_input.selection(index).entity
        if _usable_body(body) and not any(_same_body(body, old) for old in bodies):
            bodies.append(body)
    return bodies


def _visible_bodies(root: fusion.Component) -> Iterator[fusion.BRepBody]:
    """全 occurrence のプロキシをルート座標系で収集する。"""
    for body in root.bRepBodies:
        if _usable_body(body, visible_only=True):
            yield body
    for occurrence in root.allOccurrences:
        if not occurrence.isVisible:
            continue
        for body in occurrence.bRepBodies:
            if _usable_body(body, visible_only=True):
                yield body


def _pool_component(root: fusion.Component) -> fusion.Component:
    """水たまり専用コンポーネントを取得し、無ければルート直下に作成する。"""
    for occurrence in root.occurrences:
        component = occurrence.component
        marker = component.attributes.itemByName(
            pool_engine._POOL_ATTRIBUTE_GROUP, "container"
        )
        if marker is not None and marker.value == "v1":
            return component
    occurrence = root.occurrences.addNewComponent(core.Matrix3D.create())
    component = occurrence.component
    component.name = _CONTAINER_NAME
    component.attributes.add(pool_engine._POOL_ATTRIBUTE_GROUP, "container", "v1")
    return component


def _gravity_vector(
    gravity_input: core.SelectionCommandInput, reverse_input: core.BoolValueCommandInput
) -> core.Vector3D:
    """選択した線または軸から重力方向を求める。"""
    if gravity_input.selectionCount == 0:
        return core.Vector3D.create(0.0, 0.0, 1.0 if reverse_input.value else -1.0)
    entity = gravity_input.selection(0).entity
    if entity.objectType == fusion.SketchLine.classType():
        line = entity.worldGeometry
        start, end = line.startPoint, line.endPoint
        direction = core.Vector3D.create(
            end.x - start.x, end.y - start.y, end.z - start.z
        )
    elif entity.objectType == fusion.ConstructionAxis.classType():
        direction = entity.geometry.direction.copy()
    elif entity.objectType == fusion.BRepEdge.classType():
        # 端点を使うと線形エッジの向きを組立座標系で取得できる。
        start = entity.startVertex.geometry
        end = entity.endVertex.geometry
        direction = core.Vector3D.create(
            end.x - start.x, end.y - start.y, end.z - start.z
        )
    else:
        raise ValueError(tr("gravity_selection_error"))
    if not direction.normalize():
        raise ValueError(tr("gravity_zero_error"))
    if reverse_input.value:
        direction.scaleBy(-1.0)
    return direction


def _arrow_geometry(
    bodies: list[fusion.BRepBody], gravity: core.Vector3D
) -> tuple[list[float], list[int]]:
    """重力方向のプレビュー矢印の頂点と線分を作る。"""
    if bodies:
        boxes = [body.boundingBox for body in bodies]
        low = tuple(min(getattr(box.minPoint, axis) for box in boxes) for axis in "xyz")
        high = tuple(
            max(getattr(box.maxPoint, axis) for box in boxes) for axis in "xyz"
        )
        center = tuple((a + b) * 0.5 for a, b in zip(low, high))
        length = max(1.0, max(b - a for a, b in zip(low, high)) * 0.35)
    else:
        center, length = (0.0, 0.0, 0.0), 2.0

    g = (gravity.x, gravity.y, gravity.z)
    if bodies:
        # 矢じりをソリッド内部に重ねない。重力と最も直交する座標軸の外側へ置く。
        side_axis = min(range(3), key=lambda index: abs(g[index]))
        center = list(center)
        center[side_axis] = low[side_axis] - length
    start = tuple(center[i] - g[i] * length * 0.5 for i in range(3))
    tip = tuple(center[i] + g[i] * length * 0.5 for i in range(3))
    helper = (1.0, 0.0, 0.0) if abs(g[0]) < 0.9 else (0.0, 1.0, 0.0)
    side = (
        g[1] * helper[2] - g[2] * helper[1],
        g[2] * helper[0] - g[0] * helper[2],
        g[0] * helper[1] - g[1] * helper[0],
    )
    side_length = math.sqrt(sum(value * value for value in side))
    side = tuple(value / side_length for value in side)
    other = (
        g[1] * side[2] - g[2] * side[1],
        g[2] * side[0] - g[0] * side[2],
        g[0] * side[1] - g[1] * side[0],
    )
    base = tuple(tip[i] - g[i] * length * 0.23 for i in range(3))
    spread = length * 0.11
    points = [start, tip]
    for cross in (side, other):
        for sign in (-1, 1):
            points.append(tuple(base[i] + sign * cross[i] * spread for i in range(3)))
    coords = [value for point in points for value in point]
    return coords, [0, 1, 1, 2, 1, 3, 1, 4, 1, 5]


class _AnalysisCache:
    """コマンド実行中の解析結果を保持し、プレビューと OK で使い回す。

    プレビュー中はロールバックされるだけでモデルは変わらないため、
    コマンドが終わるまでキャッシュを無効化する必要はない。
    """

    def __init__(self) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        self.mesh_cache = {}
        self.key = None
        self.pools = None

    def analyze(
        self,
        bodies: list[fusion.BRepBody],
        gravity: core.Vector3D,
        mode_index: int,
        quiet: bool,
    ) -> list[pool_engine.PoolRegion]:
        """同じ入力の解析結果を再利用して水たまりを求める。"""
        key = (
            tuple(pool_engine._body_key(body) for body in bodies),
            (gravity.x, gravity.y, gravity.z),
            mode_index,
        )
        if key != self.key:
            self.pools = pool_engine.find_wet_spots(
                bodies,
                gravity=gravity,
                include_overhang=mode_index == 0,
                mesh_cache=self.mesh_cache,
                show_progress=not quiet,
            )
            self.key = key
        return self.pools


def _draw_preview_pools(
    group: fusion.CustomGraphicsGroup, pools: list[pool_engine.PoolRegion]
) -> None:
    """検出した水面をプレビュー表示する。"""
    # 選択中ボディの青いハイライトの補色で、明度差も大きい黄色にする。
    effect = fusion.CustomGraphicsSolidColorEffect.create(
        core.Color.create(255, 230, 0, 255)
    )
    for pool in pools:
        coords, indices = pool.surface_mesh
        if not indices:
            continue
        mesh = group.addMesh(
            fusion.CustomGraphicsCoordinates.create(coords), indices, [], []
        )
        mesh.color = effect
        mesh.setOpacity(0.8, True)


def _clear_preview(root: fusion.Component) -> None:
    """現在のプレビュー用グラフィックスを削除する。"""
    groups = root.customGraphicsGroups
    for index in range(groups.count - 1, -1, -1):
        group = groups.item(index)
        if group.name == _PREVIEW_NAME:
            group.deleteMe()


def _update_preview(
    command: core.Command, root: fusion.Component, cache: _AnalysisCache | None = None
) -> None:
    """選択内容に合わせて矢印と水面を更新する。"""
    inputs = command.commandInputs
    bodies = _selected_bodies(inputs.itemById(_BODY_ID))
    gravity = _gravity_vector(
        inputs.itemById(_GRAVITY_ID), inputs.itemById(_REVERSE_ID)
    )
    coords, indices = _arrow_geometry(bodies, gravity)
    _clear_preview(root)
    group = root.customGraphicsGroups.add()
    group.name = _PREVIEW_NAME
    group.isSelectable = False
    group.isChildrenSelectable = False
    lines = group.addLines(
        fusion.CustomGraphicsCoordinates.create(coords), indices, False
    )
    lines.weight = 3
    lines.color = fusion.CustomGraphicsShowThroughColorEffect.create(
        core.Color.create(235, 70, 30, 255), 1.0
    )
    app = core.Application.get()
    if cache is not None and bodies and inputs.itemById(_LIVE_ID).value:
        try:
            pools = cache.analyze(
                bodies, gravity, inputs.itemById(_MODE_ID).selectedItem.index, True
            )
            _draw_preview_pools(group, pools)
        except Exception:
            # 入力変更の度にメッセージボックスを出さないよう、ログに留める。
            app.log(tr("preview_failed", details=traceback.format_exc()))
    app.activeViewport.refresh()


def _report_error() -> None:
    """現在の例外を Fusion のメッセージボックスへ表示する。"""
    core.Application.get().userInterface.messageBox(
        tr("failed", details=traceback.format_exc())
    )


class _CreatedHandler(core.CommandCreatedEventHandler):
    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        try:
            command = args.command
            app = core.Application.get()
            design = fusion.Design.cast(app.activeProduct)
            if design is None:
                app.userInterface.messageBox(tr("open_design"))
                return
            root = design.rootComponent
            inputs = command.commandInputs

            bodies = inputs.addSelectionInput(
                _BODY_ID, tr("bodies_label"), tr("bodies_prompt")
            )
            bodies.addSelectionFilter("SolidBodies")
            bodies.setSelectionLimits(0, 0)
            bodies.isUseCurrentSelections = False

            gravity = inputs.addSelectionInput(
                _GRAVITY_ID, tr("gravity_label"), tr("gravity_prompt")
            )
            for filter_name in ("ConstructionLines", "LinearEdges", "SketchLines"):
                gravity.addSelectionFilter(filter_name)
            gravity.setSelectionLimits(0, 1)
            gravity.isUseCurrentSelections = False
            inputs.addBoolValueInput(_REVERSE_ID, tr("reverse_label"), True, "", False)

            mode = inputs.addDropDownCommandInput(
                _MODE_ID, tr("mode_label"), core.DropDownStyles.TextListDropDownStyle
            )
            mode.listItems.add(tr("mode_accurate"), True, "")
            mode.listItems.add(tr("mode_fast"), False, "")
            inputs.addBoolValueInput(_DRAIN_ID, tr("drain_label"), True, "", False)
            inputs.addBoolValueInput(_LIVE_ID, tr("live_label"), True, "", False)

            input_changed = _InputChangedHandler(root)
            cache = _AnalysisCache()
            session = {"root": root, "handlers": []}
            _sessions.append(session)
            for event, handler in (
                (command.activate, _ActivateHandler(root, input_changed)),
                (command.inputChanged, input_changed),
                (command.executePreview, _PreviewHandler(root, cache)),
                (command.preSelect, _PreSelectHandler()),
                (command.validateInputs, _ValidateHandler()),
                (command.execute, _ExecuteHandler(root, cache)),
                (command.destroy, _DestroyHandler(session)),
            ):
                event.add(handler)
                session["handlers"].append(handler)
        except Exception:
            _report_error()


class _PreSelectHandler(core.SelectionEventHandler):
    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        try:
            if args.activeInput.id == _BODY_ID:
                args.isSelectable = _usable_body(args.selection.entity)
        except Exception:
            args.isSelectable = False


class _ActivateHandler(core.CommandEventHandler):
    def __init__(
        self, root: fusion.Component, input_changed: _InputChangedHandler
    ) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        super().__init__()
        self.root = root
        self.input_changed = input_changed
        self.initialized = False

    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        if self.initialized:
            return
        self.initialized = True
        try:
            body_input = args.command.commandInputs.itemById(_BODY_ID)
            selected = _selected_bodies(body_input)
            self.input_changed._updating = True
            try:
                for body in _visible_bodies(self.root):
                    if not any(_same_body(body, old) for old in selected):
                        if body_input.addSelection(body):
                            selected.append(body)
            finally:
                self.input_changed._updating = False
        except Exception:
            _report_error()


class _InputChangedHandler(core.InputChangedEventHandler):
    def __init__(self, root: fusion.Component) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        super().__init__()
        self.root = root
        self._updating = False

    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        if self._updating:
            return
        self._updating = True
        try:
            inputs = args.inputs
            body_input = inputs.itemById(_BODY_ID)
            changed_id = args.input.id
            if changed_id == _BODY_ID:
                unique = _selected_bodies(body_input)
                if len(unique) != body_input.selectionCount:
                    body_input.clearSelection()
                    for body in unique:
                        body_input.addSelection(body)
        except Exception:
            _report_error()
        finally:
            self._updating = False


class _PreviewHandler(core.CommandEventHandler):
    """プレビューのロールバック毎に矢印 (とライブプレビューの水面) を描き直す。"""

    def __init__(self, root: fusion.Component, cache: _AnalysisCache | None) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        super().__init__()
        self.root = root
        self.cache = cache

    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        try:
            _update_preview(args.command, self.root, self.cache)
            args.isValidResult = False
        except Exception:
            _report_error()


class _ValidateHandler(core.ValidateInputsEventHandler):
    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        args.areInputsValid = bool(_selected_bodies(args.inputs.itemById(_BODY_ID)))


class _ExecuteHandler(core.CommandEventHandler):
    def __init__(self, root: fusion.Component, cache: _AnalysisCache | None) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        super().__init__()
        self.root = root
        self.cache = cache

    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        try:
            inputs = args.command.commandInputs
            bodies = _selected_bodies(inputs.itemById(_BODY_ID))
            if not bodies:
                return
            gravity = _gravity_vector(
                inputs.itemById(_GRAVITY_ID), inputs.itemById(_REVERSE_ID)
            )
            _clear_preview(self.root)
            pool_engine.clear_failed_pool_graphics(self.root)
            mode = inputs.itemById(_MODE_ID).selectedItem
            # プレビューと同じ入力なら、解析を繰り返さずに結果を使い回す。
            pools = self.cache.analyze(bodies, gravity, mode.index, False)
            pool_comp = _pool_component(self.root)
            created = pool_engine.create_pool_bodies(pools, bodies, pool_comp)
            created_pools = pool_engine.get_last_created_pools()
            drain = inputs.itemById(_DRAIN_ID).value
            # OFF や水たまり 0 件でも前回の点は消し、ボディとの食い違いを防ぐ。
            pool_engine.create_drain_points(created_pools if drain else [], pool_comp)
            app = core.Application.get()
            for line in pool_engine.format_last_timings():
                app.log(line)
            if not pools:
                app.userInterface.messageBox(tr("no_pools"))
                return
            lines = [tr("created_count", count=len(created))]
            for pool in created_pools:
                index = next(
                    i for i, candidate in enumerate(pools, 1) if candidate is pool
                )
                lines.append(
                    tr(
                        "pool_summary",
                        index=index,
                        volume=pool.volume,
                        depth=pool.max_depth,
                        area=pool.area,
                    )
                )
                if drain:
                    for point in pool.drain_points or (pool.lowest_point,):
                        lines.append(tr("drain_point", x=point.x, y=point.y, z=point.z))
            if drain and created_pools:
                lines.append(
                    tr(
                        "drain_count",
                        count=sum(
                            len(pool.drain_points or (pool.lowest_point,))
                            for pool in created_pools
                        ),
                        sketch=pool_engine._DRAIN_SKETCH_NAME,
                    )
                )
            app.userInterface.messageBox("\n".join(lines))
        except Exception:
            _report_error()


class _DestroyHandler(core.CommandEventHandler):
    def __init__(self, session: dict) -> None:
        """ハンドラまたは解析キャッシュの状態を初期化する。"""
        super().__init__()
        self.session = session

    def notify(self, args: core.EventArgs) -> None:
        """Fusion のコマンドイベントを処理する。"""
        preview_cleared = False
        try:
            _clear_preview(self.session["root"])
            preview_cleared = True
            core.Application.get().activeViewport.refresh()
        except Exception:
            core.Application.get().log(
                tr("preview_cleanup_failed", details=traceback.format_exc())
            )
        finally:
            if preview_cleared and self.session in _sessions:
                _sessions.remove(self.session)
            self.session["handlers"].clear()


def run(context: dict) -> None:
    """Wet Spot コマンドを Fusion のツールバーへ登録する。"""
    try:
        app = core.Application.get()
        ui = app.userInterface
        if _handlers:
            return
        panel = ui.allToolbarPanels.itemById(_PANEL_ID)
        if panel is None:
            raise RuntimeError(tr("panel_missing"))
        control = panel.controls.itemById(_COMMAND_ID)
        if control is not None:
            control.deleteMe()
        command_def = ui.commandDefinitions.itemById(_COMMAND_ID)
        if command_def is not None:
            command_def.deleteMe()
        command_def = ui.commandDefinitions.addButtonDefinition(
            _COMMAND_ID,
            tr("button_name"),
            tr("button_description"),
            str(Path(__file__).resolve().parent / "resources"),
        )
        created = _CreatedHandler()
        command_def.commandCreated.add(created)
        _handlers.append(created)
        control = panel.controls.addCommand(command_def)
        control.isPromotedByDefault = True
        control.isPromoted = True
    except Exception:
        _report_error()
        stop(context)


def stop(context: dict) -> None:
    """登録したコマンドとプレビューを解放する。"""
    try:
        for session in list(_sessions):
            try:
                _clear_preview(session["root"])
            except Exception:
                core.Application.get().log(
                    tr("preview_cleanup_failed", details=traceback.format_exc())
                )
            session["handlers"].clear()
        _sessions.clear()
        ui = core.Application.get().userInterface
        panel = ui.allToolbarPanels.itemById(_PANEL_ID)
        if panel is not None:
            control = panel.controls.itemById(_COMMAND_ID)
            if control is not None:
                control.deleteMe()
        command_def = ui.commandDefinitions.itemById(_COMMAND_ID)
        if command_def is not None:
            command_def.deleteMe()
    except Exception:
        _report_error()
    finally:
        _handlers.clear()
