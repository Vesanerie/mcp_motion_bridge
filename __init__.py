bl_info = {
    "name": "MCP_Motion_Bridge",
    "author": "You",
    "version": (0, 8, 1),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Prepare motion capture context for Claude Code via BlenderMCP.",
    "category": "Animation",
}

import json
import os

import bpy
from bpy.props import EnumProperty, FloatProperty, IntProperty, StringProperty
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
    mesh_source: EnumProperty(
        name="Mesh Source",
        items=[
            ("SCENE", "Scene Mesh", "Use a mesh already in the Blender scene"),
            ("SMPL", "SMPL Body", "Import a SMPL base mesh as the target"),
        ],
        default="SCENE",
    )
    mesh_object: StringProperty(
        name="Mesh",
        description="Mesh object to rig and animate (auto-detected if selected)",
        default="",
    )
    smpl_model_path: StringProperty(
        name="SMPL Model",
        description="Path to SMPL model file (.pkl) or exported mesh (.obj / .npz)",
        subtype="FILE_PATH",
        default="",
    )
    smpl_gender: EnumProperty(
        name="Gender",
        items=[
            ("neutral", "Neutral", ""),
            ("male", "Male", ""),
            ("female", "Female", ""),
        ],
        default="neutral",
    )
    video_mode: EnumProperty(
        name="Video Mode",
        items=[
            ("MULTI_FILE", "One File per View", "One video file per camera angle"),
            ("SINGLE_FILE", "Single File – Multi-Angle",
             "One video containing all angles at once (split-screen or sequential)"),
        ],
        default="MULTI_FILE",
    )
    multi_angle_video: StringProperty(
        name="Multi-Angle Video",
        description="Video file that contains all angles simultaneously or sequentially",
        subtype="FILE_PATH",
        default="",
    )
    video_layout: EnumProperty(
        name="Layout",
        items=[
            ("AUTO",       "Auto-detect",    "Claude detects the layout automatically"),
            ("2x1",        "2×1 side-by-side", "Two angles side by side"),
            ("1x2",        "1×2 stacked",    "Two angles stacked vertically"),
            ("2x2",        "2×2 grid",       "Four angles in a 2×2 grid"),
            ("3x2",        "3×2 grid",       "Six angles in a 3×2 grid"),
            ("SEQUENTIAL", "Sequential",     "Angles appear one after another in time"),
        ],
        default="AUTO",
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
    user_camera: StringProperty(
        name="Protected Camera",
        description="This camera will never be deleted (your render/hero camera)",
        default="",
    )
    cleanup_cameras: bpy.props.BoolProperty(
        name="Delete Other Cameras",
        description="Delete all cameras that are not VMMCP analysis cameras and not the protected camera",
        default=False,
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
    center, _min_v, _max_v, size, radius = _world_bbox(mesh)
    distance = radius * props.camera_distance
    # Use the full 3D diagonal so the mesh is always fully visible regardless of view axis
    ortho_scale = size.length * 1.15

    if props.cleanup_cameras:
        vmmcp_names = {f"VMMCP_{v.upper()}_Camera" for v, _ in CAMERA_SPECS}
        protected = props.user_camera.strip()
        to_delete = [
            obj for obj in list(context.scene.objects)
            if obj.type == "CAMERA"
            and obj.name not in vmmcp_names
            and obj.name != protected
        ]
        for obj in to_delete:
            bpy.data.objects.remove(obj, do_unlink=True)

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
        cam_obj.data.type = "ORTHO"
        cam_obj.data.ortho_scale = ortho_scale
        cam_obj.data.clip_end = max(distance * 10.0, 1000.0)
        cam_obj.data.show_passepartout = True
        cam_obj.data.passepartout_alpha = 1.0
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
    if props.video_mode == "SINGLE_FILE":
        path = _abspath(props.multi_angle_video)
        if path and os.path.isfile(path):
            return {"_single_file": path, "_layout": props.video_layout}
        return {}
    sources = {
        "front": _abspath(props.front_video),
        "back": _abspath(props.back_video),
        "left": _abspath(props.left_video),
        "right": _abspath(props.right_video),
        "top": _abspath(props.top_video),
        "bottom": _abspath(props.bottom_video),
    }
    return {k: v for k, v in sources.items() if v and os.path.isfile(v)}


def _import_smpl_mesh(context, model_path, gender):
    """Import SMPL base mesh from a .pkl, .obj, or .npz file."""
    obj_name = f"SMPL_{gender.upper()}"

    existing = bpy.data.objects.get(obj_name)
    if existing and existing.type == "MESH":
        return existing

    ext = os.path.splitext(model_path)[1].lower()

    if ext == ".obj":
        try:
            bpy.ops.wm.obj_import(filepath=model_path)
        except AttributeError:
            bpy.ops.import_scene.obj(filepath=model_path)
        imported = next((o for o in context.selected_objects if o.type == "MESH"), None)
        if imported:
            imported.name = obj_name
        return imported

    if ext == ".pkl":
        import pickle
        import numpy as np
        with open(model_path, "rb") as fh:
            model = pickle.load(fh, encoding="latin1")
        verts = model["v_template"].tolist()
        faces = model["f"].astype(int).tolist()
        mesh_data = bpy.data.meshes.new(obj_name)
        mesh_data.from_pydata(verts, [], faces)
        mesh_data.update()
        obj = bpy.data.objects.new(obj_name, mesh_data)
        context.collection.objects.link(obj)
        return obj

    if ext == ".npz":
        import numpy as np
        data = np.load(model_path, allow_pickle=True)
        verts = data["v_template"].tolist() if "v_template" in data else data["vertices"].tolist()
        faces = data["f"].astype(int).tolist() if "f" in data else data["faces"].astype(int).tolist()
        mesh_data = bpy.data.meshes.new(obj_name)
        mesh_data.from_pydata(verts, [], faces)
        mesh_data.update()
        obj = bpy.data.objects.new(obj_name, mesh_data)
        context.collection.objects.link(obj)
        return obj

    return None


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
    single_file_mode = "_single_file" in videos

    if single_file_mode:
        single_path = videos["_single_file"]
        layout = videos.get("_layout", "AUTO")
        ref_videos_payload = {
            "mode": "single_file_multi_angle",
            "video_path": single_path,
            "layout": layout,
        }
    else:
        ref_videos_payload = videos

    payload = {
        "addon": "video_mocap_mcp",
        "version": "0.8.1",
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "mesh": _mesh_summary(mesh),
        "camera_setup": cameras,
        "camera_policy": {
            "protected_camera": props.user_camera.strip() or None,
            "vmmcp_prefix": "VMMCP_",
            "deletable": "Any camera whose name does NOT start with 'VMMCP_' and is NOT the protected_camera may be deleted.",
        },
        "reference_videos": ref_videos_payload,
        "frame_range": {
            "start": props.frame_start,
            "end": props.frame_end,
            "fps": context.scene.render.fps / context.scene.render.fps_base,
        },
    }

    camera_list = "\n".join(f"  - {view}: {name}" for view, name in cameras.items())

    if single_file_mode:
        video_section = (
            "REFERENCE VIDEO (single file — multiple angles):\n"
            f"  Path   : {single_path}\n"
            f"  Layout : {layout}\n"
            "  Each angle region must be treated as a fully independent viewpoint.\n"
        )
    else:
        video_section = (
            "REFERENCE VIDEOS (one per viewpoint):\n"
            + "\n".join(f"  - {view}: {path}" for view, path in videos.items())
        )

    step_0b = (
        "STEP 0b — VIDEO SPLITTING (single-file multi-angle mode):\n"
        "The reference video contains multiple camera angles in a single file.\n"
        f"Declared layout: {layout}\n"
        "Before running the pose estimator, split the video into per-angle clips:\n"
        "  a) If layout is AUTO, inspect the first frame to detect the grid or\n"
        "     sequential structure (number of panels, their positions).\n"
        "  b) For each angle region, crop and save as a temporary file:\n"
        "       ffmpeg -i <input> -vf 'crop=W:H:X:Y' /tmp/vmmcp_angle_<N>.mp4\n"
        "     Adjust W, H, X, Y to isolate each panel precisely.\n"
        "  c) Each cropped file is ONE independent viewpoint — NEVER mix frames\n"
        "     or pixel regions across different angle crops.\n"
        "  d) Label each crop by its apparent camera direction (front, back, left,\n"
        "     right, top, bottom) based on the body pose visible in it.\n"
        "  e) Use these per-angle files as the sole inputs to STEP 1.\n\n"
    ) if single_file_mode else ""

    step_1_note = (
        "Input: the per-angle clips produced in STEP 0b.\n"
        if single_file_mode else
        "Run extraction on EVERY available video — do not skip any.\n"
    )

    return (
        "PROMPT FOR CLAUDE CODE (connected to Blender via BlenderMCP)\n"
        "=============================================================\n\n"

        "You have full access to Blender through BlenderMCP. You must do ALL the\n"
        "work yourself: camera framing, motion extraction, rigging, animation, verification.\n"
        "The user will not intervene — execute everything autonomously.\n\n"

        "CONTEXT:\n"
        "The payload below describes a Blender scene with a mesh and reference\n"
        "videos showing a person performing a movement filmed from different\n"
        "angles. Your job is to reproduce that movement on the mesh.\n\n"

        f"{video_section}\n\n"

        "ANALYSIS CAMERAS (already placed in the scene):\n"
        f"{camera_list}\n\n"

        "CAMERA MANAGEMENT RULES:\n"
        "  - VMMCP_* cameras (analysis cameras) must NEVER be deleted.\n"
        + (
            f"  - '{props.user_camera.strip()}' is the user's protected camera — do NOT delete it under any circumstance.\n"
            if props.user_camera.strip() else
            "  - No user camera is protected (user_camera is empty).\n"
        ) +
        "  - Every other camera in the scene may be freely deleted if it clutters the setup.\n\n"

        "FULL PIPELINE TO EXECUTE:\n\n"

        "STEP 0 — CAMERA FRAMING VERIFICATION:\n"
        "The addon has already configured every VMMCP camera:\n"
        "  - Orthographic projection (type = 'ORTHO')\n"
        "  - ortho_scale = mesh_bbox_diagonal * 1.15 (15 % margin)\n"
        "  - passepartout_alpha = 1.0 (full black mask outside frame)\n"
        "Your task is to VERIFY and CORRECT each camera before proceeding:\n"
        "  a) Switch to each VMMCP camera in turn\n"
        "  b) Confirm the complete mesh (tip to toe) is fully inside the frame\n"
        "  c) If the mesh is not centered or partially clipped, reposition the camera\n"
        "     so the mesh bbox center aligns with the camera's line of sight, then\n"
        "     increase ortho_scale if necessary until the mesh fits with margin\n"
        "  d) Do NOT change camera.data.type away from 'ORTHO'\n"
        "  e) Do NOT skip any camera — all viewpoints must be verified\n\n"

        + step_0b +

        "STEP 1 — MOTION EXTRACTION:\n"
        "PRIMARY estimator: MediaPipe + IK (run_mediapipe_ik.py).\n"
        + step_1_note +
        "The estimator script is at: estimator/run_mediapipe_ik.py in the addon folder.\n"
        "The Python env is ~/mp_env (mediapipe, opencv, numpy, scipy).\n"
        "Run via subprocess FOR EACH VIDEO:\n"
        "  ~/mp_env/bin/python estimator/run_mediapipe_ik.py \\\n"
        "    --video <path> --out <path>_<view>.npz\n"
        "Output: per-frame rotations for 24 joints in axis-angle format, .npz.\n"
        "COORDINATE SYSTEM — MediaPipe convention (MUST apply before any IK or rotation):\n"
        "  MediaPipe: X = subject's right, Y = down, Z = away from subject\n"
        "  Blender  : X = right, Y = forward, Z = up\n"
        "  Conversion per landmark: blender_pos = (-mp.x, mp.z, -mp.y)\n"
        "  Apply this to EVERY position and direction vector before use.\n"
        "  Failure to apply this transform causes upside-down or mirrored poses.\n"
        "FALLBACK: if run_mediapipe_ik.py fails, use estimator/run_4dhumans.py with\n"
        "  PYTORCH_ENABLE_MPS_FALLBACK=1 ~/hmr2_env/bin/python \\\n"
        "    estimator/run_4dhumans.py --video <path> --out <path>_<view>.npz\n"
        "  WARNING: HMR2 on Apple Silicon MPS produces near-static poses (std ≤ 0.06°\n"
        "  per joint) on large movements — verify output variance before trusting it.\n"
        "IMPORTANT: each viewpoint constrains different body parts. Use front/back as\n"
        "the primary source; fuse lateral/top/bottom to resolve ambiguities.\n"
        "Every viewpoint must be processed — no skipping.\n"
        "VISUAL AIDS: If the video contains grid overlays, strong lines, measurement\n"
        "markers, motion capture dots, or scale references printed on the character\n"
        "or the background, treat them as hard positional constraints:\n"
        "  - Grid lines → use intersections to infer absolute scale and orientation\n"
        "  - Markers on the body → lock those landmarks to their pixel positions\n"
        "    per frame and use them to correct estimator output where they disagree\n"
        "  - Strong contrast lines on clothing → use as limb direction cues\n"
        "  - Scale/measurement rulers → calibrate world-space joint distances\n"
        "Do NOT ignore these visual aids — they are more reliable than estimator priors.\n\n"

        "STEP 2 — BONE SURVEY, MAPPING AND RIGGING:\n\n"

        "STEP 2a — BONE SURVEY (do this first, before touching anything):\n"
        "List EVERY bone in every armature present in the scene. For each bone record:\n"
        "  - Exact name (case-sensitive)\n"
        "  - Parent bone name (or 'ROOT' if none)\n"
        "  - Head world position (x, y, z)\n"
        "  - Whether it has vertex weights on the mesh\n"
        "Print this list explicitly before proceeding. Do NOT skip bones.\n"
        "HARD RULE: do NOT rename, delete, reparent, or restructure any existing bone.\n"
        "           Do NOT create a new armature if one already exists.\n\n"

        "STEP 2b — SMPL→RIG MAPPING (build this before touching keyframes):\n"
        "The 24 SMPL joints are:\n"
        "  pelvis, left_hip, right_hip, spine1, left_knee, right_knee, spine2,\n"
        "  left_ankle, right_ankle, spine3, left_foot, right_foot, neck,\n"
        "  left_collar, right_collar, head, left_shoulder, right_shoulder,\n"
        "  left_elbow, right_elbow, left_wrist, right_wrist, left_hand, right_hand\n"
        "For EACH SMPL joint, find the best matching existing bone using this order:\n"
        "  1. Exact name match (case-insensitive, strip prefixes DEF- ORG- MCH-)\n"
        "  2. Partial name match (e.g. 'thigh.L' matches 'left_hip', 'forearm.R' matches\n"
        "     'right_elbow', 'spine' matches 'spine1'/'spine2'/'spine3')\n"
        "  3. Positional proximity: compare bone head world position to the expected\n"
        "     anatomical position of the SMPL joint on the mesh\n"
        "  4. If no match found: mark as UNMAPPED — do NOT invent a bone or skip silently\n"
        "Output the final mapping as an explicit dict, e.g.:\n"
        "  smpl_to_rig = {\n"
        "    'pelvis':        'root',\n"
        "    'left_hip':      'thigh.L',\n"
        "    'right_shoulder':'upper_arm.R',\n"
        "    'left_hand':     UNMAPPED,\n"
        "    ...\n"
        "  }\n"
        "Show this dict in full before proceeding to STEP 2c.\n\n"

        "STEP 2c — ROOT MOTION BONE IDENTIFICATION:\n"
        "Identify the bone that controls GLOBAL body position — the one whose location\n"
        "keyframes move the entire character in world space (jumps, runs, displacement).\n"
        "Candidates (check in this order):\n"
        "  - Bone with no parent at all\n"
        "  - Bone named: root, master, COG, center_of_gravity, hips, pelvis, torso\n"
        "  - Topmost bone in the hierarchy that is NOT a helper/IK target\n"
        "Name this bone 'root_motion_bone'. It will receive smpl_trans location data.\n"
        "If no clear root bone exists, use the pelvis-mapped bone as fallback.\n"
        "Print: 'ROOT MOTION BONE: <name>' before proceeding.\n\n"

        "STEP 2d — RIGGING (only if no armature exists yet):\n"
        "If the mesh has NO armature modifier and NO armature in the scene:\n"
        "  - Create a 24-bone SMPL armature from scratch\n"
        "  - Place bones anatomically inside the mesh\n"
        "  - Verify from ALL 6 cameras before proceeding\n"
        "  - Parent mesh to armature with automatic weights\n"
        "  - Add IK constraints on ankles and wrists\n"
        "If an armature already exists: skip creation entirely. Work with what exists.\n\n"

        "STEP 3 — ANIMATION TRANSFER:\n"
        "Use smpl_to_rig and root_motion_bone from STEP 2. Skip any SMPL joint marked\n"
        "UNMAPPED — do NOT approximate with a wrong bone.\n"
        "Use the front/back .npz as base; fuse lateral/top/bottom for ambiguous joints.\n\n"
        "BEFORE the frame loop — build per-bone rest offsets (MANDATORY, no exceptions):\n"
        "  rest_offsets = {}\n"
        "  bpy.ops.object.mode_set(mode='EDIT')\n"
        "  for bone in armature.data.edit_bones:\n"
        "      rest_offsets[bone.name] = bone.matrix.to_quaternion()\n"
        "  bpy.ops.object.mode_set(mode='OBJECT')\n"
        "  rest_inv = {name: q.conjugated() for name, q in rest_offsets.items()}\n\n"
        "For each frame f, for each SMPL joint that is mapped:\n"
        "  a) Extract axis-angle (3 values) from smpl_poses[frame, joint*3:joint*3+3]\n"
        "  b) Convert to quaternion via scipy:\n"
        "       r = Rotation.from_rotvec(axis_angle)\n"
        "       q = Quaternion(r.as_quat()[[3,0,1,2]])  # scipy xyzw → Blender wxyz\n"
        "     NEVER do manual axis-angle math.\n"
        "  c) Coordinate conversion (MediaPipe output):\n"
        "       q_bl = Quaternion((q.w, -q.x, q.z, -q.y))\n"
        "     If using HMR2/SMPL (Y-up source): q_bl = Quaternion((q.w, q.x, q.z, -q.y))\n"
        "  d) Rest-offset composition — REQUIRED for EVERY mapped bone, no exceptions.\n"
        "     Skipping produces dislocated shoulders and twisted limbs on any rig whose\n"
        "     rest pose is not a perfect T-pose:\n"
        "       rig_bone = smpl_to_rig[smpl_joint]\n"
        "       final_rot = rest_inv[rig_bone] @ q_bl @ rest_offsets[rig_bone]\n"
        "  e) pose_bone.rotation_mode = 'QUATERNION'\n"
        "     pose_bone.rotation_quaternion = final_rot\n"
        "     pose_bone.keyframe_insert('rotation_quaternion', frame=f)\n\n"
        "ROOT MOTION — apply for EVERY frame (handles jumps, runs, any global displacement):\n"
        "  trans = smpl_trans[frame]           # (x, y, z) from .npz\n"
        "  # MediaPipe coord conversion:\n"
        "  loc = Vector((-trans[0], trans[2], -trans[1]))\n"
        "  # HMR2/SMPL coord conversion:\n"
        "  # loc = Vector((trans[0], trans[2], -trans[1]))\n"
        "  root_pose_bone.location = loc\n"
        "  root_pose_bone.keyframe_insert('location', frame=f)\n"
        "IMPORTANT: root motion location keyframes are what make the character travel\n"
        "in world space. Without them, jumps and runs will appear to happen in place.\n\n"

        "STEP 4 — TEMPORAL SMOOTHING:\n"
        "Smooth ALL rotation curves using quaternion SLERP or log-quaternion filter.\n"
        "NEVER smooth Euler angles (gimbal lock, discontinuities).\n"
        "Apply a 3-frame moving average if jitter remains.\n\n"

        "STEP 5 — FOOT CONTACT CORRECTION:\n"
        "Foot skating is the worst visual artifact. Fix it:\n"
        "  - Detect frames where feet should be planted (low velocity + low height)\n"
        "  - Activate IK constraints on ankle bones for those frame ranges\n"
        "  - Pin foot position to the ground plane during contact\n\n"

        "STEP 6 — MULTI-ANGLE VERIFICATION AND CORRECTION:\n"
        "This is MANDATORY. Execute it for every available viewpoint, not just 'some'.\n"
        "For EACH angle (whether a separate file or a crop from the single video):\n"
        "  a) Switch to the corresponding analysis camera\n"
        "  b) Play back the animation side-by-side with the reference footage\n"
        "  c) Check every frame for: limbs penetrating mesh, impossible joint angles,\n"
        "     asymmetric motion that should be symmetric, floating or sliding feet,\n"
        "     limb positions contradicted by this viewpoint; also cross-check against\n"
        "     any grid overlays, markers, or strong lines visible in the footage\n"
        "  d) If ANY contradiction is found — adjust those keyframes immediately\n"
        "  e) After adjusting, re-verify from ALL other viewpoints to ensure\n"
        "     the correction did not introduce a new error in another view\n"
        "Loop until ALL viewpoints are consistent with their reference footage.\n"
        "Do NOT declare the work done until every viewpoint has passed this check.\n\n"

        "HARD CONSTRAINTS:\n"
        "  - Bone lengths MUST remain constant across all frames\n"
        "  - Do NOT rename, delete, reparent, or restructure any existing bone\n"
        "  - Do NOT create a new armature if one already exists in the scene\n"
        "  - Do NOT apply rotations to UNMAPPED bones — leave them at rest pose\n"
        "  - Do NOT invent motion for body parts not visible in any video\n"
        "  - Keep mesh parented to the same armature throughout\n"
        "  - Set scene FPS to match motion data FPS before keyframing\n"
        "  - Ignore objects/people/props not present in the reference videos\n"
        "  - Every provided viewpoint MUST be used — skipping a video is not allowed\n\n"

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

        if props.mesh_source == "SMPL":
            model_path = bpy.path.abspath(props.smpl_model_path)
            if not model_path or not os.path.isfile(model_path):
                self.report({"ERROR"}, "SMPL model file not found — set a valid path.")
                return {"CANCELLED"}
            mesh = _import_smpl_mesh(context, model_path, props.smpl_gender)
            if mesh is None:
                self.report({"ERROR"}, "Failed to import SMPL mesh. Check file format (.pkl / .obj / .npz).")
                return {"CANCELLED"}
            props.mesh_object = mesh.name
        else:
            mesh = _target_mesh(context, props)
            if mesh is None:
                self.report({"ERROR"}, "Select a mesh object first.")
                return {"CANCELLED"}

        videos = _video_sources(props)
        if not videos:
            if props.video_mode == "SINGLE_FILE":
                self.report({"ERROR"}, "Set a valid multi-angle video file.")
            else:
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

        # Mesh source
        box = layout.box()
        box.label(text="Mesh", icon="MESH_DATA")
        box.prop(props, "mesh_source", expand=True)
        if props.mesh_source == "SCENE":
            box.prop_search(props, "mesh_object", bpy.data, "objects")
        else:
            box.prop(props, "smpl_model_path")
            box.prop(props, "smpl_gender")

        # Videos
        box = layout.box()
        box.label(text="Reference Videos", icon="FILE_MOVIE")
        box.prop(props, "video_mode", expand=True)
        if props.video_mode == "SINGLE_FILE":
            box.prop(props, "multi_angle_video")
            box.prop(props, "video_layout")
        else:
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

        # Camera cleanup
        box = layout.box()
        box.label(text="Cameras", icon="CAMERA_DATA")
        box.prop(props, "cleanup_cameras")
        if props.cleanup_cameras:
            row = box.row()
            row.alert = not props.user_camera.strip()
            row.prop_search(props, "user_camera", bpy.data, "objects",
                            icon="CAMERA_DATA")

        layout.separator()

        # The one button
        layout.operator("video_mocap.generate", icon="COPYDOWN", text="Generate Prompt")
        layout.label(text="Then paste in Claude Code + BlenderMCP", icon="INFO")

        # Estimator info
        layout.separator()
        info = layout.box()
        info.label(text="Estimator: 4D-Humans / HMR2 (SMPL)", icon="ARMATURE_DATA")
        info.label(text="Env: ~/hmr2_env (macOS Apple Silicon OK)")
        info.label(text="Output: .npz with 24-joint rotations")


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
