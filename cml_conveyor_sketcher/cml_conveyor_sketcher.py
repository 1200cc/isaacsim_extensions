import json
import math
import weakref

import omni.ext
import omni.ui as ui
import omni.usd
from omni.kit.manipulator.camera import ViewportCameraManipulator
from omni.kit.menu.utils import MenuItemDescription, add_menu_items, remove_menu_items
from omni.kit.viewport.registry import RegisterScene
from omni.kit.viewport.utility import get_active_viewport
from omni.ui import scene as sc
from pxr import Gf, Usd, UsdGeom, UsdPhysics


_extension_ref = None
_SKETCH_ROOT_PATH = "/World/ConveyorSketcher"


class _SketchViewportManipulator(ViewportCameraManipulator):
    def __init__(self, enabled, gestures, *args, **kwargs):
        self._enabled = enabled
        self._gestures = gestures
        self._transform = None
        self._screen = None
        super().__init__(*args, **kwargs)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = bool(value)
        if self._screen is not None:
            self._screen.visible = self._enabled
        else:
            self.invalidate()

    def on_build(self):
        if not self._enabled:
            return
        self._transform = sc.Transform()
        with self._transform:
            self._screen = sc.Screen(gestures=self._gestures)

    def destroy(self):
        if self._transform is not None:
            self._transform.clear()
            self._transform = None
        if self._screen is not None:
            self._screen.visible = False
            self._screen = None
        super().destroy()


class _SketchViewportScene:
    def __init__(self, viewport_args):
        self._viewport_api = viewport_args["viewport_api"]
        self._extension_ref = _extension_ref
        self._manipulator = None

        hover_gesture = sc.HoverGesture(
            name="conveyor_sketcher_hover",
            on_changed_fn=self._on_mouse_moved,
        )
        click_gesture = sc.ClickGesture(
            name="conveyor_sketcher_click",
            on_ended_fn=self._on_mouse_clicked,
        )

        extension = self._get_extension()
        self._manipulator = _SketchViewportManipulator(
            enabled=extension is not None and extension._is_drawing,
            gestures=[hover_gesture, click_gesture],
            viewport_api=self._viewport_api,
        )
        self._manipulator.visible = True
        if extension is not None:
            extension._register_viewport_scene(self)

    def _get_extension(self):
        return self._extension_ref() if self._extension_ref is not None else None

    def _on_mouse_moved(self, sender):
        extension = self._get_extension()
        if extension is not None:
            extension._on_viewport_mouse_moved(
                self._viewport_api, sender.gesture_payload.mouse
            )

    def _on_mouse_clicked(self, sender):
        extension = self._get_extension()
        if extension is not None:
            extension._on_viewport_mouse_clicked(
                self._viewport_api, sender.gesture_payload.mouse
            )

    def set_enabled(self, enabled):
        if self._manipulator is not None:
            self._manipulator.enabled = enabled

    @property
    def visible(self):
        return self._manipulator is None or self._manipulator.visible

    @visible.setter
    def visible(self, value):
        if self._manipulator is not None:
            self._manipulator.visible = value

    def destroy(self):
        if self._manipulator is not None:
            self._manipulator.destroy()
            self._manipulator = None

    def __del__(self):
        self.destroy()


