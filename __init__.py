bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 6, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Prepare motion capture context for Claude Code via BlenderMCP.",
    "category": "Animation",
}

import json
import os

import bpy
from bpy.props import FloatProperty, IntProperty, StringProperty
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
# Properties — only what the user needs to fill
# ------------------------------------------------------------------
class VMMCP_Props(PropertyGroup):
    mesh_object: StringProperty(
        name="Mesh",
        description="Mesh object to rig and animate (auto-detected if selected)",
        default="",
    )
    front_video: StringProperty(name="Front", subtype="FILE_PATH", default="")
    back_video: StringProperty(name="Back", subtype="FILE_PATH", default="")
    left_video: StringProperty(name="Left", subtype="FILE_PATH", default="")
    right_video: StringProperty(name="Right", subtype="FILE_PATH", default="")
    top_video: StringProperty(name="Top", subtype="FILE_PATH", default="")
    bottom_video: StringProperty(name="Bottom", subtype="FILE_PATH", default="")
    camera_distance: FloatProperty(
        name="Camera Distance",
        default=2.4, min=0.5, max=20.0,
    )
    frame_start: IntProperty(name="Start", default=1, min=1)
    frame_end: IntProperty(name="End", default=250, min=1)


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
        "world_location": list(obj.location),
        "world_scale": list(obj.scale),
        "modifiers": modifiers,
        "materials": materials,
        "shape_keys": shape_keys,
    }


def _video_sources(props):
    sources = {
        "front": _abspath(props.front_video),
        "back": _abspath(props.back_video),
        "left": _abspath(props.left_video),
        "right": _abspath(props.right_video),
        "top": _abspath(props.top_video),
        "bottom": _abspath(props.bottom_video),
    }
    return {k: v for k, v in sources.items() if v and os.path.isfile(v)}


def _write_text(name, content):
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(content)
    return text


def _export_txt(content, blend_filepath):
    base_dir = os.path.dirname(blend_filepath) if blend_filepath else bpy.app.tempdir
    out_path = os.path.join(base_dir, "VMMCP_Prompt.txt")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)
    return out_path


