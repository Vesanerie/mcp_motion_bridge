bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 3, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Multi-view video mocap: MediaPipe extracts poses, Claude rigs and animates via BlenderMCP.",
    "category": "Animation",
}

import json
import os
import subprocess
import sys

import bpy
from bpy.props import (
    BoolProperty, EnumProperty, FloatProperty,
    IntProperty, StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Vector


CAMERA_SPECS = (
    ("front", Vector((0.0, -1.0, 0.0))),
    ("back", Vector((0.0, 1.0, 0.0))),
    ("left", Vector((-1.0, 0.0, 0.0))),
    ("right", Vector((1.0, 0.0, 0.0))),
    ("top", Vector((0.0, 0.0, 1.0))),
    ("bottom", Vector((0.0, 0.0, -1.0))),
)


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------
class VMMCP_Props(PropertyGroup):
    mesh_object: StringProperty(
        name="Mesh",
        description="Mesh object to rig and animate",
        default="",
    )
    # Videos per view
    front_video: StringProperty(name="Front", subtype="FILE_PATH", default="")
    back_video: StringProperty(name="Back", subtype="FILE_PATH", default="")
    left_video: StringProperty(name="Left", subtype="FILE_PATH", default="")
    right_video: StringProperty(name="Right", subtype="FILE_PATH", default="")
    top_video: StringProperty(name="Top", subtype="FILE_PATH", default="")
    bottom_video: StringProperty(name="Bottom", subtype="FILE_PATH", default="")

    # MediaPipe settings
    python_exe: StringProperty(
        name="External Python",
        description="Python with mediapipe installed",
        subtype="FILE_PATH",
        default="",
    )
    model_complexity: IntProperty(
        name="Model complexity",
        description="MediaPipe model complexity (0 fast, 1 balanced, 2 accurate)",
        default=1, min=0, max=2,
    )
    min_detection_conf: FloatProperty(
        name="Min detection conf",
        default=0.5, min=0.0, max=1.0,
    )
    smooth_landmarks: BoolProperty(
        name="Smooth landmarks",
        default=True,
    )

    # Scene settings
    frame_start: IntProperty(name="Start", default=1, min=1)
    frame_end: IntProperty(name="End", default=250, min=1)
    camera_distance: FloatProperty(
        name="Camera Distance",
        description="Multiplier based on the mesh bounding box size",
        default=2.4, min=0.5, max=20.0,
    )
    create_camera_setup: BoolProperty(
        name="Create/Update 6 Cameras",
        default=True,
    )

    # Internal state
    landmarks_path: StringProperty(
        name="Landmarks JSON",
        description="Path to the extracted landmarks JSON",
        subtype="FILE_PATH",
        default="",
    )
    request_text_name: StringProperty(
        name="Last Request",
        default="",
    )
    extraction_status: StringProperty(
        name="Status",
        default="",
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _abspath(path):
    return bpy.path.abspath(path) if path else ""


def _target_mesh(context, props):
    obj = bpy.data.objects.get(props.mesh_object) if props.mesh_object else None
    if obj is None and context.object and context.object.type == "MESH":
        obj = context.object
        props.mesh_object = obj.name
    if obj is None or obj.type != "MESH":
        return None
    return obj


def _world_bbox(obj):
    points = [obj.matrix_world @ Vector(corner) for corner in obj.bound_box]
    center = sum(points, Vector()) / len(points)
    min_v = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    max_v = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    size = max_v - min_v
    radius = max(size.length * 0.5, 0.5)
    return center, min_v, max_v, size, radius


def _look_at(obj, target):
    direction = target - obj.location
    if direction.length < 1e-6:
        return
    obj.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()


def _setup_cameras(context, mesh, props):
    center, _min_v, _max_v, _size, radius = _world_bbox(mesh)
    distance = radius * props.camera_distance
    cameras = {}
    for view_name, axis in CAMERA_SPECS:
        cam_name = f"VMMCP_{view_name.upper()}_Camera"
        cam_obj = bpy.data.objects.get(cam_name)
        if cam_obj is None:
            cam_data = bpy.data.cameras.new(cam_name)
            cam_obj = bpy.data.objects.new(cam_name, cam_data)
            context.collection.objects.link(cam_obj)
        elif cam_obj.type != "CAMERA":
            continue
        cam_obj.location = center + axis.normalized() * distance
        _look_at(cam_obj, center)
        cam_obj.data.lens = 70
        cam_obj.data.clip_end = max(distance * 10.0, 1000.0)
        cameras[view_name] = cam_obj.name
    return cameras


def _mesh_summary(obj):
    mesh = obj.data
    center, min_v, max_v, size, _radius = _world_bbox(obj)
    modifiers = [{"name": m.name, "type": m.type} for m in obj.modifiers]
    materials = [slot.material.name for slot in obj.material_slots if slot.material]
    shape_keys = []
    if mesh.shape_keys:
        shape_keys = [key.name for key in mesh.shape_keys.key_blocks]
    return {
        "name": obj.name,
        "vertex_count": len(mesh.vertices),
        "polygon_count": len(mesh.polygons),
        "bbox_min": [round(v, 4) for v in min_v],
        "bbox_max": [round(v, 4) for v in max_v],
        "bbox_size": [round(v, 4) for v in size],
        "bbox_center": [round(v, 4) for v in center],
        "modifiers": modifiers,
        "materials": materials,
        "shape_keys": shape_keys,
    }


def _get_videos(props):
    """Return dict of view_name -> absolute path for filled-in videos."""
    mapping = {
        "front": _abspath(props.front_video),
        "back": _abspath(props.back_video),
        "left": _abspath(props.left_video),
        "right": _abspath(props.right_video),
        "top": _abspath(props.top_video),
        "bottom": _abspath(props.bottom_video),
    }
    return {k: v for k, v in mapping.items() if v and os.path.isfile(v)}


def _auto_detect_python():
    """Try to find a python with mediapipe installed."""
    env = os.environ.get("VIDEO_MOCAP_PYTHON")
    if env and os.path.isfile(env):
        return env
    candidates = ["python3", "python"] if sys.platform != "win32" else ["python", "python3"]
    for c in candidates:
        try:
            r = subprocess.run(
                [c, "-c", "import mediapipe; print('ok')"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return ""


def _write_text(name, content):
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(content)
    return text


# ------------------------------------------------------------------
# Operator: Extract landmarks via MediaPipe (subprocess)
# ------------------------------------------------------------------
class VMMCP_OT_extract(Operator):
    """Run MediaPipe on the videos and extract landmarks (multi-view or single)."""
    bl_idname = "video_mocap.extract"
    bl_label = "Extract Landmarks"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        videos = _get_videos(props)

        if not videos:
            self.report({"ERROR"}, "Add at least one video.")
            return {"CANCELLED"}

        # Find Python
        py = _abspath(props.python_exe) if props.python_exe else _auto_detect_python()
        if not py:
            self.report({"ERROR"}, "No Python with mediapipe found. Set External Python path.")
            return {"CANCELLED"}

        # Output path
        blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else "/tmp"
        out_json = os.path.join(blend_dir, "vmmcp_landmarks.json")

        script = os.path.join(os.path.dirname(__file__), "extractor", "extract_pose.py")

        # Build command
        cmd = [py, script, "--out", out_json,
               "--complexity", str(props.model_complexity),
               "--min-detection", str(props.min_detection_conf)]
        if props.smooth_landmarks:
            cmd.append("--smooth")

        if len(videos) >= 2:
            cmd.append("--views")
            for vname, vpath in videos.items():
                cmd.append(f"{vname}={vpath}")
        else:
            vpath = list(videos.values())[0]
            cmd.extend(["--video", vpath])

        print("[video_mocap] running:", " ".join(cmd))
        props.extraction_status = "Extracting..."

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 30)
        except Exception as e:
            props.extraction_status = f"Failed: {e}"
            self.report({"ERROR"}, f"Extraction failed: {e}")
            return {"CANCELLED"}

        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            props.extraction_status = "Failed (check console)"
            self.report({"ERROR"}, f"Extraction failed (code {proc.returncode}). Check console.")
            return {"CANCELLED"}

        props.landmarks_path = out_json
        props.extraction_status = "Done"
        self.report({"INFO"}, f"Landmarks extracted: {out_json}")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Rig Mesh (generate MCP request with landmark data)
# ------------------------------------------------------------------
class VMMCP_OT_rig_mesh(Operator):
    """Generate MCP request for Claude to rig the mesh, informed by landmark data."""
    bl_idname = "video_mocap.rig_mesh"
    bl_label = "Rig Mesh (MCP)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props) if props.create_camera_setup else {}

        # Load landmarks if available (helps Claude understand the anatomy)
        landmark_summary = None
        lm_path = _abspath(props.landmarks_path)
        if lm_path and os.path.isfile(lm_path):
            try:
                with open(lm_path, "r") as f:
                    lm_data = json.load(f)
                # Include just the first frame as reference for bone placement
                first_frame = lm_data.get("frames", [{}])[0] if lm_data.get("frames") else {}
                landmark_summary = {
                    "mode": lm_data.get("mode"),
                    "landmark_names": lm_data.get("landmark_names"),
                    "frame_count": lm_data.get("frame_count"),
                    "fps": lm_data.get("fps"),
                    "reference_frame": first_frame,
                }
            except Exception:
                pass

        payload = {
            "addon": "video_mocap_mcp",
            "task": "rig_mesh",
            "mesh": _mesh_summary(mesh),
            "camera_setup": cameras,
            "landmark_data": landmark_summary,
            "instructions": (
                "Use BlenderMCP to inspect the mesh and create an armature that "
                "matches the mesh anatomy. Use the landmark reference frame to "
                "understand where joints should be placed (landmarks give real "
                "human proportions). Place bones inside the mesh, parent the mesh "
                "to the armature with automatic weights, and create usable FK/IK "
                "controls. The landmark names follow MediaPipe Pose convention."
            ),
        }

        content = (
            "MCP request for Claude / BlenderMCP\n"
            "===================================\n\n"
            f"{json.dumps(payload, indent=2)}\n"
        )
        text = _write_text("VMMCP_Rig_Request", content)
        props.request_text_name = text.name
        context.window_manager.clipboard = content
        self.report({"INFO"}, "Rig request generated and copied to clipboard.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Animate (generate MCP request with full landmark animation)
# ------------------------------------------------------------------
class VMMCP_OT_animate(Operator):
    """Generate MCP request for Claude to animate the rig using extracted landmarks."""
    bl_idname = "video_mocap.animate"
    bl_label = "Animate (MCP)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}

        lm_path = _abspath(props.landmarks_path)
        if not lm_path or not os.path.isfile(lm_path):
            self.report({"ERROR"}, "No landmarks JSON. Run 'Extract Landmarks' first.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props) if props.create_camera_setup else {}

        # Load full landmark data
        with open(lm_path, "r") as f:
            lm_data = json.load(f)

        payload = {
            "addon": "video_mocap_mcp",
            "task": "animate",
            "mesh": _mesh_summary(mesh),
            "camera_setup": cameras,
            "frame_range": {
                "start": props.frame_start,
                "end": min(props.frame_end, lm_data.get("frame_count", props.frame_end)),
                "fps": lm_data.get("fps", 30.0),
            },
            "landmarks_file": lm_path,
            "landmark_summary": {
                "mode": lm_data.get("mode"),
                "frame_count": lm_data.get("frame_count"),
                "fps": lm_data.get("fps"),
                "landmark_names": lm_data.get("landmark_names"),
                "landmark_format": lm_data.get("landmark_format"),
            },
            "instructions": (
                "Use BlenderMCP to animate the existing rig on the mesh. "
                "The landmarks JSON file contains precise 3D positions of "
                "33 body landmarks for every frame, extracted via MediaPipe "
                "from the reference videos. Read this file, then for each "
                "frame, compute the bone rotations that make the rig match "
                "the landmark positions. Keyframe every bone every frame. "
                "The landmark coordinate system is: X right, Y up, Z forward "
                "(MediaPipe world coordinates in meters). Convert to Blender "
                "coordinates (Z up, -Y forward) before applying."
            ),
        }

        content = (
            "MCP request for Claude / BlenderMCP\n"
            "===================================\n\n"
            f"{json.dumps(payload, indent=2)}\n"
        )
        text = _write_text("VMMCP_Animate_Request", content)
        props.request_text_name = text.name
        context.window_manager.clipboard = content
        self.report({"INFO"}, "Animate request generated and copied to clipboard.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Run full pipeline
# ------------------------------------------------------------------
class VMMCP_OT_run_all(Operator):
    """Extract landmarks, then generate rig + animate requests."""
    bl_idname = "video_mocap.run_all"
    bl_label = "Run Full Pipeline"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if bpy.ops.video_mocap.extract() != {"FINISHED"}:
            return {"CANCELLED"}
        if bpy.ops.video_mocap.rig_mesh() != {"FINISHED"}:
            return {"CANCELLED"}
        self.report({"INFO"}, "Pipeline done. Rig request in clipboard. After rigging, run Animate.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Setup cameras only
# ------------------------------------------------------------------
class VMMCP_OT_setup_cameras(Operator):
    bl_idname = "video_mocap.setup_cameras"
    bl_label = "Setup Cameras"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}
        cameras = _setup_cameras(context, mesh, props)
        self.report({"INFO"}, f"Camera setup ready: {len(cameras)} cameras.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Panel
# ------------------------------------------------------------------
class VMMCP_PT_panel(Panel):
    bl_label = "Video Mocap MCP"
    bl_idname = "VMMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Mocap"

    def draw(self, context):
        layout = self.layout
        props = context.scene.vmmcp

        # Mesh
        box = layout.box()
        box.label(text="Mesh", icon="MESH_DATA")
        box.prop_search(props, "mesh_object", bpy.data, "objects")

        # Videos
        box = layout.box()
        box.label(text="Reference Videos", icon="FILE_MOVIE")
        box.prop(props, "front_video")
        box.prop(props, "back_video")
        box.prop(props, "left_video")
        box.prop(props, "right_video")
        box.prop(props, "top_video")
        box.prop(props, "bottom_video")

        # MediaPipe
        box = layout.box()
        box.label(text="MediaPipe", icon="ARMATURE_DATA")
        box.prop(props, "python_exe")
        box.prop(props, "model_complexity")
        box.prop(props, "min_detection_conf")
        box.prop(props, "smooth_landmarks")

        # Cameras
        box = layout.box()
        box.label(text="Scene Setup", icon="CAMERA_DATA")
        row = box.row(align=True)
        row.prop(props, "frame_start")
        row.prop(props, "frame_end")
        box.prop(props, "create_camera_setup")
        box.prop(props, "camera_distance")
        box.operator("video_mocap.setup_cameras", icon="CAMERA_DATA")

        layout.separator()

        # Pipeline buttons
        col = layout.column(align=True)
        col.operator("video_mocap.extract", icon="PLAY")
        col.operator("video_mocap.rig_mesh", icon="ARMATURE_DATA")
        col.operator("video_mocap.animate", icon="ANIM")
        layout.separator()
        layout.operator("video_mocap.run_all", icon="SEQ_SEQUENCER")

        # Status
        if props.extraction_status:
            layout.label(text=f"Extraction: {props.extraction_status}")
        if props.landmarks_path:
            layout.label(text=f"JSON: {os.path.basename(props.landmarks_path)}", icon="FILE")
        if props.request_text_name:
            layout.label(text=f"Request: {props.request_text_name}", icon="TEXT")


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------
classes = (
    VMMCP_Props,
    VMMCP_OT_extract,
    VMMCP_OT_rig_mesh,
    VMMCP_OT_animate,
    VMMCP_OT_run_all,
    VMMCP_OT_setup_cameras,
    VMMCP_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vmmcp = bpy.props.PointerProperty(type=VMMCP_Props)


def unregister():
    if hasattr(bpy.types.Scene, "vmmcp"):
        del bpy.types.Scene.vmmcp
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