class Extension(omni.ext.IExt):
    def on_startup(self, ext_id):
        global _extension_ref
        _extension_ref = weakref.ref(self)

        self._window = None
        self._draw_button = None
        self._velocity_model = None
        self._status_label = None
        self._menu_items = [
            MenuItemDescription(
                name="CML",
                sub_menu=[
                    MenuItemDescription(
                        name="Conveyor Sketcher",
                        onclick_fn=self._show_main_window,
                    ),
                ],
            ),
        ]
        self._viewport_scenes = []
        self._is_drawing = False
        self._points = []
        self._turn_specs = []
        self._segment_directions = []
        self._segment_breaks = []
        self._straight_runs = []
        self._heading = None
        self._sketch_path = None
        self._stroke_path = None
        self._line_curve = None
        self._preview_curve = None
        self._split_marker_curve = None
        self._stage_key = None
        self._builder_config = None
        self._builder_assets_location = None
        self._track_templates = {}

        self._viewport_scene_registration = RegisterScene(
            _SketchViewportScene, ext_id
        )
        add_menu_items(self._menu_items, "Tools")

    def on_shutdown(self):
        global _extension_ref
        self._stop_drawing()
        self._viewport_scene_registration = None
        _extension_ref = None

        if self._window is not None:
            self._window.destroy()
            self._window = None
        if self._menu_items:
            remove_menu_items(self._menu_items, "Tools")
            self._menu_items.clear()

        self._draw_button = None
        self._velocity_model = None
        self._status_label = None
        self._viewport_scenes = []
        self._sketch_path = None
        self._split_marker_curve = None
        self._track_templates = {}
        self._builder_config = None
        self._builder_assets_location = None
        self._segment_breaks = []
        self._straight_runs = []

    def _show_main_window(self):
        if self._window is None:
            self._window = ui.Window("Conveyor Sketcher", width=300, height=220)
            with self._window.frame:
                with ui.VStack(spacing=8):
                    self._draw_button = ui.Button(
                        "Start Drawing",
                        height=32,
                        clicked_fn=self._toggle_drawing,
                    )
                    ui.Button(
                        "Generate",
                        height=32,
                        clicked_fn=self._generate_conveyors,
                    )
                    with ui.HStack(height=32, spacing=6):
                        ui.Label("Velocity", width=60)
                        self._velocity_model = ui.SimpleFloatModel(0.1)
                        ui.FloatField(
                            model=self._velocity_model,
                            width=90,
                            height=32,
                        )
                        ui.Button(
                            "Set Velocity",
                            height=32,
                            clicked_fn=self._set_selected_velocity,
                        )
                    self._status_label = ui.Label(
                            "Each Start Drawing adds a group; delete it from Stage to remove."
                    )
        self._window.visible = True

    def _toggle_drawing(self):
        if self._is_drawing:
            self._stop_drawing("Drawing finished.")
            return

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._set_status("Open a USD stage before drawing.")
            return
        if get_active_viewport() is None:
            self._set_status("An active viewport is required to draw.")
            return
        try:
            self._get_track_template("NONE")
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
            self._set_status(f"Could not load the straight conveyor asset: {error}")
            return

        self._start_new_sketch(stage)
        self._is_drawing = True
        self._set_viewport_scenes_enabled(True)
        if self._draw_button is not None:
            self._draw_button.text = "Finish Drawing"
        self._set_status("New sketch group created. Click the ground to add points.")

    def _generate_conveyors(self):
        if self._is_drawing:
            self._stop_drawing()

        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._set_status("Open a USD stage before generating conveyors.")
            return
        if self._get_stage_key(stage) != self._stage_key:
            self._prepare_stage(stage)
        if len(self._points) < 2:
            self._set_status("Add at least two points before generating conveyors.")
            return
        if len(self._segment_directions) != len(self._points) - 1:
            self._set_status("Finish drawing the route before generating conveyors.")
            return
        if self._velocity_model is None:
            self._set_status("Open Conveyor Sketcher before generating conveyors.")
            return

        velocity = self._velocity_model.get_value_as_float()
        if not math.isfinite(velocity):
            self._set_status("Velocity must be a finite number.")
            return

        if self._sketch_path is None:
            self._set_status("Start a sketch before generating conveyors.")
            return

        generated_path = f"{self._sketch_path}/Generated"
        generation_started = False
        try:
            from isaacsim.asset.gen.conveyor.ui.impl.conveyor_builder.conveyor_system import (
                ConveyorBuilder,
            )

            straight_template = self._get_track_template("NONE")
            stage_meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
            if stage_meters_per_unit <= 0:
                raise RuntimeError("Could not determine the stage scale units.")
            default_straight_length = (
                straight_template["asset_length"]
                * straight_template["meters_per_unit"]
                / stage_meters_per_unit
            )
            if default_straight_length <= 1e-6:
                raise RuntimeError("The default straight conveyor has zero length.")

            curve_frames = {}
            for index, turn in enumerate(self._turn_specs):
                if turn is None:
                    continue
                template = self._get_track_template(turn["angle"])
                curve_frames[index] = self._make_curve_frame(
                    stage,
                    template,
                    self._points[index],
                    turn["incoming"],
                    turn["turn_sign"],
                )

            for index, direction in enumerate(self._segment_directions):
                start = (
                    curve_frames[index]["end"]
                    if index in curve_frames
                    else self._points[index]
                )
                end = (
                    curve_frames[index + 1]["start"]
                    if index + 1 in curve_frames
                    else self._points[index + 1]
                )
                delta = end - start
                length = Gf.Dot(delta, direction)
                lateral_offset = delta - direction * length
                if length < -1e-4 or lateral_offset.GetLength() > max(
                    0.01, abs(length) * 1e-3
                ):
                    raise RuntimeError(
                        "A turn curve does not line up with its neighboring straight."
                    )

            straight_pieces = {}
            full_length_tolerance = 1e-5 * max(default_straight_length, 1.0)
            for run in self._straight_runs:
                route_points = [run["start"], *run["breakpoints"], run["end"]]
                pieces = []
                for start, end in zip(route_points, route_points[1:]):
                    length = Gf.Dot(end - start, run["direction"])
                    if length <= 1e-6:
                        continue
                    full_length = (
                        abs(length - default_straight_length)
                        <= full_length_tolerance
                    )
                    pieces.append(
                        (start, run["direction"], length, full_length)
                    )
                if pieces:
                    straight_pieces[run["first_index"]] = pieces

            generated_prim = stage.GetPrimAtPath(generated_path)
            if generated_prim.IsValid():
                stage.RemovePrim(generated_prim.GetPath())
            generation_started = True
            UsdGeom.Xform.Define(stage, generated_path)

            builder = ConveyorBuilder(stage)
            generated_world = omni.usd.get_world_transform_matrix(
                stage.GetPrimAtPath(generated_path)
            )
            generated_world_inverse = generated_world.GetInverse()
            for index in range(len(self._points)):
                if index in curve_frames:
                    frame = curve_frames[index]
                    self._add_generated_track(
                        stage,
                        builder,
                        generated_path,
                        generated_world_inverse,
                        frame["track"],
                        frame["pose"],
                        frame["scale"],
                        velocity,
                        frame["mirror_y"],
                    )

                for start, direction, length, full_length in straight_pieces.get(
                    index, []
                ):
                    pose, scale = self._straight_track_pose(
                        stage,
                        straight_template,
                        start,
                        direction,
                        length,
                        full_length,
                    )
                    self._add_generated_track(
                        stage,
                        builder,
                        generated_path,
                        generated_world_inverse,
                        straight_template["track"],
                        pose,
                        scale,
                        velocity,
                    )
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
            if generation_started:
                generated_prim = stage.GetPrimAtPath(generated_path)
                if generated_prim.IsValid():
                    stage.RemovePrim(generated_prim.GetPath())
            self._set_status(f"Generate failed: {error}")
            return

        straight_count = sum(len(pieces) for pieces in straight_pieces.values())
        partial_count = sum(
            1
            for pieces in straight_pieces.values()
            for _, _, _, full_length in pieces
            if not full_length
        )
        full_count = straight_count - partial_count
        curve_count = len(curve_frames)
        self._set_status(
            f"Generated {full_count} full + {partial_count} partial straight(s), "
            f"{curve_count} curve(s); default span {default_straight_length:g}, "
            f"velocity {velocity:g}."
        )

    def _straight_track_pose(
        self, stage, template, start, direction, length, full_length=False
    ):
        stage_meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
        if stage_meters_per_unit <= 0:
            raise RuntimeError("Could not determine the stage scale units.")
        unit_scale = template["meters_per_unit"] / stage_meters_per_unit
        scale = [unit_scale, unit_scale, unit_scale]
        if not full_length:
            scale[template["long_axis"]] = length / template["asset_length"]
        rotation = Gf.Rotation(template["asset_axis"].GetNormalized(), direction)
        scaled_anchor = self._scale_vector(template["start_position"], scale)
        pose = Gf.Matrix4d(1.0)
        pose.SetRotate(rotation)
        pose.SetTranslateOnly(start - rotation.TransformDir(scaled_anchor))
        return pose, Gf.Vec3d(*scale)

    def _add_generated_track(
        self,
        stage,
        builder,
        generated_path,
        generated_world_inverse,
        track,
        pose,
        scale,
        velocity,
        y_direction=1,
    ):
        from isaacsim.asset.gen.conveyor.ui.impl.conveyor_builder.conveyor_system import (
            set_pose_from_transform,
        )

        conveyor_path = builder.add_track(
            track,
            track_anchor=track.get_anchors()[0],
            y_direction=y_direction,
            parent=generated_path,
        )
        conveyor_prim = stage.GetPrimAtPath(conveyor_path)
        if not conveyor_prim.IsValid():
            raise RuntimeError(
                f"Conveyor Track Builder did not create {conveyor_path}."
            )
        self._enable_kinematic_belt(stage, conveyor_path)
        conveyor_graphs = self._find_selected_conveyor_graphs(
            stage, [conveyor_path]
        )
        if not conveyor_graphs:
            raise RuntimeError(
                f"No ConveyorBeltGraph found under {conveyor_path}."
            )
        for graph_prim in conveyor_graphs:
            self._set_graph_velocity(graph_prim, velocity)
        set_pose_from_transform(
            conveyor_prim,
            generated_world_inverse * pose,
            scale,
        )

    def _set_selected_velocity(self):
        stage = omni.usd.get_context().get_stage()
        if stage is None:
            self._set_status("Open a USD stage before setting velocity.")
            return
        if self._velocity_model is None:
            self._set_status("Open Conveyor Sketcher before setting velocity.")
            return

        velocity = self._velocity_model.get_value_as_float()
        if not math.isfinite(velocity):
            self._set_status("Velocity must be a finite number.")
            return

        selection = omni.usd.get_context().get_selection()
        selected_paths = selection.get_selected_prim_paths()
        if not selected_paths:
            self._set_status("Select a conveyor prim before setting velocity.")
            return

        graph_prims = self._find_selected_conveyor_graphs(stage, selected_paths)
        if not graph_prims:
            self._set_status(
                "No ConveyorBeltGraph found under the selected prims."
            )
            return

        try:
            for graph_prim in graph_prims:
                self._set_graph_velocity(graph_prim, velocity)
        except RuntimeError as error:
            self._set_status(f"Set velocity failed: {error}")
            return

        self._set_status(
            f"Set velocity to {velocity:g} on {len(graph_prims)} conveyor graph(s)."
        )

    @staticmethod
    def _set_graph_velocity(graph_prim, velocity):
        velocity_attr = graph_prim.GetAttribute("graph:variable:Velocity")
        if not velocity_attr.IsValid():
            raise RuntimeError(
                f"Conveyor graph has no Velocity variable: {graph_prim.GetPath()}"
            )
        if not velocity_attr.Set(velocity):
            raise RuntimeError(f"Could not set velocity on {graph_prim.GetPath()}.")

    @staticmethod
    def _find_selected_conveyor_graphs(stage, selected_paths):
        graph_prims = {}
        velocity_attribute = "graph:variable:Velocity"

        def graphs_under(root_prim):
            return [
                candidate
                for candidate in Usd.PrimRange(root_prim)
                if candidate.GetName().startswith("ConveyorBeltGraph")
                and candidate.GetAttribute(velocity_attribute).IsValid()
            ]

        for selected_path in selected_paths:
            prim = stage.GetPrimAtPath(selected_path)
            if not prim.IsValid():
                continue

            selected_graphs = graphs_under(prim)
            if selected_graphs:
                graph_prims.update(
                    {str(graph.GetPath()): graph for graph in selected_graphs}
                )
                continue

            ancestor = prim.GetParent()
            while ancestor.IsValid() and not ancestor.IsPseudoRoot():
                if ancestor.GetName().startswith(
                    ("ConveyorTrack", "ConveyorBeltGraph")
                ):
                    ancestor_graphs = graphs_under(ancestor)
                    graph_prims.update(
                        {
                            str(graph.GetPath()): graph
                            for graph in ancestor_graphs
                        }
                    )
                    break
                ancestor = ancestor.GetParent()

        return list(graph_prims.values())

    @staticmethod
    def _enable_kinematic_belt(stage, conveyor_path):
        conveyor_prim = stage.GetPrimAtPath(conveyor_path)
        belt_prim = stage.GetPrimAtPath(f"{conveyor_path}/Belt")
        if not conveyor_prim.IsValid() or not belt_prim.IsValid():
            raise RuntimeError(f"Generated conveyor has no Belt prim: {conveyor_path}")

        body_prim = belt_prim
        while body_prim.IsValid():
            if body_prim.HasAPI(UsdPhysics.RigidBodyAPI):
                rigid_body_api = UsdPhysics.RigidBodyAPI(body_prim)
                kinematic_attr = rigid_body_api.GetKinematicEnabledAttr()
                if not kinematic_attr:
                    kinematic_attr = rigid_body_api.CreateKinematicEnabledAttr()
                if not kinematic_attr.Set(True):
                    raise RuntimeError(
                        f"Could not enable kinematic mode on {body_prim.GetPath()}."
                    )
                return
            if body_prim.GetPath() == conveyor_prim.GetPath():
                break
            body_prim = body_prim.GetParent()

        raise RuntimeError(
            f"No rigid body API found for generated conveyor Belt: {belt_prim.GetPath()}"
        )

    def _stop_drawing(self, message=None):
        if self._preview_curve is not None:
            prim = self._preview_curve.GetPrim()
            if prim.IsValid():
                UsdGeom.Imageable(prim).GetVisibilityAttr().Set(
                    UsdGeom.Tokens.invisible
                )
        self._set_line_visible(True)

        self._is_drawing = False
        self._set_viewport_scenes_enabled(False)
        if self._draw_button is not None:
            self._draw_button.text = "Start Drawing"
        if message:
            self._set_status(message)

    def _register_viewport_scene(self, scene):
        self._viewport_scenes.append(weakref.ref(scene))
        scene.set_enabled(self._is_drawing)

    def _set_viewport_scenes_enabled(self, enabled):
        live_scenes = []
        for scene_ref in self._viewport_scenes:
            scene = scene_ref()
            if scene is not None:
                live_scenes.append(scene_ref)
                scene.set_enabled(enabled)
        self._viewport_scenes = live_scenes

    def _on_viewport_mouse_moved(self, viewport_api, mouse_ndc):
        if not self._is_drawing or not self._points:
            return

        stage = viewport_api.stage
        if stage is None:
            self._stop_drawing("The USD stage was closed.")
            return

        self._prepare_stage(stage)
        if not self._points:
            return
        point = self._ground_point(viewport_api, mouse_ndc, stage)
        if point is None:
            self._set_preview_visible(False)
            self._set_line_visible(True)
            return

        try:
            preview = (
                self._get_route_preview(stage, point)
                if len(self._points) >= 2
                else {
                    "point": point,
                    "turn": None,
                }
            )
            if preview is None:
                self._set_preview_visible(False)
                self._set_line_visible(True)
                return
            self._update_preview(
                stage,
                self._build_sketch_route_points(stage, preview),
            )
            if preview["turn"] is not None:
                angle = 90 if preview["turn"]["angle"] == "HALF" else 180
                self._set_status(f"{angle}-degree curve preview. Click to commit.")
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
            self._set_preview_visible(False)
            self._set_line_visible(True)
            self._set_status(f"Curve preview failed: {error}")

    def _on_viewport_mouse_clicked(self, viewport_api, mouse_ndc):
        if not self._is_drawing:
            return

        stage = viewport_api.stage
        if stage is None:
            self._stop_drawing("The USD stage was closed.")
            return

        self._prepare_stage(stage)
        point = self._ground_point(viewport_api, mouse_ndc, stage)
        if point is None:
            self._set_status("The mouse ray does not intersect the ground plane.")
            return

        if not self._points:
            self._add_point(stage, point)
            self._update_preview(stage, [point, point])
            return

        if len(self._points) == 1:
            direction = point - self._points[0]
            length = direction.GetLength()
            if length <= 1e-6:
                self._set_status("The first two points must be different.")
                return
            self._heading = direction / length
            self._segment_directions.append(self._heading)
            self._add_point(stage, point)
            self._update_preview(stage, self._build_sketch_route_points(stage))
            return

        try:
            preview = self._get_route_preview(stage, point)
        except (OSError, ValueError, RuntimeError, KeyError, IndexError) as error:
            self._set_status(f"Could not place the turn: {error}")
            return
        if preview is None:
            self._set_status("Move farther along the current path before placing a point.")
            return

        turn = preview["turn"]
        self._turn_specs[-1] = turn
        self._segment_directions.append(preview["outgoing"])
        self._heading = preview["outgoing"]
        self._add_point(stage, preview["point"])
        self._update_preview(stage, self._build_sketch_route_points(stage))
        if turn is not None:
            angle = 90 if turn["angle"] == "HALF" else 180
            self._set_status(f"Committed {angle}-degree Conveyor Track Builder curve.")

    def _prepare_stage(self, stage):
        stage_key = self._get_stage_key(stage)
        if stage_key == self._stage_key:
            return

        self._start_new_sketch(stage)
        self._set_status("Stage changed; a new sketch group was started.")

    def _start_new_sketch(self, stage):
        self._set_preview_visible(False)
        UsdGeom.Xform.Define(stage, _SKETCH_ROOT_PATH)
        index = 1
        while stage.GetPrimAtPath(
            f"{_SKETCH_ROOT_PATH}/Sketch_{index:03d}"
        ).IsValid():
            index += 1

        self._points = []
        self._turn_specs = []
        self._segment_directions = []
        self._segment_breaks = []
        self._straight_runs = []
        self._heading = None
        self._sketch_path = f"{_SKETCH_ROOT_PATH}/Sketch_{index:03d}"
        self._stroke_path = None
        self._line_curve = None
        self._preview_curve = None
        self._split_marker_curve = None
        self._stage_key = self._get_stage_key(stage)
        UsdGeom.Xform.Define(stage, self._sketch_path)

    @staticmethod
    def _get_stage_key(stage):
        return (stage.GetRootLayer().identifier, stage.GetSessionLayer().identifier)

    @staticmethod
    def _ground_axes(stage):
        up_axis = UsdGeom.GetStageUpAxis(stage)
        if up_axis == UsdGeom.Tokens.x:
            return Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 1, 0), Gf.Vec3d(0, 0, 1)
        if up_axis == UsdGeom.Tokens.y:
            return Gf.Vec3d(0, 1, 0), Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 0, 1)
        return Gf.Vec3d(0, 0, 1), Gf.Vec3d(1, 0, 0), Gf.Vec3d(0, 1, 0)

    def _ground_point(self, viewport_api, mouse_ndc, stage):
        x, y = float(mouse_ndc[0]), float(mouse_ndc[1])
        if not (-1.0 <= x <= 1.0 and -1.0 <= y <= 1.0):
            return None

        ndc_to_world = viewport_api.ndc_to_world
        near_point = ndc_to_world.Transform(Gf.Vec3d(x, y, 0.1))
        far_point = ndc_to_world.Transform(Gf.Vec3d(x, y, 1.0))
        ray_direction = far_point - near_point
        up_axis, _, _ = self._ground_axes(stage)
        denominator = Gf.Dot(ray_direction, up_axis)
        if abs(denominator) < 1e-6:
            return None

        distance = -Gf.Dot(near_point, up_axis) / denominator
        if distance < 0:
            return None
        return near_point + ray_direction * distance + up_axis * 0.01

    def _get_track_template(self, angle):
        if angle in self._track_templates:
            return self._track_templates[angle]

        from isaacsim.asset.gen.conveyor.ui.impl.conveyor_builder.conveyor_track import (
            ConveyorTrack,
        )
        from isaacsim.asset.gen.conveyor.ui.impl.preferences import (
            ConveyorBuilderPreferences,
        )

        if self._builder_config is None:
            preferences = ConveyorBuilderPreferences()
            with open(preferences.config_file, encoding="utf-8") as config_file:
                self._builder_config = json.load(config_file)
            self._builder_assets_location = preferences.assets_location.replace(
                "\\", "/"
            )
            if not self._builder_assets_location.endswith("/"):
                self._builder_assets_location += "/"

        assets = self._builder_config["assets"]
        straight_selection = next(
            (
                (name, track_config)
                for name, track_config in assets.items()
                if track_config.get("style") == "BELT"
                and track_config.get("type") == "STRAIGHT"
                and track_config.get("angle") == "NONE"
                and track_config.get("ramp") == "FLAT"
            ),
            None,
        )
        if straight_selection is None:
            raise RuntimeError("No flat straight belt is configured in Conveyor Track Builder.")
        straight_config = straight_selection[1]

        if angle == "NONE":
            selected = straight_selection
        else:
            selected = next(
                (
                    (name, track_config)
                    for name, track_config in assets.items()
                    if track_config.get("style") == "BELT"
                    and track_config.get("type") == "STRAIGHT"
                    and track_config.get("angle") == angle
                    and track_config.get("curvature") == "SMALL"
                    and track_config.get("ramp") == "FLAT"
                    and track_config.get("start_level")
                    == straight_config.get("start_level")
                ),
                None,
            )
        if selected is None:
            raise RuntimeError(
                f"No flat {angle} belt curve is configured in Conveyor Track Builder."
            )

        track_name, track_config = selected
        track = ConveyorTrack(
            base_usd=f"{self._builder_assets_location}{track_name}.usd",
            **track_config,
        )
        asset_stage = Usd.Stage.Open(track.base_usd)
        if asset_stage is None:
            raise RuntimeError(
                f"Could not open Conveyor Track Builder asset: {track.base_usd}"
            )
        asset_root = asset_stage.GetDefaultPrim()
        if not asset_root.IsValid():
            raise RuntimeError(f"Conveyor asset has no default prim: {track.base_usd}")
        anchors = track.get_anchors()
        if len(anchors) < 2:
            raise RuntimeError(f"Conveyor asset needs two anchors: {track.base_usd}")

        start_matrix = self._get_track_anchor_matrix(
            asset_stage, asset_root, anchors[0]
        )
        end_matrix = self._get_track_anchor_matrix(
            asset_stage, asset_root, anchors[1]
        )
        start_position = start_matrix.ExtractTranslation()
        end_position = end_matrix.ExtractTranslation()
        axis = end_position - start_position
        asset_length = axis.GetLength()
        if asset_length <= 1e-6:
            raise RuntimeError(f"Conveyor asset has zero anchor length: {track.base_usd}")

        start_direction = start_matrix.ExtractRotation().TransformDir(
            Gf.Vec3d(1, 0, 0)
        ).GetNormalized()
        end_direction = end_matrix.ExtractRotation().TransformDir(
            Gf.Vec3d(1, 0, 0)
        ).GetNormalized()
        long_axis = max(range(3), key=lambda axis_index: abs(axis[axis_index]))
        local_turn = self._cross_2d(start_direction, end_direction)
        if abs(local_turn) <= 1e-6:
            local_turn = self._cross_2d(start_direction, axis)
        if angle != "NONE" and abs(local_turn) <= 1e-6:
            raise RuntimeError(f"Could not determine curve direction: {track.base_usd}")

        template = {
            "track": track,
            "angle": angle,
            "start_position": start_position,
            "end_position": end_position,
            "start_direction": start_direction,
            "end_direction": end_direction,
            "asset_axis": axis,
            "asset_length": asset_length,
            "long_axis": long_axis,
            "meters_per_unit": UsdGeom.GetStageMetersPerUnit(asset_stage),
            "default_turn_sign": 1 if local_turn >= 0 else -1,
        }
        self._track_templates[angle] = template
        return template

    @staticmethod
    def _get_track_anchor_matrix(asset_stage, asset_root, anchor_name):
        if not anchor_name:
            return Gf.Matrix4d(1.0)
        anchor_prim = asset_stage.GetPrimAtPath(
            f"{asset_root.GetPath()}{anchor_name}"
        )
        if not anchor_prim.IsValid():
            raise RuntimeError(f"Conveyor asset anchor not found: {anchor_name}")
        return omni.usd.get_local_transform_matrix(anchor_prim)

    @staticmethod
    def _cross_2d(a, b):
        return a[0] * b[1] - a[1] * b[0]

    @staticmethod
    def _scale_vector(vector, scale):
        return Gf.Vec3d(
            vector[0] * scale[0],
            vector[1] * scale[1],
            vector[2] * scale[2],
        )

    def _make_curve_frame(self, stage, template, waypoint, incoming, turn_sign):
        if UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
            raise RuntimeError("Conveyor Track Builder curves require a Z-up stage.")

        stage_meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
        if stage_meters_per_unit <= 0:
            raise RuntimeError("Could not determine the stage scale units.")
        unit_scale = template["meters_per_unit"] / stage_meters_per_unit
        mirror_y = (
            1
            if turn_sign == template["default_turn_sign"]
            else -1
        )
        scale = (unit_scale, unit_scale * mirror_y, unit_scale)
        start_position = self._scale_vector(template["start_position"], scale)
        end_position = self._scale_vector(template["end_position"], scale)
        start_direction = self._scale_vector(
            template["start_direction"], scale
        ).GetNormalized()
        end_direction = self._scale_vector(
            template["end_direction"], scale
        ).GetNormalized()
        incoming = incoming.GetNormalized()
        rotation = Gf.Rotation(start_direction, incoming)

        translation = waypoint - rotation.TransformDir(start_position)

        pose = Gf.Matrix4d(1.0)
        pose.SetRotate(rotation)
        pose.SetTranslateOnly(translation)
        return {
            "track": template["track"],
            "angle": template["angle"],
            "turn_sign": turn_sign,
            "mirror_y": mirror_y,
            "incoming": incoming,
            "outgoing": rotation.TransformDir(end_direction).GetNormalized(),
            "waypoint": waypoint,
            "start": translation + rotation.TransformDir(start_position),
            "end": translation + rotation.TransformDir(end_position),
            "pose": pose,
            "scale": Gf.Vec3d(*scale),
        }

    def _curve_arc_points(self, stage, frame):
        up_axis, axis_a, axis_b = self._ground_axes(stage)
        left = Gf.Cross(up_axis, frame["incoming"]).GetNormalized()
        if frame["angle"] == "HALF":
            chord = (frame["end"] - frame["start"]).GetLength()
            radius = chord / (2.0 * math.sin(math.pi / 4.0))
            sweep = frame["turn_sign"] * math.pi / 2.0
        else:
            chord = (frame["end"] - frame["start"]).GetLength()
            radius = chord / 2.0
            sweep = frame["turn_sign"] * math.pi
        center = frame["start"] + left * frame["turn_sign"] * radius

        offset = frame["start"] - center
        start_angle = math.atan2(
            Gf.Dot(offset, axis_b), Gf.Dot(offset, axis_a)
        )
        samples = [
            center
            + (
                axis_a * math.cos(start_angle + sweep * fraction)
                + axis_b * math.sin(start_angle + sweep * fraction)
            )
            * radius
            for fraction in (index / 12.0 for index in range(13))
        ]
        samples[0] = frame["start"]
        samples[-1] = frame["end"]
        return samples

    def _get_route_preview(self, stage, cursor_point):
        if self._heading is None or not self._points:
            return None
        origin = self._points[-1]
        pointer_delta = cursor_point - origin
        pointer_length = pointer_delta.GetLength()
        if pointer_length <= 1e-6:
            return None

        pointer_direction = pointer_delta / pointer_length
        dot = max(-1.0, min(1.0, Gf.Dot(self._heading, pointer_direction)))
        threshold = math.sqrt(0.5)
        if dot >= threshold:
            distance = Gf.Dot(pointer_delta, self._heading)
            if distance <= 1e-6:
                return None
            outgoing = self._heading
            return {
                "point": origin + outgoing * distance,
                "turn": None,
                "outgoing": outgoing,
            }

        angle = "HALF" if dot > -threshold else "FULL"
        turn_sign = 1 if self._cross_2d(self._heading, pointer_direction) >= 0 else -1
        template = self._get_track_template(angle)
        frame = self._make_curve_frame(
            stage, template, origin, self._heading, turn_sign
        )
        distance = max(0.0, Gf.Dot(cursor_point - frame["end"], frame["outgoing"]))
        return {
            "point": frame["end"] + frame["outgoing"] * distance,
            "turn": {
                "angle": angle,
                "turn_sign": turn_sign,
                "incoming": self._heading,
            },
            "outgoing": frame["outgoing"],
            "frame": frame,
        }

    def _build_sketch_route_points(self, stage, preview_state=None):
        if not self._points:
            return []

        frames = {}
        for index, turn in enumerate(self._turn_specs):
            if turn is not None:
                template = self._get_track_template(turn["angle"])
                frames[index] = self._make_curve_frame(
                    stage,
                    template,
                    self._points[index],
                    turn["incoming"],
                    turn["turn_sign"],
                )
        if preview_state is not None and preview_state["turn"] is not None:
            frames[len(self._points) - 1] = preview_state["frame"]

        route_points = [self._points[0]]

        def append_if_new(point):
            if (point - route_points[-1]).GetLength() > 1e-6:
                route_points.append(point)

        for index in range(len(self._points) - 1):
            start = frames[index]["end"] if index in frames else self._points[index]
            end = (
                frames[index + 1]["start"]
                if index + 1 in frames
                else self._points[index + 1]
            )
            append_if_new(start)
            if index < len(self._segment_breaks):
                for break_point in self._segment_breaks[index]:
                    append_if_new(break_point)
            append_if_new(end)
            if index + 1 in frames:
                for point in self._curve_arc_points(
                    stage, frames[index + 1]
                )[1:]:
                    append_if_new(point)

        if preview_state is not None:
            append_if_new(preview_state["point"])
        return route_points

    def _recompute_segment_breaks(self, stage):
        self._segment_breaks = [[] for _ in self._segment_directions]
        self._straight_runs = []
        if not self._segment_directions:
            self._rebuild_split_markers(stage)
            return

        template = self._get_track_template("NONE")
        stage_meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
        if stage_meters_per_unit <= 0:
            return
        default_length = (
            template["asset_length"]
            * template["meters_per_unit"]
            / stage_meters_per_unit
        )
        if default_length <= 1e-6:
            return

        curve_frames = {}
        for index, turn in enumerate(self._turn_specs):
            if turn is not None:
                curve_frames[index] = self._make_curve_frame(
                    stage,
                    self._get_track_template(turn["angle"]),
                    self._points[index],
                    turn["incoming"],
                    turn["turn_sign"],
                )

        edges = []
        for index, direction in enumerate(self._segment_directions):
            start = (
                curve_frames[index]["end"]
                if index in curve_frames
                else self._points[index]
            )
            end = (
                curve_frames[index + 1]["start"]
                if index + 1 in curve_frames
                else self._points[index + 1]
            )
            delta = end - start
            length = Gf.Dot(delta, direction)
            lateral_offset = delta - direction * length
            if length <= 1e-6 or lateral_offset.GetLength() > max(
                0.01, abs(length) * 1e-3
            ):
                continue
            edges.append(
                {
                    "index": index,
                    "start": start,
                    "end": end,
                    "direction": direction,
                    "length": length,
                }
            )

        for edge in edges:
            if (
                self._straight_runs
                and edge["index"] == self._straight_runs[-1]["last_index"] + 1
                and edge["index"] not in curve_frames
                and Gf.Dot(
                    self._straight_runs[-1]["direction"], edge["direction"]
                )
                >= 0.999
                and (edge["start"] - self._straight_runs[-1]["end"]).GetLength()
                <= 1e-3
            ):
                run = self._straight_runs[-1]
                run["end"] = edge["end"]
                run["length"] += edge["length"]
                run["last_index"] = edge["index"]
                run["edges"].append(edge)
            else:
                self._straight_runs.append(
                    {
                        "first_index": edge["index"],
                        "last_index": edge["index"],
                        "start": edge["start"],
                        "end": edge["end"],
                        "direction": edge["direction"],
                        "length": edge["length"],
                        "edges": [edge],
                        "breakpoints": [],
                    }
                )

        tolerance = 1e-5 * max(default_length, 1.0)
        for run in self._straight_runs:
            split_count = max(0, math.ceil(run["length"] / default_length) - 1)
            for split_index in range(1, split_count + 1):
                distance = split_index * default_length
                if distance >= run["length"] - tolerance:
                    continue
                split_point = run["start"] + run["direction"] * distance
                run["breakpoints"].append(split_point)

                distance_in_run = 0.0
                for edge in run["edges"]:
                    edge_distance = distance - distance_in_run
                    if edge_distance < edge["length"] - tolerance:
                        if edge_distance > tolerance:
                            self._segment_breaks[edge["index"]].append(
                                edge["start"] + edge["direction"] * edge_distance
                            )
                        break
                    distance_in_run += edge["length"]

        self._rebuild_split_markers(stage)

    def _rebuild_split_markers(self, stage):
        if self._stroke_path is None:
            return

        _, axis_a, axis_b = self._ground_axes(stage)
        half_size = 0.12
        points = []
        counts = []
        for edge_breaks in self._segment_breaks:
            for point in edge_breaks:
                points.extend(
                    [
                        point - axis_a * half_size - axis_b * half_size,
                        point + axis_a * half_size + axis_b * half_size,
                        point - axis_a * half_size + axis_b * half_size,
                        point + axis_a * half_size - axis_b * half_size,
                    ]
                )
                counts.extend([2, 2])

        if not points:
            if self._split_marker_curve is not None:
                prim = self._split_marker_curve.GetPrim()
                if prim.IsValid():
                    UsdGeom.Imageable(prim).GetVisibilityAttr().Set(
                        UsdGeom.Tokens.invisible
                    )
            return

        if self._split_marker_curve is None:
            self._split_marker_curve = self._define_curve(
                stage,
                f"{self._stroke_path}/AutoSplitMarkers",
                counts,
                points,
                (1.0, 0.65, 0.0),
                0.015,
            )
        else:
            self._set_curve_points(self._split_marker_curve, counts, points)
        UsdGeom.Imageable(self._split_marker_curve.GetPrim()).GetVisibilityAttr().Set(
            UsdGeom.Tokens.inherited
        )

    def _create_stroke(self, stage):
        self._stroke_path = f"{self._sketch_path}/Stroke"
        UsdGeom.Xform.Define(stage, self._stroke_path)

    @staticmethod
    def _define_curve(stage, path, counts, points, color, width):
        curve = UsdGeom.BasisCurves.Define(stage, path)
        curve.CreateTypeAttr(UsdGeom.Tokens.linear)
        curve.CreateCurveVertexCountsAttr(counts)
        curve.CreatePointsAttr(
            [Gf.Vec3f(point[0], point[1], point[2]) for point in points]
        )
        curve.CreateWidthsAttr([width])
        curve.SetWidthsInterpolation(UsdGeom.Tokens.constant)
        curve.CreateDisplayColorAttr([Gf.Vec3f(*color)])
        return curve

    @staticmethod
    def _set_curve_points(curve, counts, points):
        curve.GetCurveVertexCountsAttr().Set(counts)
        curve.GetPointsAttr().Set(
            [Gf.Vec3f(point[0], point[1], point[2]) for point in points]
        )

    def _add_point(self, stage, point):
        if self._stroke_path is None:
            self._create_stroke(stage)

        self._points.append(point)
        self._turn_specs.append(None)
        point_index = len(self._points)
        marker_path = f"{self._stroke_path}/Point_{point_index:03d}"
        _, axis_a, axis_b = self._ground_axes(stage)
        half_size = 0.3
        marker_points = [
            point - axis_a * half_size - axis_b * half_size,
            point + axis_a * half_size + axis_b * half_size,
            point - axis_a * half_size + axis_b * half_size,
            point + axis_a * half_size - axis_b * half_size,
        ]
        self._define_curve(
            stage,
            marker_path,
            [2, 2],
            marker_points,
            (1.0, 0.1, 0.1),
            0.02,
        )

        self._recompute_segment_breaks(stage)
        if self._preview_curve is None:
            self._preview_curve = self._define_curve(
                stage,
                f"{self._stroke_path}/Preview",
                [2],
                [point, point],
                (1.0, 0.8, 0.0),
                0.02,
            )

        self._rebuild_sketch_line(stage)
        self._set_status(
            f"{point_index} point(s) added. Move the mouse to preview the next segment."
        )

    def _rebuild_sketch_line(self, stage):
        points = self._build_sketch_route_points(stage)
        if len(points) < 2 or self._stroke_path is None:
            return
        if self._line_curve is None:
            self._line_curve = self._define_curve(
                stage,
                f"{self._stroke_path}/Line",
                [len(points)],
                points,
                (0.1, 0.8, 1.0),
                0.025,
            )
        else:
            self._set_curve_points(self._line_curve, [len(points)], points)
        self._set_line_visible(not self._is_drawing)

    def _update_preview(self, stage, points):
        if len(points) < 2 or self._stroke_path is None:
            self._set_preview_visible(False)
            return
        if self._preview_curve is None:
            self._preview_curve = self._define_curve(
                stage,
                f"{self._stroke_path}/Preview",
                [len(points)],
                points,
                (1.0, 0.8, 0.0),
                0.02,
            )
        else:
            self._set_curve_points(self._preview_curve, [len(points)], points)
        self._set_line_visible(False)
        self._set_preview_visible(True)

    def _set_line_visible(self, visible):
        if self._line_curve is None:
            return
        prim = self._line_curve.GetPrim()
        if prim.IsValid():
            token = (
                UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible
            )
            UsdGeom.Imageable(prim).GetVisibilityAttr().Set(token)

    def _set_preview_visible(self, visible):
        if self._preview_curve is None:
            return
        prim = self._preview_curve.GetPrim()
        if prim.IsValid():
            visibility = (
                UsdGeom.Tokens.inherited if visible else UsdGeom.Tokens.invisible
            )
            UsdGeom.Imageable(prim).GetVisibilityAttr().Set(visibility)

    def _set_status(self, text):
        if self._status_label is not None:
            self._status_label.text = text
