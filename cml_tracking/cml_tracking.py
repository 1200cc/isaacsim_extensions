import omni.ext
import omni.ui as ui
import omni.usd
import omni.kit.commands
import omni.kit.viewport.utility as viewport_utils
import numpy as np
import weakref
import isaacsim.core.experimental.utils.backend as backend_utils
from omni.kit.menu.utils import MenuItemDescription, add_menu_items, remove_menu_items
from isaacsim.core.experimental.prims import RigidPrim, XformPrim
from isaacsim.core.rendering_manager import RenderingEvent, RenderingManager
from isaacsim.core.utils.viewports import set_camera_view
from pxr import Gf, Usd, UsdGeom, UsdPhysics

class Extension(omni.ext.IExt):
	def on_startup(self, ext_id):
		self._menu_items = [MenuItemDescription(name="CML", sub_menu=[MenuItemDescription(name="Tracking", onclick_fn=self._weak_method_callback("_show_main_window"))])]
		self._window = None
		self._enabled_model = None
		self._enabled_changed_callback_id = None
		self._status_label = None
		self._render_callback_id = None
		self._render_callback = None
		self._render_callback_seen = False
		self._viewport_api = None
		self._camera_path = None
		self._target_path = None
		self._target_xform = None
		self._target_pose_backend = None
		self._target_pose_path = None
		self._camera_up = None
		self._camera_heading = None
		self._camera_side_angle = None
		self._camera_initial_horizontal_offset = None
		self._camera_horizontal_distance = None
		self._camera_height_offset = None
		self._last_camera_target_position = None
		add_menu_items(self._menu_items, "Tools")

	def on_shutdown(self):
		self._stop_tracking(clear_target=True)
		if self._enabled_model is not None and self._enabled_changed_callback_id is not None:
			self._enabled_model.remove_value_changed_fn(self._enabled_changed_callback_id)
			self._enabled_changed_callback_id = None
		if self._window:
			self._window.destroy()
			self._window = None
		if self._menu_items:
			remove_menu_items(self._menu_items, "Tools")
			self._menu_items.clear()
		self._enabled_model = None
		self._status_label = None
		self._menu_items = []

	def _show_main_window(self):
		if self._window is None:
			self._window = ui.Window("Tracking", width=300, height=130)
			with self._window.frame:
				with ui.VStack(spacing=8):
					with ui.HStack():
						ui.Label("Enabled")
						self._enabled_model = ui.SimpleBoolModel(False)
						ui.CheckBox(model=self._enabled_model, width=20)
					ui.Button("Set Selected", clicked_fn=self._weak_method_callback("_set_selected_target"))
					self._status_label = ui.Label("Select an object in the Stage to begin.")
			self._enabled_changed_callback_id = self._enabled_model.add_value_changed_fn(self._on_enabled_changed)
		self._window.visible = True

	def _weak_method_callback(self, method_name):
		extension_ref = weakref.ref(self)

		def callback(*args, **kwargs):
			extension = extension_ref()
			if extension is not None:
				return getattr(extension, method_name)(*args, **kwargs)

		return callback

	def _on_enabled_changed(self, model):
		if model.as_bool:
			try:
				self._start_tracking()
			except RuntimeError as error:
				model.set_value(False)
				self._set_status(str(error))
		else:
			self._stop_tracking()
			self._set_status(f"Tracking paused: {self._target_pose_path}" if self._target_path else "Tracking disabled.")

	def _set_selected_target(self):
		selected_paths = omni.usd.get_context().get_selection().get_selected_prim_paths()
		if not selected_paths:
			self._set_status("Select an object before clicking Set Selected.")
			return
		try:
			self._capture_target(selected_paths[0])
		except RuntimeError as error:
			self._set_status(str(error))
			return
		state = "Tracking" if self._render_callback_id is not None else "Target set"
		self._set_status(f"{state}: {self._target_pose_path} [{self._target_pose_backend}]")

	def _start_tracking(self):
		if self._target_path is None or self._target_xform is None:
			raise RuntimeError("Click Set Selected before enabling camera tracking.")
		stage = omni.usd.get_context().get_stage()
		if stage is None:
			raise RuntimeError("No USD stage is open.")
		if not stage.GetPrimAtPath(self._target_pose_path).IsValid():
			raise RuntimeError("The locked target is no longer available. Click Set Selected again.")
		if not stage.GetPrimAtPath(self._camera_path).IsValid():
			raise RuntimeError("The viewport camera is no longer available.")
		if self._render_callback_id is not None:
			return
		self._render_callback_seen = False
		self._render_callback = self._weak_method_callback("_on_render_frame")
		self._render_callback_id = RenderingManager.register_callback(RenderingEvent.NEW_FRAME, callback=self._render_callback)
		self._set_status(f"Tracking {self._target_pose_path} [{self._target_pose_backend}]")

	def _capture_target(self, target_path):
		stage = omni.usd.get_context().get_stage()
		if stage is None:
			raise RuntimeError("No USD stage is open.")

		viewport_api = viewport_utils.get_active_viewport()
		camera_path = viewport_utils.get_active_viewport_camera_path()
		if viewport_api is None or camera_path is None or not str(camera_path):
			raise RuntimeError("No active viewport camera is available.")
		self._viewport_api = viewport_api
		self._camera_path = str(camera_path)

		target_xform, pose_backend, pose_path = self._create_target_pose_source(stage, target_path)
		if pose_backend == "tensor" and not target_xform.is_physics_tensor_entity_valid():
			pose_backend = "fabric"
		target_position = self._get_world_position(target_xform, pose_backend)
		camera_position = self._get_camera_position(stage)

		self._target_path = target_path
		self._target_xform = target_xform
		self._target_pose_backend = pose_backend
		self._target_pose_path = pose_path
		set_camera_view(eye=camera_position, target=target_position, camera_prim_path=self._camera_path, viewport_api=self._viewport_api)
		self._initialize_follow_camera(stage, target_position, camera_position)

	def _initialize_follow_camera(self, stage, target_position, camera_position):
		up_axis = UsdGeom.GetStageUpAxis(stage)
		if up_axis == UsdGeom.Tokens.x:
			self._camera_up = np.array([1.0, 0.0, 0.0])
		elif up_axis == UsdGeom.Tokens.y:
			self._camera_up = np.array([0.0, 1.0, 0.0])
		else:
			self._camera_up = np.array([0.0, 0.0, 1.0])

		camera_offset = camera_position - target_position
		self._camera_height_offset = float(np.dot(camera_offset, self._camera_up))
		horizontal_offset = camera_offset - self._camera_height_offset * self._camera_up
		self._camera_horizontal_distance = float(np.linalg.norm(horizontal_offset))
		if self._camera_horizontal_distance <= 1e-6:
			fallback_offset = (
				np.array([0.0, 1.0, 0.0])
				if up_axis == UsdGeom.Tokens.x
				else np.array([1.0, 0.0, 0.0])
			)
			fallback_offset -= np.dot(fallback_offset, self._camera_up) * self._camera_up
			fallback_offset /= np.linalg.norm(fallback_offset)
			self._camera_horizontal_distance = max(abs(self._camera_height_offset) * 0.25, 1.0)
			horizontal_offset = fallback_offset * self._camera_horizontal_distance
		self._camera_initial_horizontal_offset = horizontal_offset.copy()
		self._camera_heading = None
		self._camera_side_angle = None
		self._last_camera_target_position = target_position.copy()

	def _get_camera_position(self, stage):
		camera_prim = stage.GetPrimAtPath(self._camera_path)
		if not camera_prim.IsValid():
			raise RuntimeError("The viewport camera is no longer available.")
		return np.array(UsdGeom.Xformable(camera_prim).ComputeLocalToWorldTransform(Usd.TimeCode.Default()).ExtractTranslation(), dtype=np.float64)

	def _on_render_frame(self, event):
		if not self._render_callback_seen:
			self._render_callback_seen = True
			self._set_status(f"Tracking {self._target_pose_path} [{self._target_pose_backend}]")

		context = omni.usd.get_context()
		stage = context.get_stage()
		if stage is None:
			self._disable_tracking("The USD stage was closed; tracking stopped.")
			return

		if self._target_xform is None or self._target_pose_path is None:
			self._disable_tracking("No target is set; click Set Selected.")
			return
		if not stage.GetPrimAtPath(self._target_pose_path).IsValid():
			self._disable_tracking("The tracked object is no longer available.")
			return

		if not stage.GetPrimAtPath(self._camera_path).IsValid():
			self._disable_tracking("The viewport camera is no longer available; tracking stopped.")
			return
		try:
			target_position = self._get_world_position(
				self._target_xform, self._target_pose_backend
			)
			delta = target_position - self._last_camera_target_position
			moved = np.linalg.norm(delta) > 1e-9
			if moved:
				horizontal_delta = delta - np.dot(delta, self._camera_up) * self._camera_up
				horizontal_distance = np.linalg.norm(horizontal_delta)
				if horizontal_distance > 1e-5:
					new_heading = horizontal_delta / horizontal_distance
					if self._camera_heading is None:
						self._camera_heading = new_heading
						self._camera_side_angle = self._get_side_angle(-new_heading, self._camera_initial_horizontal_offset)
					else:
						blended_heading = 0.65 * self._camera_heading + 0.35 * new_heading
						blended_length = np.linalg.norm(blended_heading)
						self._camera_heading = (
							blended_heading / blended_length
							if blended_length > 1e-6
							else new_heading
						)
				self._last_camera_target_position = target_position.copy()
				self._move_camera(stage, target_position)
		except RuntimeError as error:
			self._disable_tracking(str(error))
			return

	def _create_target_pose_source(self, stage, target_path):
		selected_prim = stage.GetPrimAtPath(target_path)
		if not selected_prim.IsValid():
			raise RuntimeError("The selected object is no longer available.")

		mesh_prims = (
			[selected_prim]
			if selected_prim.IsA(UsdGeom.Mesh)
			else [
				prim for prim in Usd.PrimRange(selected_prim)
				if prim != selected_prim and prim.IsA(UsdGeom.Mesh)
			]
		)
		if len(mesh_prims) == 1:
			mesh_path = str(mesh_prims[0].GetPath())
			if mesh_prims[0].HasAPI(UsdPhysics.RigidBodyAPI):
				return RigidPrim(mesh_path), "tensor", mesh_path
			return XformPrim(mesh_path), "fabric", mesh_path

		ancestor = selected_prim
		while ancestor.IsValid() and not ancestor.IsPseudoRoot():
			if ancestor.HasAPI(UsdPhysics.RigidBodyAPI):
				rigid_body_path = str(ancestor.GetPath())
				return RigidPrim(rigid_body_path), "tensor", rigid_body_path
			ancestor = ancestor.GetParent()

		descendant_bodies = [
			prim for prim in Usd.PrimRange(selected_prim)
			if prim != selected_prim and prim.HasAPI(UsdPhysics.RigidBodyAPI)
		]
		if len(descendant_bodies) > 1:
			raise RuntimeError(
				"Select a prim containing only one rigid body, or select the rigid body itself."
			)
		if descendant_bodies:
			rigid_body_path = str(descendant_bodies[0].GetPath())
			return RigidPrim(rigid_body_path), "tensor", rigid_body_path

		return XformPrim(target_path), "fabric", target_path

	def _get_world_position(self, target_xform, pose_backend):
		with backend_utils.use_backend(pose_backend, raise_on_fallback=pose_backend == "tensor"):
			positions, _ = target_xform.get_world_poses()
		return np.array(positions.numpy()[0], dtype=np.float64)

	def _get_side_angle(self, rear_direction, camera_offset):
		horizontal_offset = camera_offset - np.dot(camera_offset, self._camera_up) * self._camera_up
		length = np.linalg.norm(horizontal_offset)
		if length <= 1e-6:
			return 0.0
		offset_direction = horizontal_offset / length
		return float(np.arctan2(np.dot(np.cross(rear_direction, offset_direction), self._camera_up), np.dot(rear_direction, offset_direction)))

	def _move_camera(self, stage, target_position):
		camera_prim = stage.GetPrimAtPath(self._camera_path)
		camera_xformable = UsdGeom.Xformable(camera_prim)
		time_code = Usd.TimeCode.Default()
		parent_inverse = camera_xformable.ComputeParentToWorldTransform(time_code).GetInverse()
		old_world_transform = camera_xformable.ComputeLocalToWorldTransform(time_code)
		old_local_transform = old_world_transform * parent_inverse
		if self._camera_heading is None or self._camera_side_angle is None:
			horizontal_offset = self._camera_initial_horizontal_offset
		else:
			rear_direction = -self._camera_heading
			angle = self._camera_side_angle
			horizontal_direction = (
				rear_direction * np.cos(angle)
				+ np.cross(self._camera_up, rear_direction) * np.sin(angle)
			)
			horizontal_offset = horizontal_direction * self._camera_horizontal_distance
		camera_eye = (
			target_position
			+ horizontal_offset
			+ self._camera_up * self._camera_height_offset
		)
		new_world_transform = Gf.Matrix4d(1.0).SetLookAt(Gf.Vec3d(*camera_eye), Gf.Vec3d(*target_position), Gf.Vec3d(*self._camera_up)).GetInverse()
		new_local_transform = new_world_transform * parent_inverse
		omni.kit.commands.create("TransformPrimCommand", path=self._camera_path, new_transform_matrix=new_local_transform, old_transform_matrix=old_local_transform, time_code=time_code, usd_context_name=self._viewport_api.usd_context_name).do()

	def _disable_tracking(self, message):
		self._stop_tracking(clear_target=True)
		if self._enabled_model is not None and self._enabled_model.as_bool:
			self._enabled_model.set_value(False)
		self._set_status(message)

	def _stop_tracking(self, clear_target=False):
		if self._render_callback_id is not None:
			RenderingManager.deregister_callback(self._render_callback_id)
			self._render_callback_id = None
		self._render_callback = None
		self._render_callback_seen = False
		if clear_target:
			self._viewport_api = None
			self._camera_path = None
			self._target_path = None
			self._target_xform = None
			self._target_pose_backend = None
			self._target_pose_path = None
			self._last_camera_target_position = None
			self._camera_up = None
			self._camera_heading = None
			self._camera_side_angle = None
			self._camera_initial_horizontal_offset = None
			self._camera_horizontal_distance = None
			self._camera_height_offset = None

	def _set_status(self, text):
		if self._status_label is not None:
			self._status_label.text = text
