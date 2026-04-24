bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 4, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "SMPL-based video mocap: TRAM/HMR2 extracts motion, Claude rigs and animates via BlenderMCP.",
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

RIG_PRESET_GUIDE = {
    "CUSTOM": {
        "label": "Custom",
        "default_bones": 65,
        "guidance": "Use the requested bone count as a creative/technical target for this mesh.",
    },
    "RIGIFY": {
        "label": "Rigify",
        "default_bones": 90,
        "guidance": (
            "Create a Blender-friendly deformation rig with control bones. "
            "Prefer clear FK/IK controls and keep deformation bones anatomically consistent."
        ),
    },
    "UNREAL": {
        "label": "Unreal",
        "default_bones": 55,
        "guidance": (
            "Favor an engine-friendly skeleton close to common Unreal humanoid structure. "
            "Keep naming, hierarchy and root/pelvis separation suitable for export."
        ),
    },
    "SMPL_24": {
        "label": "SMPL 24-joint",
        "default_bones": 24,
        "guidance": (
            "Create an armature with exactly the 24 SMPL joints for direct "
            "SMPL rotation transfer. Bone names must match SMPL joint names."
        ),
    },
}


def _update_rig_preset(self, _context):
    preset = RIG_PRESET_GUIDE.get(self.rig_preset)
    if preset:
        self.requested_bone_count = preset["default_bones"]


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------
class VMMCP_Props(PropertyGroup):
    mesh_object: StringProperty(
        name="Mesh",
        description="Mesh object to rig and animate",
        default="",
    )
    # Video source (single video for SMPL estimation)
    video_path: StringProperty(
        name="Video",
        description="Reference video for motion extraction",
        subtype="FILE_PATH",
        default="",
    )
    # Multi-angle reference videos (for Claude visual verification)
    front_video: StringProperty(name="Front", subtype="FILE_PATH", default="")
    back_video: StringProperty(name="Back", subtype="FILE_PATH", default="")
    left_video: StringProperty(name="Left", subtype="FILE_PATH", default="")
    right_video: StringProperty(name="Right", subtype="FILE_PATH", default="")
    top_video: StringProperty(name="Top", subtype="FILE_PATH", default="")
    bottom_video: StringProperty(name="Bottom", subtype="FILE_PATH", default="")
    image_sequence_dir: StringProperty(
        name="Image Sequence",
        description="Optional folder containing an image sequence",
        subtype="DIR_PATH",
        default="",
    )

    # Estimator settings
    estimation_method: EnumProperty(
        name="Method",
        items=[
            ("SMPL", "SMPL (TRAM/HMR2)", "High quality: joint rotations via TRAM or 4D-Humans"),
            ("MEDIAPIPE", "MediaPipe (fallback)", "Degraded: 33 landmarks, noisy depth"),
        ],
        default="SMPL",
    )
    python_exe: StringProperty(
        name="External Python",
        description="Python with TRAM/HMR2 or mediapipe installed",
        subtype="FILE_PATH",
        default="",
    )
    smpl_method: EnumProperty(
        name="SMPL Estimator",
        items=[
            ("auto", "Auto-detect", "Use best available (TRAM > HMR2)"),
            ("tram", "TRAM", "Best quality, SLAM/DPVO for camera tracking"),
            ("hmr2", "4D-Humans (HMR2)", "Lighter, good for simpler videos"),
        ],
        default="auto",
    )

    # Rig settings
    rig_preset: EnumProperty(
        name="Rig Target",
        items=[
            ("SMPL_24", "SMPL 24-joint", "Direct SMPL rotation transfer (recommended with SMPL method)"),
            ("CUSTOM", "Custom", "General-purpose rig driven by the requested bone count"),
            ("RIGIFY", "Rigify", "Blender/Rigify-style rig with animator controls"),
            ("UNREAL", "Unreal", "Game-engine-friendly humanoid skeleton"),
        ],
        default="SMPL_24",
        update=_update_rig_preset,
    )
    requested_bone_count: IntProperty(
        name="Bones",
        description="Target number of bones for the armature",
        default=24, min=8, max=512,
    )

    # Scene settings
    frame_start: IntProperty(name="Start", default=1, min=1)
    frame_end: IntProperty(name="End", default=250, min=1)
    camera_distance: FloatProperty(
        name="Camera Distance",
        default=2.4, min=0.5, max=20.0,
    )
    create_camera_setup: BoolProperty(
        name="Create/Update 6 Cameras",
        default=True,
    )

    # Internal state
    motion_data_path: StringProperty(
        name="Motion Data",
        subtype="FILE_PATH",
        default="",
    )
    request_text_name: StringProperty(default="")
    request_txt_path: StringProperty(
        name="Request TXT",
        subtype="FILE_PATH",
        default="",
    )
    extraction_status: StringProperty(default="")


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
    modifiers = [{"name": m.name, "type": m.type, "show_viewport": m.show_viewport} for m in obj.modifiers]
    materials = [slot.material.name for slot in obj.material_slots if slot.material]
    shape_keys = []
    if mesh.shape_keys:
        shape_keys = [key.name for key in mesh.shape_keys.key_blocks]
    return {
        "name": obj.name,
        "data_name": mesh.name,
        "vertex_count": len(mesh.vertices),
        "edge_count": len(mesh.edges),
        "polygon_count": len(mesh.polygons),
        "world_location": list(obj.location),
        "world_rotation_euler": list(obj.rotation_euler),
        "world_scale": list(obj.scale),
        "bbox_min": list(min_v),
        "bbox_max": list(max_v),
        "bbox_size": list(size),
        "bbox_center": list(center),
        "modifiers": modifiers,
        "materials": materials,
        "shape_keys": shape_keys,
    }