# ------------------------------------------------------------------
# The one prompt that tells Claude Code everything
# ------------------------------------------------------------------
def _build_prompt(context, mesh, cameras, videos, props):
    payload = {
        "addon": "video_mocap_mcp",
        "version": "0.5.0",
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "mesh": _mesh_summary(mesh),
        "camera_setup": cameras,
        "reference_videos": videos,
        "frame_range": {
            "start": props.frame_start,
            "end": props.frame_end,
            "fps": context.scene.render.fps / context.scene.render.fps_base,
        },
    }

    return (
        "PROMPT FOR CLAUDE CODE (connected to Blender via BlenderMCP)\n"
        "=============================================================\n\n"

        "You have full access to Blender through BlenderMCP. You must do ALL the\n"
        "work yourself: motion extraction, rigging, animation, verification.\n"
        "The user will not intervene — execute everything autonomously.\n\n"

        "CONTEXT:\n"
        "The payload below describes a Blender scene with a mesh and reference\n"
        "videos showing a person performing a movement filmed from different\n"
        "angles. Your job is to reproduce that movement on the mesh.\n\n"

        "FULL PIPELINE TO EXECUTE:\n\n"

        "STEP 1 — MOTION EXTRACTION:\n"
        "Run 4D-Humans (HMR2.0) on the reference videos to extract SMPL params.\n"
        "This is the recommended estimator for macOS Apple Silicon (MPS backend).\n"
        "The estimator script is at: estimator/run_4dhumans.py in the addon folder.\n"
        "Run it in a separate Python env (~/hmr2_env) via subprocess:\n"
        "  PYTORCH_ENABLE_MPS_FALLBACK=1 ~/hmr2_env/bin/python run_4dhumans.py \\\n"
        "    --video <path> --out <path>.npz\n"
        "Output: per-frame SMPL parameters (72 axis-angle rotations for 24 joints,\n"
        "10 shape params, root translation) saved as .npz.\n"
        "If multiple videos are provided, use the one with best visibility for\n"
        "extraction, and use the others for cross-view verification later.\n"
        "NOTE: HMR2 outputs camera-relative poses (no global trajectory).\n"
        "The character will animate in place. For global movement, reconstruct\n"
        "from 2D hip positions in the video.\n\n"

        "STEP 2 — RIGGING:\n"
        "Inspect the mesh from the 6 analysis cameras (already placed in the scene).\n"
        "Create an armature with 24 bones matching the SMPL joint names:\n"
        "  pelvis, left_hip, right_hip, spine1, left_knee, right_knee, spine2,\n"
        "  left_ankle, right_ankle, spine3, left_foot, right_foot, neck,\n"
        "  left_collar, right_collar, head, left_shoulder, right_shoulder,\n"
        "  left_elbow, right_elbow, left_wrist, right_wrist, left_hand, right_hand\n"
        "Place bones inside the mesh at anatomically correct positions.\n"
        "Parent the mesh to the armature with automatic weights.\n"
        "Add IK constraints on ankles and wrists for post-processing.\n"
        "Verify bone placement from each of the 6 camera angles.\n\n"

        "STEP 3 — ANIMATION TRANSFER:\n"
        "Read the .npz motion data. For each frame, for each joint:\n"
        "  a) Extract axis-angle (3 values) from smpl_poses[frame, joint*3:joint*3+3]\n"
        "  b) Convert to quaternion using scipy.spatial.transform.Rotation\n"
        "     NEVER do manual axis-angle math.\n"
        "  c) Coordinate conversion: SMPL is Y-up, Blender is Z-up.\n"
        "     Swap: (x, y, z) → (x, z, -y) for both rotations and translations.\n"
        "  d) Rest pose offset: SMPL assumes T-pose. If the rig rest pose differs,\n"
        "     compose: final_rot = rest_offset_inv × smpl_rot × rest_offset\n"
        "     Without this, shoulders and arms WILL be dislocated.\n"
        "  e) Set bone.rotation_quaternion + keyframe_insert\n"
        "  f) For pelvis: also set bone.location from smpl_trans + keyframe\n\n"

        "STEP 4 — TEMPORAL SMOOTHING:\n"
        "Smooth ALL rotation curves using quaternion SLERP or log-quaternion filter.\n"
        "NEVER smooth Euler angles (gimbal lock, discontinuities).\n"
        "Apply a 3-frame moving average if jitter remains.\n\n"

        "STEP 5 — FOOT CONTACT CORRECTION:\n"
        "Foot skating is the worst visual artifact. Fix it:\n"
        "  - Detect frames where feet should be planted (low velocity + low height)\n"
        "  - Activate IK constraints on ankle bones for those frame ranges\n"
        "  - Pin foot position to the ground plane during contact\n\n"

        "STEP 6 — MULTI-ANGLE VERIFICATION:\n"
        "This is the quality gate. After animation is applied:\n"
        "  - Inspect the animated mesh from EACH of the 6 cameras\n"
        "  - Compare against the reference videos for that angle (if provided)\n"
        "  - Check for: limbs penetrating mesh, impossible joint angles,\n"
        "    asymmetric motion that should be symmetric, floating feet\n"
        "  - If a contradiction is found between views, adjust those frames\n"
        "  - Do this BEFORE declaring the work done\n\n"

        "HARD CONSTRAINTS:\n"
        "  - Bone lengths MUST remain constant across all frames\n"
        "  - Do NOT invent motion for body parts not visible in any video\n"
        "  - Keep mesh parented to the same armature throughout\n"
        "  - Set scene FPS to match motion data FPS before keyframing\n"
        "  - Ignore objects/people/props not present in the reference videos\n\n"

        "PAYLOAD:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


# ------------------------------------------------------------------
# Single operator: Generate Prompt
# ------------------------------------------------------------------
class VMMCP_OT_generate(Operator):
    """Generate the full prompt for Claude Code."""
    bl_idname = "video_mocap.generate"
    bl_label = "Generate Prompt"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select a mesh object first.")
            return {"CANCELLED"}

        videos = _video_sources(props)
        if not videos:
            self.report({"ERROR"}, "Add at least one reference video.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props)
        prompt = _build_prompt(context, mesh, cameras, videos, props)

        # Save everywhere
        _write_text("VMMCP_Prompt", prompt)
        context.window_manager.clipboard = prompt
        txt_path = _export_txt(prompt, bpy.data.filepath)

        self.report({"INFO"}, f"Prompt copied to clipboard and saved to {txt_path}")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Panel — minimal UI
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

        # Mesh (auto-detect from selection)
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

        # Settings
        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        box.prop(props, "camera_distance")
        row = box.row(align=True)
        row.prop(props, "frame_start")
        row.prop(props, "frame_end")

        layout.separator()

        # The one button
        layout.operator("video_mocap.generate", icon="COPYDOWN", text="Generate Prompt")
        layout.label(text="Then paste in Claude Code + BlenderMCP", icon="INFO")

        # Estimator info
        layout.separator()
        info = layout.box()
        info.label(text="Estimator: 4D-Humans (local, macOS compatible)", icon="ARMATURE_DATA")
        info.label(text="For cinematic camera: TRAM cloud (coming soon)")
        info.label(text="Legacy MediaPipe fallback for quick tests")


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------
classes = (
    VMMCP_Props,
    VMMCP_OT_generate,
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
