import asyncio
import io
import math
import os

import omni.ext
import omni.ui as ui
from omni.kit.menu.utils import MenuItemDescription, add_menu_items, remove_menu_items
from omni.kit.window.filepicker import FilePickerDialog


class Extension(omni.ext.IExt):
	_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp")
	_DEFAULT_MAX_BLOCK_COUNT = 1024
	_BLOCK_LENGTH_M = 0.05
	_BLOCK_WIDTH_M = 0.01
	_BLOCK_HEIGHT_M = 0.10
	_CENTER_SPACING_M = 0.05
	_YAW_DEGREES = 45.0
	_INITIAL_TILT_DEGREES = 8.0

	def on_startup(self, ext_id):
		import omni.timeline

		self._menu_items = [
			MenuItemDescription(name="CML", sub_menu=[
				MenuItemDescription(name="Dominoes", onclick_fn=self._show_main_window),
			]),
		]
		self._main_window = None
		self._file_picker = None
		self._selected_image_label = None
		self._image_preview = None
		self._generate_button = None
		self._progress_bar = None
		self._max_block_count_model = None
		self._generation_task = None
		self._timeline = omni.timeline.get_timeline_interface()
		self._timeline_event_types = (
			int(omni.timeline.TimelineEventType.PLAY),
			int(omni.timeline.TimelineEventType.PAUSE),
			int(omni.timeline.TimelineEventType.STOP),
		)
		self._timeline_subscription = self._timeline.get_timeline_event_stream().create_subscription_to_pop(self._on_timeline_event, name="CML Dominoes timeline")
		self._selected_image_path = None
		self._generated_root_path = None
		add_menu_items(self._menu_items, "Tools")

	def on_shutdown(self):
		if self._generation_task:
			self._generation_task.cancel()
			self._generation_task = None
		self._timeline_subscription = None
		self._timeline = None
		if self._menu_items:
			remove_menu_items(self._menu_items, "Tools")
			self._menu_items.clear()
		self._menu_items = None
		if self._main_window:
			self._main_window.destroy()
		self._main_window = None
		self._image_preview = None
		self._selected_image_label = None
		self._generate_button = None
		self._progress_bar = None
		self._max_block_count_model = None
		if self._file_picker:
			self._file_picker.set_click_apply_handler(lambda filename, dirname: None)
			self._file_picker.set_item_filter_fn(lambda file_item: True)
			self._file_picker.destroy()
		self._file_picker = None
		self._selected_image_path = None

	def _show_main_window(self):
		if self._main_window is None:
			self._main_window = ui.Window("Dominoes", width=440, height=380)
			with self._main_window.frame:
				with ui.VStack(spacing=1):
					with ui.HStack(height=24, spacing=4):
						ui.Label("Maximum block count", width=200, style={"font_size": 16})
						self._max_block_count_model = ui.SimpleIntModel(self._DEFAULT_MAX_BLOCK_COUNT)
						self._max_block_count_model.set_min(1)
						ui.IntField(self._max_block_count_model, width=120, style={"font_size": 16})
					ui.Button("Select Image", clicked_fn=self._open_image_picker, style={"font_size": 16})
					self._selected_image_label = ui.Label("No image selected", style={"font_size": 14})
					self._image_preview = ui.Image("", width=360, height=220, visible=False)
					self._generate_button = ui.Button("Generate", clicked_fn=self._start_generation, enabled=False, style={"font_size": 16})
					self._progress_bar = ui.ProgressBar(height=12, visible=False)
		self._main_window.visible = True

	def _on_timeline_event(self, event):
		if event.type in self._timeline_event_types:
			self._update_generate_button()

	def _update_generate_button(self):
		if self._generate_button:
			is_generating = self._generation_task and not self._generation_task.done()
			self._generate_button.enabled = bool(self._selected_image_path) and not self._timeline.is_playing() and not is_generating

	def _open_image_picker(self):
		if self._file_picker is None:
			self._file_picker = FilePickerDialog(
				"Select Image",
				apply_button_label="Select",
				click_apply_handler=self._on_image_selected,
				item_filter_options=["Image files (*.png, *.jpg, *.jpeg, *.bmp, *.tif, *.tiff, *.webp)", "All files (*)"],
				item_filter_fn=self._filter_image_files,
			)
		self._file_picker.show()

	def _filter_image_files(self, item):
		if not item or item.is_folder:
			return True
		if self._file_picker.current_filter_option == 0:
			return item.path.lower().endswith(self._IMAGE_EXTENSIONS)
		return True

	def _on_image_selected(self, filename, dirname):
		if "://" in dirname:
			full_path = dirname.rstrip("/") + "/" + filename
		else:
			full_path = os.path.join(dirname, filename)
		is_supported_image = filename.lower().endswith(self._IMAGE_EXTENSIONS)
		is_available = "://" in dirname or os.path.isfile(full_path)
		if not is_supported_image or not is_available:
			self._selected_image_path = None
			self._selected_image_label.text = "Select a valid image file"
			self._image_preview.visible = False
			self._update_generate_button()
			self._file_picker.hide()
			return
		self._selected_image_path = full_path
		self._selected_image_label.text = full_path
		self._image_preview.source_url = full_path.replace("\\", "/")
		self._image_preview.visible = True
		self._update_generate_button()
		self._file_picker.hide()

	def _start_generation(self):
		if (self._generation_task and not self._generation_task.done()) or self._timeline.is_playing():
			return
		self._generate_button.enabled = False
		self._progress_bar.model.set_value(0.0)
		self._progress_bar.visible = True
		self._selected_image_label.text = "Preparing generation..."
		self._generation_task = asyncio.ensure_future(self._generate_blocks())

	async def _generate_blocks(self):
		if not self._selected_image_path:
			return
		stage = None
		root_path = None
		try:
			from PIL import Image
			import omni.kit.app
			import omni.usd
			from pxr import Gf, Sdf, UsdGeom, UsdPhysics

			kit_app = omni.kit.app.get_app()
			self._selected_image_label.text = "Clearing previous image..."
			await kit_app.next_update_async()
			stage = omni.usd.get_context().get_stage()
			if stage is None:
				raise RuntimeError("No USD stage is open")
			if not stage.GetPrimAtPath("/World").IsValid():
				UsdGeom.Xform.Define(stage, "/World")
			root_path = "/World/DominoesImage"
			if self._generated_root_path and self._generated_root_path != root_path and stage.GetPrimAtPath(self._generated_root_path).IsValid():
				stage.RemovePrim(Sdf.Path(self._generated_root_path))
			if stage.GetPrimAtPath(root_path).IsValid():
				stage.RemovePrim(Sdf.Path(root_path))
			self._generated_root_path = None

			image_source = self._selected_image_path
			if "://" in image_source:
				import omni.client

				result, image_data = omni.client.read_file(image_source)
				if result != omni.client.Result.OK:
					raise RuntimeError(f"Could not read image: {result}")
				image_source = io.BytesIO(image_data)
			with Image.open(image_source) as source_image:
				image = source_image.convert("RGB")
			resampling = getattr(Image, "Resampling", Image).NEAREST
			max_block_count = max(1, self._max_block_count_model.get_value_as_int())
			if image.width * image.height > max_block_count:
				resize_scale = math.sqrt(max_block_count / (image.width * image.height))
				resized_width = max(1, int(image.width * resize_scale))
				resized_height = max(1, int(image.height * resize_scale))
				while resized_width * resized_height > max_block_count:
					if resized_width / image.width >= resized_height / image.height and resized_width > 1:
						resized_width -= 1
					else:
						resized_height -= 1
				image = image.resize((resized_width, resized_height), resampling)

			UsdGeom.Xform.Define(stage, root_path)
			meters_per_unit = UsdGeom.GetStageMetersPerUnit(stage)
			if meters_per_unit <= 0:
				raise RuntimeError("Stage meters-per-unit must be greater than zero")
			block_scale = Gf.Vec3f(
				self._BLOCK_LENGTH_M / meters_per_unit,
				self._BLOCK_WIDTH_M / meters_per_unit,
				self._BLOCK_HEIGHT_M / meters_per_unit,
			)
			center_spacing = self._CENTER_SPACING_M / meters_per_unit
			if not any(prim.IsA(UsdPhysics.Scene) for prim in stage.Traverse()):
				physics_scene = UsdPhysics.Scene.Define(stage, "/World/PhysicsScene")
				physics_scene.CreateGravityDirectionAttr(Gf.Vec3f(0.0, 0.0, -1.0))
				physics_scene.CreateGravityMagnitudeAttr(9.81 / meters_per_unit)

			ground = UsdGeom.Cube.Define(stage, f"{root_path}/Ground")
			ground.CreateSizeAttr(1.0)
			ground_scale = Gf.Vec3f(image.width * center_spacing, image.height * center_spacing, 0.05 / meters_per_unit)
			ground_xform = UsdGeom.XformCommonAPI(ground.GetPrim())
			ground_xform.SetTranslate(Gf.Vec3d(0.0, 0.0, -ground_scale[2] / 2))
			ground_xform.SetScale(ground_scale)
			UsdPhysics.CollisionAPI.Apply(ground.GetPrim())

			total_blocks = image.width * image.height
			processed_blocks = 0
			for row in range(image.height):
				for column in range(image.width):
					red, green, blue = image.getpixel((column, row))
					block_path = f"{root_path}/Block_{row:03d}_{column:03d}"
					block = UsdGeom.Cube.Define(stage, block_path)
					block.CreateSizeAttr(1.0)
					block_prim = block.GetPrim()
					UsdPhysics.CollisionAPI.Apply(block_prim)
					UsdPhysics.RigidBodyAPI.Apply(block_prim)
					UsdPhysics.MassAPI.Apply(block_prim).CreateDensityAttr(500.0)
					block.CreateDisplayColorAttr().Set([
						Gf.Vec3f(
							self._srgb_to_linear(red),
							self._srgb_to_linear(green),
							self._srgb_to_linear(blue),
						)
					])
					xform = UsdGeom.XformCommonAPI(block_prim)
					tilt_degrees = self._INITIAL_TILT_DEGREES if row == 0 and column == 0 else 0.0
					tilt_radians = math.radians(tilt_degrees)
					x_position = (column - (image.width - 1) / 2) * center_spacing
					y_position = ((image.height - 1) / 2 - row) * center_spacing
					if tilt_degrees:
						pivot_offset = block_scale[2] / 2 * math.sin(tilt_radians)
						yaw_radians = math.radians(self._YAW_DEGREES)
						x_position += pivot_offset * math.sin(yaw_radians)
						y_position -= pivot_offset * math.cos(yaw_radians)
					z_position = block_scale[2] / 2 * math.cos(tilt_radians)
					xform.SetTranslate(Gf.Vec3d(x_position, y_position, z_position))
					xform.SetRotate(Gf.Vec3f(tilt_degrees, 0.0, self._YAW_DEGREES))
					xform.SetScale(block_scale)
					processed_blocks += 1
					if processed_blocks % 8 == 0 or processed_blocks == total_blocks:
						self._progress_bar.model.set_value(processed_blocks / total_blocks)
						self._selected_image_label.text = f"Generating blocks: {processed_blocks}/{total_blocks}"
						await kit_app.next_update_async()

			self._generated_root_path = root_path
			self._selected_image_label.text = f"Generated {image.width} x {image.height} blocks"
			self._progress_bar.model.set_value(1.0)
		except asyncio.CancelledError:
			if stage is not None and root_path and stage.GetPrimAtPath(root_path).IsValid():
				stage.RemovePrim(Sdf.Path(root_path))
			self._generated_root_path = None
			raise
		except Exception as error:
			if stage is not None and root_path and stage.GetPrimAtPath(root_path).IsValid():
				stage.RemovePrim(Sdf.Path(root_path))
			self._generated_root_path = None
			self._selected_image_label.text = f"Generation failed: {error}"
			self._progress_bar.visible = False
		finally:
			self._generation_task = None
			self._update_generate_button()

	def _srgb_to_linear(self, channel):
		value = channel / 255.0
		if value <= 0.04045:
			return value / 12.92
		return ((value + 0.055) / 1.055) ** 2.4