def _media_sources(props):
    sources = {
        "front": _abspath(props.front_video),
        "back": _abspath(props.back_video),
        "left": _abspath(props.left_video),
        "right": _abspath(props.right_video),
        "top": _abspath(props.top_video),
        "bottom": _abspath(props.bottom_video),
        "image_sequence_dir": _abspath(props.image_sequence_dir),
    }
    return {key: value for key, value in sources.items() if value}


def _rig_settings(props):
    preset = RIG_PRESET_GUIDE.get(props.rig_preset, RIG_PRESET_GUIDE["CUSTOM"])
    return {
        "target": props.rig_preset,
        "target_label": preset["label"],
        "requested_bone_count": props.requested_bone_count,
        "guidance": preset["guidance"],
    }


def _write_text(name, content):
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(content)
    return text


def _read_text_block(name):
    text = bpy.data.texts.get(name) if name else None
    if text is None:
        return ""
    return text.as_string()


def _default_request_txt_path(props):
    if props.request_txt_path:
        return bpy.path.abspath(props.request_txt_path)
    base_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else bpy.app.tempdir
    filename = props.request_text_name or "VMMCP_Request"
    return os.path.join(base_dir, f"{filename}.txt")


def _export_request_to_txt(context, props):
    content = _read_text_block(props.request_text_name)
    if not content:
        return ""
    out_path = _default_request_txt_path(props)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write(content)
    props.request_txt_path = out_path
    context.window_manager.clipboard = content
    return out_path


