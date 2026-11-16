import math
import time

import omni.ext
import omni.ui as ui
from omni.kit.menu.utils import MenuItemDescription, add_menu_items, remove_menu_items


class Extension(omni.ext.IExt):
	_MINIMAP_WINDOW_WIDTH = 340
	_MINIMAP_WINDOW_HEIGHT = 270
	_MINIMAP_CANVAS_WIDTH = 312
	_MINIMAP_CANVAS_HEIGHT = 208
	_MINIMAP_CONTENT_WIDTH = 320
	_MINIMAP_CONTENT_HEIGHT = 240
	_MINIMAP_UPDATE_INTERVAL = 0.20
	_MINIMAP_POSITION_INTERVAL = 0.5
	_MINIMAP_MARGIN = 8.0
	_MINIMAP_DOT_SIZE = 8.0
	_ORIENTATION_FIXED_XY = "fixed_xy"
	_ORIENTATION_FOLLOW_CAMERA = "follow_camera"
	_MINIMAP_PANEL_COLOR = 0x800C1117
	_MINIMAP_CANVAS_COLOR = 0x80404A55
	_MINIMAP_GEOMETRY_COLOR = 0xFF00FFFF
	_MINIMAP_SELECTED_COLOR = 0xFF00FF00
	_MINIMAP_COI_COLOR = 0xFF0000FF
	_MINIMAP_MODE_ACTIVE_COLOR = 0xFF4A7255
	_MINIMAP_MODE_INACTIVE_COLOR = 0xFF343B44
	
	def on_startup(self, ext_id):
		import omni.kit.app

		self._menu_items = [
			MenuItemDescription(name="CML", sub_menu=[
				MenuItemDescription(name="Minimap", onclick_fn=self._toggle_minimap),
			]),
		]
		self._minimap_window = None
		self._minimap_coi_frame = None
		self._minimap_input_widget = None
		self._minimap_geometry_provider = None
		self._minimap_canvas_width = self._MINIMAP_CANVAS_WIDTH
		self._minimap_canvas_height = self._MINIMAP_CANVAS_HEIGHT
		self._minimap_content_width = self._MINIMAP_CONTENT_WIDTH
		self._minimap_content_height = self._MINIMAP_CONTENT_HEIGHT
		self._minimap_geometry = []
		self._minimap_world_bounds = None
		self._minimap_coi_xy = None
		self._minimap_rotation = 0.0
		self._minimap_orientation = self._ORIENTATION_FIXED_XY
		self._minimap_selected_paths = set()
		self._active_camera_path = None
		self._active_viewport = None
		self._minimap_dragging = False
		self._observed_stage_key = None
		self._stage_notice_key = None
		self._minimap_geometry_dirty = True
		self._minimap_last_update = 0.0
		self._minimap_last_position_update = 0.0
		self._app = omni.kit.app.get_app()
		self._update_subscription = self._app.get_update_event_stream().create_subscription_to_pop(
			self._on_minimap_update,
			name="CML Minimap",
		)
		add_menu_items(self._menu_items, "Tools")

	def on_shutdown(self):
		self._revoke_stage_notice()
		self._update_subscription = None
		self._app = None
		if self._menu_items:
			remove_menu_items(self._menu_items, "Tools")
			self._menu_items.clear()
		self._menu_items = None
		if self._minimap_window:
			self._minimap_window.destroy()
		self._minimap_window = None
		self._minimap_coi_frame = None
		self._minimap_input_widget = None
		self._minimap_geometry_provider = None
		self._minimap_geometry = []
		self._minimap_world_bounds = None
		self._minimap_coi_xy = None
		self._minimap_rotation = 0.0
		self._minimap_orientation = self._ORIENTATION_FIXED_XY
		self._minimap_selected_paths = set()
		self._active_camera_path = None
		self._active_viewport = None
		self._minimap_dragging = False
		self._observed_stage_key = None

	def _toggle_minimap(self):
		if self._minimap_window is None:
			return
		self._minimap_window.visible = not self._minimap_window.visible

	def _set_minimap_orientation(self, orientation):
		if self._minimap_orientation == orientation:
			return
		self._minimap_orientation = orientation
		if self._minimap_window is not None:
			self._minimap_window.frame.rebuild()

	def _on_minimap_update(self, event):
		now = time.monotonic()
		if now - self._minimap_last_update < self._MINIMAP_UPDATE_INTERVAL:
			return
		self._minimap_last_update = now

		viewport, viewport_window = self._get_active_viewport_and_window()
		if viewport is not None:
			self._active_viewport = viewport
		else:
			viewport = self._active_viewport
		if self._minimap_window is None:
			if viewport_window is None:
				return
			self._create_minimap_window(viewport_window)
		if not self._minimap_window.visible:
			return

		if viewport_window is not None and now - self._minimap_last_position_update >= self._MINIMAP_POSITION_INTERVAL:
			self._position_minimap(viewport_window)
			self._minimap_last_position_update = now

		import omni.usd

		stage = omni.usd.get_context().get_stage()
		self._set_stage_notice(stage)
		view_state = self._get_camera_view_state(stage, viewport)
		coi_xy = view_state[0] if view_state is not None else None
		camera_rotation = view_state[1] if view_state is not None else None
		coi_availability_changed = (coi_xy is None) != (self._minimap_coi_xy is None)
		coi_changed = coi_xy != self._minimap_coi_xy
		rotation_changed = False
		if camera_rotation is not None:
			rotation_delta = (camera_rotation - self._minimap_rotation + math.pi) % (2 * math.pi) - math.pi
			if self._minimap_orientation == self._ORIENTATION_FIXED_XY:
				self._minimap_rotation = camera_rotation
			elif abs(rotation_delta) > math.radians(1.0):
				self._minimap_rotation = camera_rotation
				rotation_changed = True
		self._minimap_coi_xy = coi_xy
		selected_paths = self._get_selected_prim_paths()
		selection_changed = selected_paths != self._minimap_selected_paths
		self._minimap_selected_paths = selected_paths

		geometry_changed = False
		if self._minimap_geometry_dirty:
			self._minimap_geometry_dirty = False
			geometry_changed = self._refresh_minimap_geometry(stage)
		if geometry_changed or coi_availability_changed or selection_changed or rotation_changed:
			self._minimap_window.frame.rebuild()
		elif coi_changed:
			self._update_coi_dot()

	def _get_selected_prim_paths(self):
		import omni.usd

		return {
			str(path)
			for path in omni.usd.get_context().get_selection().get_selected_prim_paths()
		}

	def _set_stage_notice(self, stage):
		stage_key = None if stage is None else (
			stage.GetRootLayer().identifier,
			stage.GetSessionLayer().identifier,
		)
		if stage_key == self._observed_stage_key:
			return
		self._revoke_stage_notice()
		self._observed_stage_key = stage_key
		self._minimap_geometry_dirty = True
		if stage is not None:
			from pxr import Tf, Usd

			self._stage_notice_key = Tf.Notice.Register(
				Usd.Notice.ObjectsChanged,
				self._on_stage_objects_changed,
				stage,
			)

	def _revoke_stage_notice(self):
		if self._stage_notice_key is not None:
			self._stage_notice_key.Revoke()
			self._stage_notice_key = None

	def _on_stage_objects_changed(self, notice, sender):
		from pxr import UsdGeom

		paths = tuple(notice.GetResyncedPaths()) + tuple(notice.GetChangedInfoOnlyPaths())
		for path in paths:
			prim_path = path.GetPrimPath() if path.IsPropertyPath() else path
			if self._active_camera_path is not None and prim_path == self._active_camera_path:
				continue
			if path.IsPrimPath():
				self._minimap_geometry_dirty = True
				return
			if not path.IsPropertyPath():
				continue
			property_name = str(path).rsplit(".", 1)[-1]
			if property_name == "visibility" or property_name.startswith("xformOp"):
				self._minimap_geometry_dirty = True
				return
			prim = sender.GetPrimAtPath(prim_path)
			if prim.IsValid() and prim.IsA(UsdGeom.Imageable):
				self._minimap_geometry_dirty = True
				return

	def _get_active_viewport_and_window(self):
		from omni.kit.viewport.utility import get_active_viewport_and_window

		return get_active_viewport_and_window()

	def _create_minimap_window(self, viewport_window):
		self._minimap_window = ui.Window(
			"Minimap",
			width=self._MINIMAP_WINDOW_WIDTH,
			height=self._MINIMAP_WINDOW_HEIGHT,
			visible=False,
		)
		self._minimap_window.frame.style = {"background_color": self._MINIMAP_PANEL_COLOR}
		self._minimap_window.frame.set_build_fn(self._build_minimap)
		self._minimap_window.set_width_changed_fn(self._on_minimap_window_size_changed)
		self._minimap_window.set_height_changed_fn(self._on_minimap_window_size_changed)
		self._position_minimap(viewport_window)
		self._minimap_window.visible = True

	def _on_minimap_window_size_changed(self, *args):
		if self._minimap_window is None:
			return
		canvas_width = max(80.0, float(self._minimap_window.width) - 28.0)
		canvas_height = max(60.0, float(self._minimap_window.height) - 62.0)
		if canvas_width == self._minimap_canvas_width and canvas_height == self._minimap_canvas_height:
			return
		self._minimap_canvas_width = canvas_width
		self._minimap_canvas_height = canvas_height
		self._minimap_content_width = canvas_width + 8.0
		self._minimap_content_height = canvas_height + 32.0
		self._minimap_window.frame.rebuild()

	def _position_minimap(self, viewport_window):
		origin_x = viewport_window.position_x
		origin_y = viewport_window.position_y
		viewport_width = viewport_window.width
		viewport_height = viewport_window.height
		if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (
			origin_x, origin_y, viewport_width, viewport_height,
		)):
			return
		if abs(origin_x) > 100000 or abs(origin_y) > 100000 or viewport_width <= 0 or viewport_height <= 0:
			return

		x = max(origin_x, origin_x + viewport_width - self._minimap_window.width - 12)
		y = max(origin_y, origin_y + viewport_height - self._minimap_window.height - 12)
		self._minimap_window.position_x = x
		self._minimap_window.position_y = y

	def _get_camera_view_state(self, stage, viewport):
		if stage is None or viewport is None:
			return None

		from omni.kit.viewport.utility import get_active_viewport_camera_path
		from omni.kit.viewport.utility.camera_state import ViewportCameraState
		from pxr import UsdGeom

		camera_path = get_active_viewport_camera_path()
		if camera_path is not None:
			self._active_camera_path = camera_path
		else:
			camera_path = self._active_camera_path
		if camera_path is None:
			return None
		camera_prim = stage.GetPrimAtPath(camera_path)
		if not camera_prim.IsValid() or not camera_prim.IsA(UsdGeom.Camera):
			return None

		camera_state = ViewportCameraState(str(camera_path), viewport)
		coi = camera_state.target_world
		position = camera_state.position_world
		coi_x, coi_y = float(coi[0]), float(coi[1])
		position_x, position_y = float(position[0]), float(position[1])
		if not all(math.isfinite(value) for value in (coi_x, coi_y, position_x, position_y)):
			return None
		forward_x = coi_x - position_x
		forward_y = coi_y - position_y
		if math.hypot(forward_x, forward_y) <= 1.0e-6:
			rotation = None
		else:
			rotation = math.atan2(forward_x, forward_y)
		return (coi_x, coi_y), rotation

	def _refresh_minimap_geometry(self, stage):
		if stage is None:
			geometry = []
			world_bounds = None
		else:
			from pxr import Usd, UsdGeom

			bbox_cache = UsdGeom.BBoxCache(Usd.TimeCode.Default(), [UsdGeom.Tokens.default_])
			geometry = []
			min_x = min_y = math.inf
			max_x = max_y = -math.inf
			for prim in stage.Traverse():
				if not prim.IsA(UsdGeom.Imageable):
					continue
				imageable = UsdGeom.Imageable(prim)
				if imageable.ComputeVisibility(Usd.TimeCode.Default()) == UsdGeom.Tokens.invisible:
					continue
				bounds = bbox_cache.ComputeWorldBound(prim).ComputeAlignedRange()
				if bounds.IsEmpty():
					continue
				bounds_min = bounds.GetMin()
				bounds_max = bounds.GetMax()
				values = (
					float(bounds_min[0]), float(bounds_min[1]),
					float(bounds_max[0]), float(bounds_max[1]),
				)
				if not all(math.isfinite(value) for value in values):
					continue
				prim_min_x, prim_min_y, prim_max_x, prim_max_y = values
				geometry.append((
					str(prim.GetPath()),
					(prim_min_x + prim_max_x) / 2,
					(prim_min_y + prim_max_y) / 2,
					prim_max_x - prim_min_x,
					prim_max_y - prim_min_y,
				))
				min_x = min(min_x, prim_min_x)
				min_y = min(min_y, prim_min_y)
				max_x = max(max_x, prim_max_x)
				max_y = max(max_y, prim_max_y)

			world_bounds = (min_x, max_x, min_y, max_y) if geometry else None

		changed = (
			geometry != self._minimap_geometry
			or world_bounds != self._minimap_world_bounds
		)
		self._minimap_geometry = geometry
		self._minimap_world_bounds = world_bounds
		return changed

	def _map_transform(self):
		if self._minimap_world_bounds is None:
			center_x, center_y = self._minimap_coi_xy or (0.0, 0.0)
			min_x, max_x = center_x - 5.0, center_x + 5.0
			min_y, max_y = center_y - 5.0, center_y + 5.0
		else:
			min_x, max_x, min_y, max_y = self._minimap_world_bounds
			center_x = (min_x + max_x) / 2
			center_y = (min_y + max_y) / 2

		rotation = self._minimap_rotation if self._minimap_orientation == self._ORIENTATION_FOLLOW_CAMERA else 0.0
		cosine = math.cos(rotation)
		sine = math.sin(rotation)
		rotated_corners = []
		for x, y in ((min_x, min_y), (min_x, max_y), (max_x, min_y), (max_x, max_y)):
			delta_x = x - center_x
			delta_y = y - center_y
			rotated_corners.append((
				cosine * delta_x - sine * delta_y,
				sine * delta_x + cosine * delta_y,
			))
		half_width = max(abs(x) for x, _ in rotated_corners)
		half_height = max(abs(y) for _, y in rotated_corners)
		padding = max(half_width, half_height, 1.0e-3) * 0.06
		half_width += padding
		half_height += padding
		usable_width = self._minimap_canvas_width - 2 * self._MINIMAP_MARGIN
		usable_height = self._minimap_canvas_height - 2 * self._MINIMAP_MARGIN
		scale = min(usable_width / (2 * half_width), usable_height / (2 * half_height))
		return center_x, center_y, cosine, sine, scale

	def _world_to_minimap(self, x, y, transform):
		center_x, center_y, cosine, sine, scale = transform
		delta_x = x - center_x
		delta_y = y - center_y
		rotated_x = cosine * delta_x - sine * delta_y
		rotated_y = sine * delta_x + cosine * delta_y
		map_x = self._minimap_canvas_width / 2 + rotated_x * scale
		map_y = self._minimap_canvas_height / 2 - rotated_y * scale
		return (
			min(max(map_x, 0.0), self._minimap_canvas_width),
			min(max(map_y, 0.0), self._minimap_canvas_height),
			scale,
		)

	def _minimap_to_world(self, x, y):
		center_x, center_y, cosine, sine, scale = self._map_transform()
		rotated_x = (x - self._minimap_canvas_width / 2) / scale
		rotated_y = (self._minimap_canvas_height / 2 - y) / scale
		return (
			center_x + cosine * rotated_x + sine * rotated_y,
			center_y - sine * rotated_x + cosine * rotated_y,
		)

	def _update_coi_dot(self):
		if self._minimap_coi_frame is not None:
			self._minimap_coi_frame.rebuild()

	def _on_minimap_mouse_pressed(self, x, y, button, modifier):
		if button != 0:
			return
		self._minimap_dragging = True
		point = self._minimap_event_position(x, y)
		if point is not None:
			self._move_camera_to_map_point(*point)

	def _on_minimap_mouse_moved(self, x, y, *args):
		if self._minimap_dragging:
			point = self._minimap_event_position(x, y)
			if point is not None:
				self._move_camera_to_map_point(*point)

	def _on_minimap_mouse_released(self, x, y, button, modifier):
		self._minimap_dragging = False

	def _minimap_event_position(self, x, y):
		if self._minimap_input_widget is None:
			return None
		return (
			x - self._minimap_input_widget.screen_position_x,
			y - self._minimap_input_widget.screen_position_y,
		)

	def _move_camera_to_map_point(self, x, y):
		if self._active_viewport is None or self._active_camera_path is None:
			return

		import omni.usd
		from omni.kit.viewport.utility.camera_state import ViewportCameraState
		from pxr import Gf, UsdGeom

		stage = omni.usd.get_context().get_stage()
		if stage is None:
			return
		camera_prim = stage.GetPrimAtPath(self._active_camera_path)
		if not camera_prim.IsValid() or not camera_prim.IsA(UsdGeom.Camera):
			return

		map_x = min(max(x, 0.0), self._minimap_canvas_width)
		map_y = min(max(y, 0.0), self._minimap_canvas_height)
		world_x, world_y = self._minimap_to_world(map_x, map_y)
		camera_state = ViewportCameraState(str(self._active_camera_path), self._active_viewport)
		current_target = camera_state.target_world
		camera_state.set_target_world(
			Gf.Vec3d(world_x, world_y, float(current_target[2])),
			False,
		)
		self._minimap_coi_xy = (world_x, world_y)
		self._update_coi_dot()

	def _build_minimap(self):
		self._minimap_coi_frame = None
		self._minimap_input_widget = None
		self._minimap_geometry_provider = None
		transform = self._map_transform()
		with ui.ZStack(width=self._minimap_content_width, height=self._minimap_content_height):
			ui.Rectangle(
				width=self._minimap_content_width,
				height=self._minimap_content_height,
				style={"background_color": self._MINIMAP_PANEL_COLOR},
			)
			with ui.HStack(width=self._minimap_content_width, height=24, spacing=4):
				ui.Button(
					"Fixed XY",
					width=(self._minimap_content_width - 4) / 2,
					clicked_fn=lambda: self._set_minimap_orientation(self._ORIENTATION_FIXED_XY),
					style={
						"background_color": self._MINIMAP_MODE_ACTIVE_COLOR
						if self._minimap_orientation == self._ORIENTATION_FIXED_XY
						else self._MINIMAP_MODE_INACTIVE_COLOR,
						"font_size": 14,
					},
				)
				ui.Button(
					"Follow Camera",
					width=(self._minimap_content_width - 4) / 2,
					clicked_fn=lambda: self._set_minimap_orientation(self._ORIENTATION_FOLLOW_CAMERA),
					style={
						"background_color": self._MINIMAP_MODE_ACTIVE_COLOR
						if self._minimap_orientation == self._ORIENTATION_FOLLOW_CAMERA
						else self._MINIMAP_MODE_INACTIVE_COLOR,
						"font_size": 14,
					},
				)
			with ui.Placer(offset_x=4, offset_y=28):
				with ui.ZStack(width=self._minimap_canvas_width, height=self._minimap_canvas_height):
					ui.Rectangle(
						width=self._minimap_canvas_width,
						height=self._minimap_canvas_height,
						style={"background_color": self._MINIMAP_CANVAS_COLOR},
					)
					self._minimap_geometry_provider = ui.ByteImageProvider()
					self._minimap_geometry_provider.set_bytes_data(
						list(self._rasterize_minimap_geometry(transform)),
						[int(round(self._minimap_canvas_width)), int(round(self._minimap_canvas_height))],
					)
					ui.ImageWithProvider(
						self._minimap_geometry_provider,
						width=self._minimap_canvas_width,
						height=self._minimap_canvas_height,
						pixel_aligned=True,
					)

					self._minimap_coi_frame = ui.Frame(
						width=self._minimap_canvas_width,
						height=self._minimap_canvas_height,
						build_fn=self._build_coi_marker,
					)
					self._minimap_input_widget = ui.Rectangle(
						width=self._minimap_canvas_width,
						height=self._minimap_canvas_height,
						style={"background_color": 0x00000000},
						mouse_pressed_fn=self._on_minimap_mouse_pressed,
						mouse_moved_fn=self._on_minimap_mouse_moved,
						mouse_released_fn=self._on_minimap_mouse_released,
						opaque_for_mouse_events=True,
					)

	def _rasterize_minimap_geometry(self, transform):
		width = int(round(self._minimap_canvas_width))
		height = int(round(self._minimap_canvas_height))
		pixels = bytearray(width * height * 4)
		scale = transform[4]
		minimum_world_size = 3.0 / scale

		geometry = sorted(
			self._minimap_geometry,
			key=lambda entry: self._is_selected_geometry(entry[0]),
		)
		for path, center_x, center_y, world_width, world_height in geometry:
			world_width = max(world_width, minimum_world_size)
			world_height = max(world_height, minimum_world_size)
			corners = (
				(center_x - world_width / 2, center_y - world_height / 2),
				(center_x + world_width / 2, center_y - world_height / 2),
				(center_x + world_width / 2, center_y + world_height / 2),
				(center_x - world_width / 2, center_y + world_height / 2),
			)
			points = [
				self._world_to_minimap(x, y, transform)[:2]
				for x, y in corners
			]
			center = self._world_to_minimap(center_x, center_y, transform)
			extent = max(
				max(point[0] for point in points) - min(point[0] for point in points),
				max(point[1] for point in points) - min(point[1] for point in points),
			)
			factor = min(1.0, 18.0 / extent) if extent > 18.0 else max(1.0, 3.0 / extent) if extent > 0 else 1.0
			points = [
				(
					center[0] + (x - center[0]) * factor,
					center[1] + (y - center[1]) * factor,
				)
				for x, y in points
			]
			color = (
				self._MINIMAP_SELECTED_COLOR
				if self._is_selected_geometry(path)
				else self._MINIMAP_GEOMETRY_COLOR
			)
			self._fill_minimap_polygon(
				pixels,
				width,
				height,
				points,
				self._ui_color_to_rgba(color),
			)
		return bytes(pixels)

	def _fill_minimap_polygon(self, pixels, width, height, points, color):
		min_y = max(0, int(math.floor(min(point[1] for point in points))))
		max_y = min(height - 1, int(math.ceil(max(point[1] for point in points))))
		for y in range(min_y, max_y + 1):
			scan_y = y + 0.5
			intersections = []
			for index, (x1, y1) in enumerate(points):
				x2, y2 = points[(index + 1) % len(points)]
				if (y1 <= scan_y < y2) or (y2 <= scan_y < y1):
					intersections.append(x1 + (scan_y - y1) * (x2 - x1) / (y2 - y1))
			intersections.sort()
			for left, right in zip(intersections[0::2], intersections[1::2]):
				start_x = max(0, int(math.ceil(left - 0.5)))
				end_x = min(width - 1, int(math.floor(right - 0.5)))
				for x in range(start_x, end_x + 1):
					offset = (y * width + x) * 4
					pixels[offset:offset + 4] = color

	def _ui_color_to_rgba(self, color):
		return bytes((
			color & 0xFF,
			(color >> 8) & 0xFF,
			(color >> 16) & 0xFF,
			(color >> 24) & 0xFF,
		))

	def _build_coi_marker(self):
		if self._minimap_coi_xy is None:
			return
		camera_x, camera_y, _ = self._world_to_minimap(
			self._minimap_coi_xy[0],
			self._minimap_coi_xy[1],
			self._map_transform(),
		)
		with ui.Placer(offset_x=camera_x - self._MINIMAP_DOT_SIZE / 2, offset_y=camera_y - self._MINIMAP_DOT_SIZE / 2):
			ui.Circle(
				width=self._MINIMAP_DOT_SIZE,
				height=self._MINIMAP_DOT_SIZE,
				style={"background_color": self._MINIMAP_COI_COLOR},
			)

	def _is_selected_geometry(self, prim_path):
		return any(
			prim_path == selected_path
			or prim_path.startswith(selected_path.rstrip("/") + "/")
			or selected_path.startswith(prim_path.rstrip("/") + "/")
			for selected_path in self._minimap_selected_paths
		)