def _auto_detect_python():
    env = os.environ.get("VIDEO_MOCAP_PYTHON")
    if env and os.path.isfile(env):
        return env
    candidates = ["python3", "python"] if sys.platform != "win32" else ["python", "python3"]
    for c in candidates:
        try:
            r = subprocess.run(
                [c, "-c", "import torch; print('ok')"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return ""


# ------------------------------------------------------------------
# SMPL joint reference (included in MCP requests)
# ------------------------------------------------------------------
SMPL_JOINT_NAMES = [
    "pelvis", "left_hip", "right_hip", "spine1",
    "left_knee", "right_knee", "spine2",
    "left_ankle", "right_ankle", "spine3",
    "left_foot", "right_foot", "neck",
    "left_collar", "right_collar", "head",
    "left_shoulder", "right_shoulder",
    "left_elbow", "right_elbow",
    "left_wrist", "right_wrist",
    "left_hand", "right_hand",
]


# ------------------------------------------------------------------
# Operator: Extract motion data
# ------------------------------------------------------------------
class VMMCP_OT_extract(Operator):
    """Run SMPL estimator (TRAM/HMR2) or MediaPipe on the video."""
    bl_idname = "video_mocap.extract"
    bl_label = "Extract Motion"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        video = _abspath(props.video_path)

        if not video or not os.path.isfile(video):
            self.report({"ERROR"}, f"Video not found: {video}")
            return {"CANCELLED"}

        py = _abspath(props.python_exe) if props.python_exe else _auto_detect_python()
        if not py:
            self.report({"ERROR"}, "No suitable Python found. Set External Python path.")
            return {"CANCELLED"}

        blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else "/tmp"

        if props.estimation_method == "SMPL":
            script = os.path.join(os.path.dirname(__file__), "estimator", "run_smpl.py")
            out_path = os.path.join(blend_dir, "vmmcp_motion.npz")
            cmd = [py, script,
                   "--video", video,
                   "--out", out_path,
                   "--method", props.smpl_method]
        else:
            script = os.path.join(os.path.dirname(__file__), "_fallback", "extractor", "extract_pose.py")
            out_path = os.path.join(blend_dir, "vmmcp_landmarks.json")
            cmd = [py, script,
                   "--video", video,
                   "--out", out_path,
                   "--smooth"]

        print("[video_mocap] running:", " ".join(cmd))
        props.extraction_status = "Extracting..."

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60)
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

        props.motion_data_path = out_path
        props.extraction_status = "Done"
        self.report({"INFO"}, f"Motion data extracted: {out_path}")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Rig Mesh
# ------------------------------------------------------------------
class VMMCP_OT_rig_mesh(Operator):
    """Generate MCP request for Claude to rig the mesh."""
    bl_idname = "video_mocap.rig_mesh"
    bl_label = "Rig Mesh"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props) if props.create_camera_setup else {}

        payload = {
            "addon": "video_mocap_mcp",
            "version": "0.4.0",
            "blend_file": bpy.data.filepath,
            "scene": context.scene.name,
            "mesh": _mesh_summary(mesh),
            "camera_setup": cameras,
            "rig_settings": _rig_settings(props),
            "media_sources": _media_sources(props),
        }

        if props.rig_preset == "SMPL_24":
            payload["smpl_joint_reference"] = {
                "joint_count": 24,
                "joint_names": SMPL_JOINT_NAMES,
                "note": "Bone names MUST match these 24 SMPL joints for animation transfer.",
            }

        task = (
            "Use BlenderMCP to inspect the mesh and the six scene cameras "
            "(top, bottom, front, back, left, right). Create an armature that "
            "matches the mesh anatomy/topology, place bones inside the mesh, "
            "honor the requested rig target and approximate bone count, "
            "parent/deform the mesh to that armature, create usable IK/FK "
            "controls where appropriate, and leave the rig ready for animation."
        )

        if props.rig_preset == "SMPL_24":
            task += (
                "\n\nSMPL-specific: The armature MUST have exactly 24 bones named "
                "after the SMPL joints listed in smpl_joint_reference. Follow the "
                "SMPL kinematic tree hierarchy. This is required for automated "
                "rotation transfer from SMPL motion data."
            )

        content = (
            "PROMPT TO PASTE IN A NEW CLAUDE CONVERSATION\n"
            "===========================================\n\n"
            "You are connected to Blender through BlenderMCP. If the BlenderMCP tools "
            "are not available in this conversation, stop and ask the user to reopen "
            "the conversation with BlenderMCP enabled.\n\n"
            f"Task: {task}\n\n"
            "Important constraints:\n"
            "- Follow the rig_settings payload for target platform and bone count.\n"
            "- Use the existing Blender scene as source of truth.\n"
            "- Use the camera objects listed in the payload as analysis views.\n"
            "- Verify bone placement from each of the 6 camera angles.\n\n"
            "Payload JSON:\n"
            f"{json.dumps(payload, indent=2)}\n"
        )

        text = _write_text("VMMCP_Rig_Mesh_Request", content)
        props.request_text_name = text.name
        props.request_txt_path = ""
        context.window_manager.clipboard = content
        _export_request_to_txt(context, props)
        self.report({"INFO"}, "Rig request generated, copied and exported.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Animate
# ------------------------------------------------------------------
class VMMCP_OT_animate(Operator):
    """Generate MCP request for Claude to animate the rig from extracted motion."""
    bl_idname = "video_mocap.animate"
    bl_label = "Animate"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}

        motion_path = _abspath(props.motion_data_path)
        is_smpl = motion_path and motion_path.endswith(".npz") and os.path.isfile(motion_path)

        media = _media_sources(props)
        if not is_smpl and not media:
            self.report({"ERROR"}, "No motion data and no reference videos. Run Extract or add videos.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props) if props.create_camera_setup else {}

        if is_smpl:
            content = self._build_smpl_request(context, props, mesh, cameras, motion_path, media)
        else:
            content = self._build_mediapipe_request(context, props, mesh, cameras, motion_path, media)

        text = _write_text("VMMCP_Animate_Request", content)
        props.request_text_name = text.name
        props.request_txt_path = ""
        context.window_manager.clipboard = content
        _export_request_to_txt(context, props)
        self.report({"INFO"}, "Animate request generated, copied and exported.")
        return {"FINISHED"}

    def _build_smpl_request(self, context, props, mesh, cameras, motion_path, media):
        payload = {
            "addon": "video_mocap_mcp",
            "version": "0.4.0",
            "task": "animate",
            "motion_source": "smpl",
            "blend_file": bpy.data.filepath,
            "scene": context.scene.name,
            "mesh": _mesh_summary(mesh),
            "camera_setup": cameras,
            "media_sources": media,
            "rig_settings": _rig_settings(props),
            "motion_data_file": motion_path,
            "frame_range": {
                "start": props.frame_start,
                "end": props.frame_end,
                "fps": context.scene.render.fps / context.scene.render.fps_base,
            },
            "smpl_data_format": {
                "file_type": ".npz (numpy compressed)",
                "fields": {
                    "smpl_poses": "(N, 72) axis-angle rotations for 24 joints, 3 values per joint",
                    "smpl_betas": "(N, 10) or (10,) body shape parameters",
                    "smpl_trans": "(N, 3) root translation per frame",
                    "fps": "scalar, video frame rate",
                    "frame_count": "scalar, number of frames",
                    "joint_names": "list of 24 SMPL joint names",
                    "parent_indices": "kinematic tree parent for each joint",
                },
                "rotation_format": "axis-angle (3 values per joint), convert to quaternion for Blender",
            },
        }

        return (
            "PROMPT TO PASTE IN A NEW CLAUDE CONVERSATION\n"
            "===========================================\n\n"
            "You are connected to Blender through BlenderMCP. If the BlenderMCP tools "
            "are not available, stop and ask the user to enable them.\n\n"
            "Task: Animate the mesh rig from SMPL motion data.\n\n"

            "STEP 1 - LOAD DATA:\n"
            "Read the .npz file with numpy. Extract smpl_poses (N,72), "
            "smpl_trans (N,3), fps, joint_names.\n\n"

            "STEP 2 - COORDINATE SYSTEM CONVERSION:\n"
            "SMPL uses Y-up coordinate system. Blender uses Z-up.\n"
            "For EVERY rotation and translation:\n"
            "  - Use scipy.spatial.transform.Rotation to convert axis-angle to quaternion\n"
            "  - Apply coordinate swap: (x, y, z) -> (x, z, -y)\n"
            "  - NEVER do manual axis-angle math. Always go through scipy.\n\n"

            "STEP 3 - REST POSE OFFSET:\n"
            "SMPL rest pose is a T-pose. The mesh rig may have a different rest pose "
            "(A-pose or custom). Before applying rotations:\n"
            "  - Read each bone's rest-pose rotation in the rig\n"
            "  - Compute the offset between SMPL T-pose and rig rest pose\n"
            "  - Compose: final_rotation = offset_inv * smpl_rotation * offset\n"
            "  - Without this step, shoulders and arms WILL be dislocated.\n\n"

            "STEP 4 - APPLY ROTATIONS:\n"
            "For each frame, for each of the 24 joints:\n"
            "  - Extract the 3 axis-angle values from smpl_poses[frame, joint*3 : joint*3+3]\n"
            "  - Convert to quaternion (scipy)\n"
            "  - Apply coordinate conversion\n"
            "  - Apply rest pose offset\n"
            "  - Set bone.rotation_quaternion and keyframe_insert\n"
            "For the root (pelvis): also set bone.location from smpl_trans and keyframe.\n\n"

            "STEP 5 - TEMPORAL SMOOTHING:\n"
            "After all keyframes are set:\n"
            "  - Smooth quaternion curves using SLERP interpolation\n"
            "  - NEVER smooth Euler angles (gimbal lock, discontinuities)\n"
            "  - Apply a 3-frame moving average on the log-quaternion if jitter remains\n\n"

            "STEP 6 - FOOT CONTACT CORRECTION:\n"
            "Foot skating is the most visible artifact. After animation:\n"
            "  - Detect frames where feet should be planted (low velocity + low height)\n"
            "  - Add IK constraints on ankle bones for those frame ranges\n"
            "  - Pin foot position to the ground plane during contact\n\n"

            "STEP 7 - MULTI-ANGLE VERIFICATION:\n"
            "After animation is applied, verify from EACH of the 6 cameras:\n"
            "  - Render/inspect the animated pose from front, back, left, right, top, bottom\n"
            "  - Check for: limbs penetrating the mesh, impossible joint angles, "
            "asymmetric movements that should be symmetric\n"
            "  - If a contradiction is found between views, adjust the problematic frames\n\n"

            "CONSTRAINTS:\n"
            "  - Bone lengths MUST remain constant across all frames\n"
            "  - Do NOT invent motion for body parts not visible in the source video\n"
            "  - Keep the mesh parented to the same armature bones throughout\n"
            "  - Set scene FPS to match the motion data FPS before keyframing\n"
            "  - Ignore every object, person, prop, background element not in the references\n\n"

            "Payload JSON:\n"
            f"{json.dumps(payload, indent=2)}\n"
        )

    def _build_mediapipe_request(self, context, props, mesh, cameras, motion_path, media):
        payload = {
            "addon": "video_mocap_mcp",
            "version": "0.4.0",
            "task": "animate",
            "motion_source": "mediapipe_fallback",
            "quality_warning": (
                "MediaPipe provides 33 independent keypoints with noisy depth. "
                "This is a DEGRADED fallback. For better results, use SMPL method."
            ),
            "blend_file": bpy.data.filepath,
            "scene": context.scene.name,
            "mesh": _mesh_summary(mesh),
            "camera_setup": cameras,
            "media_sources": media,
            "rig_settings": _rig_settings(props),
            "motion_data_file": motion_path if motion_path and os.path.isfile(motion_path) else "",
            "frame_range": {
                "start": props.frame_start,
                "end": props.frame_end,
                "fps": context.scene.render.fps / context.scene.render.fps_base,
            },
        }

        return (
            "PROMPT TO PASTE IN A NEW CLAUDE CONVERSATION\n"
            "===========================================\n\n"
            "You are connected to Blender through BlenderMCP.\n\n"
            "Task: Animate the mesh rig from reference videos/images.\n\n"
            "WARNING: This is the degraded MediaPipe fallback pipeline.\n\n"
            "If a motion data file is provided, read it and use the landmark "
            "positions to compute bone rotations via shortest-arc quaternion.\n"
            "Otherwise, analyze the reference videos visually.\n\n"
            "Apply the same multi-angle verification from the 6 cameras.\n"
            "Constrain bone lengths to remain constant.\n"
            "Smooth using quaternion SLERP, never Euler angles.\n"
            "Do NOT invent motion for body parts not visible in the source.\n\n"
            "Payload JSON:\n"
            f"{json.dumps(payload, indent=2)}\n"
        )


# ------------------------------------------------------------------
# Operator: Run full pipeline
# ------------------------------------------------------------------
class VMMCP_OT_run_all(Operator):
    """Extract motion + generate rig and animate requests."""
    bl_idname = "video_mocap.run_all"
    bl_label = "Run Full Pipeline"
    bl_options = {"REGISTER"}

    def execute(self, context):
        if bpy.ops.video_mocap.extract() != {"FINISHED"}:
            return {"CANCELLED"}
        if bpy.ops.video_mocap.rig_mesh() != {"FINISHED"}:
            return {"CANCELLED"}
        self.report({"INFO"}, "Pipeline done. Rig request in clipboard. After rigging, click Animate.")
        return {"FINISHED"}


# ------------------------------------------------------------------
# Operator: Setup cameras
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
# Operator: Copy request to txt
# ------------------------------------------------------------------
class VMMCP_OT_copy_request_to_txt(Operator):
    bl_idname = "video_mocap.copy_request_to_txt"
    bl_label = "Copy Request to txt"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        if not props.request_text_name:
            self.report({"ERROR"}, "No request generated yet.")
            return {"CANCELLED"}
        out_path = _export_request_to_txt(context, props)
        if not out_path:
            self.report({"ERROR"}, f"Request text block not found: {props.request_text_name}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Request exported: {out_path}")
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

        # Video source for SMPL
        box = layout.box()
        box.label(text="Motion Source", icon="FILE_MOVIE")
        box.prop(props, "video_path")
        box.prop(props, "estimation_method")
        if props.estimation_method == "SMPL":
            box.prop(props, "smpl_method")
        elif props.estimation_method == "MEDIAPIPE":
            box.label(text="WARNING: Degraded quality", icon="ERROR")
        box.prop(props, "python_exe")

        # Reference videos (for Claude visual verification)
        box = layout.box()
        box.label(text="Reference Views (verification)", icon="CAMERA_DATA")
        box.prop(props, "front_video")
        box.prop(props, "back_video")
        box.prop(props, "left_video")
        box.prop(props, "right_video")
        box.prop(props, "top_video")
        box.prop(props, "bottom_video")
        box.prop(props, "image_sequence_dir")

        # Rig & Scene
        box = layout.box()
        box.label(text="Rig & Scene", icon="ARMATURE_DATA")
        box.prop(props, "rig_preset")
        box.prop(props, "requested_bone_count")
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
        col.operator("video_mocap.animate", icon="PLAY")
        layout.separator()
        layout.operator("video_mocap.run_all", icon="SEQ_SEQUENCER")

        # Status
        if props.extraction_status:
            layout.label(text=f"Extraction: {props.extraction_status}")
        if props.motion_data_path:
            layout.label(text=f"Data: {os.path.basename(props.motion_data_path)}", icon="FILE")
        if props.request_text_name:
            layout.label(text=f"Request: {props.request_text_name}", icon="TEXT")
            layout.prop(props, "request_txt_path")
            layout.operator("video_mocap.copy_request_to_txt", icon="FILE_TEXT")


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
    VMMCP_OT_copy_request_to_txt,
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
