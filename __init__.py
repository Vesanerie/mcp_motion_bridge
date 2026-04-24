bl_info = {
    "name": "MCP_Motion_Bridge",
    "author": "You",
    "version": (0, 9, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Prepare motion capture context for Claude Code via BlenderMCP.",
    "category": "Animation",
}

import json
import os
import shutil
import subprocess
import threading

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
    pipeline_running: bpy.props.BoolProperty(name="Pipeline Running", default=False)
    pipeline_status: bpy.props.StringProperty(name="Pipeline Status", default="")


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
    # Fixed 1m base distance — Claude adjusts later if needed
    distance = 1.0
    # Ortho scale: large enough to see the full mesh with generous margin
    ortho_scale = max(size.length * 2.0, 3.0)

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
        # Square sensor for uniform framing on all axes
        cam_obj.data.sensor_fit = "AUTO"
        cam_obj.data.sensor_width = 36.0
        cam_obj.data.sensor_height = 36.0
        cameras[view_name] = cam_obj.name

    # Set render resolution to square so camera view matches
    context.scene.render.resolution_x = 1024
    context.scene.render.resolution_y = 1024
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
# Sequential pipeline — state, thread runner, bpy timer
# ------------------------------------------------------------------
_pipe = {
    "running":     False,
    "step_idx":    0,
    "steps":       [],   # list of (marker, label, step_text)
    "history":     "",   # growing log: context + previous responses
    "status":      "",
    "error":       "",
    "thread":      None,
    "scene_name":  "",
}
_pipe_lock = threading.Lock()


def _steps_dir():
    """Return the VMMCP_Steps directory path (next to the .blend file or in temp)."""
    base = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else bpy.app.tempdir
    d = os.path.join(base, "VMMCP_Steps")
    os.makedirs(d, exist_ok=True)
    return d


def _write_step_file(step_idx, marker, label, full_prompt):
    """Write a step prompt as a .md file in VMMCP_Steps/."""
    d = _steps_dir()
    filename = f"step_{step_idx:02d}_{marker.replace('_OK', '')}.md"
    path = os.path.join(d, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(full_prompt)
    return path


def _write_current_step_pointer(step_idx, path):
    """Write CURRENT_STEP.md that tells Claude which step to execute."""
    d = _steps_dir()
    pointer = os.path.join(d, "CURRENT_STEP.md")
    with open(pointer, "w", encoding="utf-8") as f:
        f.write(f"# Current Step: {step_idx}\n\n")
        f.write(f"Execute the prompt in: {path}\n\n")
        f.write("After reading this, open and execute the step file above.\n")
        f.write("When done, signal completion via BlenderMCP:\n")
        f.write(f"  bpy.context.scene['vmmcp_step_done'] = '<MARKER>'\n")
        f.write("(The exact marker is specified at the end of the step file.)\n")
    return pointer


def _clear_steps_dir():
    """Remove all step files from VMMCP_Steps/."""
    d = _steps_dir()
    for f in os.listdir(d):
        if f.endswith(".md"):
            os.unlink(os.path.join(d, f))


def _ensure_mcp_config(steps_dir):
    """Write .mcp.json in the steps dir so Claude Code CLI has BlenderMCP access."""
    mcp_path = os.path.join(steps_dir, ".mcp.json")
    config = {
        "mcpServers": {
            "blender": {
                "command": "uvx",
                "args": ["blender-mcp"]
            }
        }
    }
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    return mcp_path


def _launch_claude_terminal(steps_dir, first_step_path):
    """Open a new terminal window with Claude Code session in the steps dir.
    Works on macOS and Windows.
    """
    import sys

    # Write .mcp.json so claude has BlenderMCP
    _ensure_mcp_config(steps_dir)

    # Find claude CLI
    claude_path = None
    candidates = [
        shutil.which("claude"),
        os.path.expanduser("~/.nvm/versions/node/v24.14.1/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]
    nvm_dir = os.path.expanduser("~/.nvm/versions/node")
    if os.path.isdir(nvm_dir):
        for v in sorted(os.listdir(nvm_dir), reverse=True):
            candidates.append(os.path.join(nvm_dir, v, "bin", "claude"))
    if sys.platform == "win32":
        candidates.extend([
            os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.expandvars(r"%PROGRAMFILES%\nodejs\claude.cmd"),
        ])
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            claude_path = c
            break

    if not claude_path:
        raise RuntimeError("'claude' CLI not found. Install Claude Code CLI first.")

    # First message: tell Claude to read and execute the first step
    first_msg = f"Read and execute the prompt in {first_step_path}"

    if sys.platform == "darwin":
        # macOS: open Terminal.app with claude
        script = f'''
        tell application "Terminal"
            activate
            do script "cd '{steps_dir}' && '{claude_path}' \\"{first_msg}\\""
        end tell
        '''
        subprocess.run(["osascript", "-e", script], timeout=10)

    elif sys.platform == "win32":
        # Windows: open cmd with claude
        cmd = f'start cmd /k "cd /d {steps_dir} && {claude_path} "{first_msg}""'
        subprocess.run(cmd, shell=True, timeout=10)

    else:
        # Linux fallback
        subprocess.Popen(
            ["x-terminal-emulator", "-e",
             f"bash -c 'cd {steps_dir} && {claude_path} \"{first_msg}\"'"],
        )


def _run_one_step(marker, label, full_prompt):
    """Write step prompt to .md file."""
    with _pipe_lock:
        idx = _pipe["step_idx"]
        path = _write_step_file(idx, marker, label, full_prompt)
        _write_current_step_pointer(idx, path)
        _pipe["status"] = f"{label} — written to {os.path.basename(path)}"


def _pipeline_tick():
    """bpy.app.timers callback — sends one step, then stops.
    User clicks 'Next Step' to send the next one (Claude needs time to execute)."""
    with _pipe_lock:
        if not _pipe["running"]:
            _sync_status_to_scene()
            return None

        if _pipe["error"]:
            _sync_status_to_scene()
            _pipe["running"] = False
            return None

        thread = _pipe["thread"]
        if thread is not None and thread.is_alive():
            _sync_status_to_scene()
            return 1.0

        # Thread finished — mark step as sent, pause for user
        idx = _pipe["step_idx"]
        steps = _pipe["steps"]

        if idx >= len(steps):
            _pipe["running"] = False
            _pipe["status"] = f"All {len(steps)} steps sent to Claude Code!"
            _sync_status_to_scene(finished=True)
            return None

        # Step was just sent — pause pipeline, wait for user to click Next
        _pipe["running"] = False
        _pipe["status"] = f"Step {idx}/{len(steps)} sent — click 'Next Step' when Claude is done"
        _sync_status_to_scene()
    return None


def _sync_status_to_scene(finished=False):
    """Write _pipe status into the Blender scene property (called from main thread)."""
    scene = bpy.data.scenes.get(_pipe.get("scene_name", ""))
    if scene is None:
        return
    props = scene.vmmcp
    if _pipe["error"]:
        props.pipeline_status = f"ERROR: {_pipe['error']}"
        props.pipeline_running = False
    else:
        props.pipeline_status = _pipe["status"]
        props.pipeline_running = not finished and _pipe["running"]


# ------------------------------------------------------------------
# Sequential pipeline — context + step builders
# ------------------------------------------------------------------
def _build_context(context, mesh, cameras, videos, props):
    """Compact shared briefing sent once at the start of the pipeline."""
    single_file_mode = "_single_file" in videos
    ref = (
        {"mode": "single_file_multi_angle",
         "video_path": videos["_single_file"],
         "layout": videos.get("_layout", "AUTO")}
        if single_file_mode else dict(videos)
    )
    payload = {
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "mesh": _mesh_summary(mesh),
        "camera_setup": cameras,
        "reference_videos": ref,
        "frame_range": {
            "start": props.frame_start,
            "end": props.frame_end,
            "fps": context.scene.render.fps / context.scene.render.fps_base,
        },
    }
    cam_lines = "\n".join(f"  {v}: {n}" for v, n in cameras.items())
    vid_lines = (
        f"  Single file: {videos['_single_file']}  layout={videos.get('_layout','AUTO')}"
        if single_file_mode else
        "\n".join(f"  {v}: {p}" for v, p in videos.items())
    )
    protected_line = (
        f"  Protected: '{props.user_camera.strip()}' — never delete.\n"
        if props.user_camera.strip() else "  No protected camera.\n"
    )
    return (
        "VMMCP PIPELINE — SHARED CONTEXT\n"
        "================================\n"
        "You are Claude Code connected to Blender via BlenderMCP.\n"
        "Execute autonomously. Do NOT ask the user for input.\n\n"
        "CAMERAS (ortho, passepartout 1.0):\n" + cam_lines + "\n\n"
        "REFERENCE VIDEOS:\n" + vid_lines + "\n\n"
        "CAMERA RULES:\n"
        "  Never delete VMMCP_* cameras.\n" + protected_line +
        "  Any other camera may be deleted if it clutters the scene.\n\n"
        "HARD CONSTRAINTS (apply to every step):\n"
        "  - Never rename / delete / reparent existing bones\n"
        "  - Never create a new armature if one already exists\n"
        "  - Never apply rotations to UNMAPPED bones\n"
        "  - Bone lengths must stay constant across all frames\n"
        "  - Set scene FPS to match motion data FPS before keyframing\n"
        "  - Every provided viewpoint must be processed — no skipping\n\n"
        f"SCENE PAYLOAD:\n{json.dumps(payload, indent=2)}\n"
    )


def _build_steps(videos, props):
    """Return list of (marker, label, step_text) — one entry per pipeline step."""
    single_file_mode = "_single_file" in videos
    layout = videos.get("_layout", "AUTO") if single_file_mode else "N/A"

    def wrap(marker, label, body):
        return (marker, label,
            f"=== {label} ===\n"
            "Execute ONLY this step. Do NOT start any other step.\n\n"
            + body +
            f"\nWhen this step is FULLY COMPLETE, you MUST execute this command "
            f"via BlenderMCP as the very last action:\n"
            f"  bpy.context.scene['vmmcp_step_done'] = '{marker}'\n"
            f"This signals the addon that you are done. Do NOT run this command "
            f"until the step is truly finished.\n"
        )

    steps = []

    steps.append(wrap("STEP_0_OK", "Step 0 — Camera Framing",
        "The 6 VMMCP cameras are placed at 1m from center as a starting point.\n"
        "Your job is to adjust them so the ENTIRE mesh is visible from each angle.\n\n"
        "For EACH VMMCP camera:\n"
        "  a) Switch to the camera view\n"
        "  b) Move the camera BACK along its axis until the full mesh is visible\n"
        "     with at least 20% margin on all sides\n"
        "  c) Increase ortho_scale to fit: use mesh bbox diagonal * 2.0 as minimum\n"
        "  d) Center the camera on the mesh bbox center\n"
        "  e) Confirm: head, feet, arms, and all extremities are inside the frame\n"
        "  f) Do NOT change camera.data.type away from 'ORTHO'\n"
        "  g) Do NOT skip any camera — all 6 must be verified\n\n"
        "The cameras must show the mesh FULLY DEZOOMED — it's better to have too\n"
        "much margin than to clip any part of the mesh.\n"
    ))

    if single_file_mode:
        steps.append(wrap("STEP_0b_OK", "Step 0b — Video Splitting",
            f"The reference video contains multiple angles (layout: {layout}).\n"
            "  a) If AUTO: inspect first frame to detect grid / sequential structure\n"
            "  b) Crop each angle: ffmpeg -i <in> -vf 'crop=W:H:X:Y' /tmp/vmmcp_angle_N.mp4\n"
            "  c) Each crop = one independent viewpoint — no cross-mixing\n"
            "  d) Label each crop by direction (front/back/left/right/top/bottom)\n"
            "  e) List the resulting files with their labels before finishing\n"
        ))

    steps.append(wrap("STEP_1_OK", "Step 1 — Motion Extraction",
        "PRIMARY: ~/mp_env/bin/python estimator/run_mediapipe_ik.py\n"
        "  --video <path> --out <path>.npz    (run for EACH video / crop)\n"
        "COORDINATE CONVERSION (mandatory before any IK):\n"
        "  MediaPipe X=right Y=down Z=away → Blender: blender = (-mp.x, mp.z, -mp.y)\n"
        "FALLBACK if MediaPipe fails: ~/hmr2_env via run_4dhumans.py\n"
        "  WARNING: HMR2 on Apple MPS gives near-static poses on large movements\n"
        "  — verify output variance (expect std > 0.5° per joint) before trusting.\n"
        "VISUAL AIDS: grids / markers / strong lines on character → hard constraints.\n"
        "Report: list of .npz files and their frame counts.\n"
    ))

    steps.append(wrap("STEP_2a_OK", "Step 2a — Bone Survey",
        "List EVERY bone in every armature in the scene. Per bone:\n"
        "  - Exact name (case-sensitive)\n"
        "  - Parent bone name (or ROOT)\n"
        "  - Head world position (x y z, 3 decimals)\n"
        "  - Has vertex weights on the mesh: yes / no\n"
        "Print the full list before finishing.\n"
        "RULE: do not rename / delete / reparent / modify any bone.\n"
        "      Do not create a new armature if one already exists.\n"
    ))

    steps.append(wrap("STEP_2b_OK", "Step 2b — SMPL→Rig Mapping",
        "Using the bone list from Step 2a, match each SMPL joint to a rig bone.\n"
        "SMPL joints: pelvis, left_hip, right_hip, spine1, left_knee, right_knee,\n"
        "  spine2, left_ankle, right_ankle, spine3, left_foot, right_foot, neck,\n"
        "  left_collar, right_collar, head, left_shoulder, right_shoulder,\n"
        "  left_elbow, right_elbow, left_wrist, right_wrist, left_hand, right_hand\n"
        "Matching order (stop at first hit):\n"
        "  1. Exact name (case-insensitive, strip DEF-/ORG-/MCH- prefixes)\n"
        "  2. Partial name (thigh.L→left_hip, forearm.R→right_elbow, spine→spineN…)\n"
        "  3. Positional proximity to anatomical position on mesh\n"
        "  4. UNMAPPED — if no match found (do NOT invent a bone)\n"
        "Output the full smpl_to_rig dict explicitly. Mark UNMAPPEDs clearly.\n"
    ))

    steps.append(wrap("STEP_2c_OK", "Step 2c — Root Motion Bone",
        "Find the bone that controls global body position in world space.\n"
        "Check in order:\n"
        "  1. Bone with no parent\n"
        "  2. Named: root, master, COG, center_of_gravity, hips, pelvis, torso\n"
        "  3. Topmost non-helper bone in hierarchy\n"
        "  4. Fallback: pelvis-mapped bone from Step 2b\n"
        "Output: ROOT_MOTION_BONE: <exact bone name>\n"
        "This bone receives smpl_trans location keyframes every frame (jumps, runs…).\n"
    ))

    steps.append(wrap("STEP_3_OK", "Step 3 — Animation Transfer",
        "Use smpl_to_rig (Step 2b) and root_motion_bone (Step 2c).\n"
        "Skip UNMAPPED joints — do NOT approximate with a wrong bone.\n"
        "Front/back .npz = base; fuse lateral/top/bottom for ambiguous joints.\n\n"
        "BEFORE frame loop (mandatory, build once):\n"
        "  bpy.ops.object.mode_set(mode='EDIT')\n"
        "  rest_offsets = {b.name: b.matrix.to_quaternion()\n"
        "                  for b in armature.data.edit_bones}\n"
        "  bpy.ops.object.mode_set(mode='OBJECT')\n"
        "  rest_inv = {n: q.conjugated() for n, q in rest_offsets.items()}\n\n"
        "PER FRAME, PER MAPPED JOINT:\n"
        "  a) aa = smpl_poses[f, joint*3 : joint*3+3]\n"
        "  b) q  = Quaternion(Rotation.from_rotvec(aa).as_quat()[[3,0,1,2]])\n"
        "  c) q_bl = Quaternion((q.w, -q.x, q.z, -q.y))   # MediaPipe→Blender\n"
        "     or  = Quaternion((q.w,  q.x, q.z, -q.y))   # HMR2/SMPL→Blender\n"
        "  d) rig = smpl_to_rig[joint]\n"
        "     final = rest_inv[rig] @ q_bl @ rest_offsets[rig]\n"
        "  e) pb.rotation_mode = 'QUATERNION'\n"
        "     pb.rotation_quaternion = final\n"
        "     pb.keyframe_insert('rotation_quaternion', frame=f)\n\n"
        "ROOT MOTION every frame:\n"
        "  t = smpl_trans[f]\n"
        "  root_pb.location = Vector((-t[0], t[2], -t[1]))  # MediaPipe\n"
        "  root_pb.keyframe_insert('location', frame=f)\n"
        "Report: bones animated, total keyframes, frames processed.\n"
    ))

    steps.append(wrap("STEP_4_OK", "Step 4 — Temporal Smoothing",
        "Smooth ALL rotation curves with quaternion SLERP or log-quat filter.\n"
        "NEVER smooth Euler angles (gimbal lock).\n"
        "Apply 3-frame moving average if jitter remains.\n"
        "Report: curves smoothed, peak jitter before / after.\n"
    ))

    steps.append(wrap("STEP_5_OK", "Step 5 — Foot Contact Correction",
        "Fix foot skating:\n"
        "  - Detect frames where feet are planted (low velocity + low height)\n"
        "  - Activate IK on ankle bones for those frame ranges\n"
        "  - Pin foot to ground plane during contact\n"
        "Report: contact frame ranges detected, IK ranges applied.\n"
    ))

    steps.append(wrap("STEP_6_OK", "Step 6 — Multi-Angle Verification",
        "MANDATORY for every viewpoint:\n"
        "  a) Switch to the corresponding VMMCP camera\n"
        "  b) Check every frame: limb penetration, impossible angles, floating feet,\n"
        "     asymmetry, positions contradicted by this viewpoint,\n"
        "     mismatch vs grid / markers / strong lines in the footage\n"
        "  c) If contradiction found: fix those keyframes immediately\n"
        "  d) After fix: re-verify from ALL other viewpoints\n"
        "Loop until every viewpoint passes. Do NOT declare done before that.\n"
        "Report: viewpoints checked, issues found and fixed per viewpoint.\n"
    ))

    return steps


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
# Pipeline operators
# ------------------------------------------------------------------
def _validate_scene(context, props):
    """Shared validation — returns (mesh, videos) or raises."""
    if props.mesh_source == "SMPL":
        model_path = bpy.path.abspath(props.smpl_model_path)
        if not model_path or not os.path.isfile(model_path):
            raise ValueError("SMPL model file not found — set a valid path.")
        mesh = _import_smpl_mesh(context, model_path, props.smpl_gender)
        if mesh is None:
            raise ValueError("Failed to import SMPL mesh (.pkl / .obj / .npz).")
        props.mesh_object = mesh.name
    else:
        mesh = _target_mesh(context, props)
        if mesh is None:
            raise ValueError("Select a mesh object first.")
    videos = _video_sources(props)
    if not videos:
        msg = ("Set a valid multi-angle video file."
               if props.video_mode == "SINGLE_FILE"
               else "Add at least one reference video.")
        raise ValueError(msg)
    return mesh, videos


class VMMCP_OT_generate_steps(Operator):
    """Write per-step prompt files to VMMCP_Steps/ without running them."""
    bl_idname = "video_mocap.generate_steps"
    bl_label = "Generate Step Files"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        try:
            mesh, videos = _validate_scene(context, props)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props)
        ctx_text = _build_context(context, mesh, cameras, videos, props)
        steps = _build_steps(videos, props)

        base = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else bpy.app.tempdir
        step_dir = os.path.join(base, "VMMCP_Steps")
        os.makedirs(step_dir, exist_ok=True)

        ctx_path = os.path.join(step_dir, "VMMCP_Context.txt")
        with open(ctx_path, "w", encoding="utf-8") as f:
            f.write(ctx_text)

        for marker, label, step_text in steps:
            fname = marker.replace(":", "").replace("_OK", "") + ".txt"
            with open(os.path.join(step_dir, fname), "w", encoding="utf-8") as f:
                f.write(ctx_text + "\n\n" + step_text)

        self.report({"INFO"}, f"{len(steps)} step files written to {step_dir}")
        return {"FINISHED"}


class VMMCP_OT_run_pipeline(Operator):
    """Send the first step to Claude Code app. Then use 'Next Step' for each following step."""
    bl_idname = "video_mocap.run_pipeline"
    bl_label = "Start Pipeline"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp

        try:
            mesh, videos = _validate_scene(context, props)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props)
        ctx_text = _build_context(context, mesh, cameras, videos, props)
        steps = _build_steps(videos, props)

        with _pipe_lock:
            _pipe["step_idx"] = 0
            _pipe["steps"] = steps
            _pipe["history"] = ctx_text
            _pipe["status"] = ""
            _pipe["error"] = ""
            _pipe["thread"] = None
            _pipe["scene_name"] = context.scene.name

        # Clear old step files and marker
        _clear_steps_dir()
        context.scene["vmmcp_step_done"] = ""

        # Write ALL step files at once
        steps = _pipe["steps"]
        steps_dir = _steps_dir()
        for i, (marker, label, step_text) in enumerate(steps):
            full_prompt = _pipe["history"] + "\n\n" + step_text
            _write_step_file(i, marker, label, full_prompt)

        # Point to step 0
        marker, label, step_text = steps[0]
        first_path = _write_step_file(0, marker, label, _pipe["history"] + "\n\n" + step_text)
        _write_current_step_pointer(0, first_path)

        _pipe["step_idx"] = 1
        props.pipeline_running = len(steps) > 1

        # Launch a fresh Claude Code terminal session
        try:
            _launch_claude_terminal(steps_dir, first_path)
            props.pipeline_status = f"Step 1/{len(steps)}: {label} — Claude Code launched"
            self.report({"INFO"}, f"Claude Code terminal opened with {len(steps)} steps")
        except Exception as exc:
            # Fallback: copy to clipboard if terminal launch fails
            instruction = f"Read and execute the step in {first_path}"
            context.window_manager.clipboard = instruction
            props.pipeline_status = f"Step 1/{len(steps)}: {label} — paste clipboard in Claude Code"
            self.report({"WARNING"}, f"Terminal launch failed ({exc}), instruction copied to clipboard")

        return {"FINISHED"}


class VMMCP_OT_next_step(Operator):
    """Send the next step to Claude Code (only if previous step is confirmed done)."""
    bl_idname = "video_mocap.next_step"
    bl_label = "Next Step"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        """Only allow clicking if the previous step's marker is confirmed in the scene."""
        steps = _pipe.get("steps", [])
        idx = _pipe.get("step_idx", 0)
        if not steps or idx <= 0 or idx > len(steps):
            return False
        # Check that the PREVIOUS step's marker exists in the scene
        prev_marker = steps[idx - 1][0]
        done_marker = context.scene.get("vmmcp_step_done", "")
        return done_marker == prev_marker

    def execute(self, context):
        props = context.scene.vmmcp
        idx = _pipe["step_idx"]
        steps = _pipe["steps"]

        if idx >= len(steps):
            props.pipeline_status = f"All {len(steps)} steps done!"
            props.pipeline_running = False
            return {"FINISHED"}

        marker, label, step_text = steps[idx]

        # Clear the marker before pointing to the new step
        context.scene["vmmcp_step_done"] = ""

        # Update CURRENT_STEP.md to point to this step
        step_path = os.path.join(_steps_dir(),
                                 f"step_{idx:02d}_{marker.replace('_OK', '')}.md")
        _write_current_step_pointer(idx, step_path)

        # Copy instruction to clipboard — user pastes in the existing Claude terminal
        instruction = f"Read and execute the step in {step_path}"
        context.window_manager.clipboard = instruction

        _pipe["step_idx"] = idx + 1
        remaining = len(steps) - _pipe["step_idx"]
        props.pipeline_status = f"Step {idx + 1}/{len(steps)}: {label} — paste in Claude terminal"
        props.pipeline_running = remaining > 0
        self.report({"INFO"}, f"Step ready: {label} — Cmd+V in Claude Code terminal")
        return {"FINISHED"}


class VMMCP_OT_stop_pipeline(Operator):
    """Abort the running pipeline."""
    bl_idname = "video_mocap.stop_pipeline"
    bl_label = "Stop Pipeline"
    bl_options = {"REGISTER"}

    def execute(self, context):
        with _pipe_lock:
            _pipe["running"] = False
            _pipe["error"] = "Stopped by user."
        context.scene.vmmcp.pipeline_running = False
        context.scene.vmmcp.pipeline_status = "Stopped."
        return {"FINISHED"}


class VMMCP_OT_reset_pipeline(Operator):
    """Reset pipeline to Step 0 and clear all animation/keyframes in Blender."""
    bl_idname = "video_mocap.reset_pipeline"
    bl_label = "Reset Pipeline"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp

        # Reset pipeline state
        with _pipe_lock:
            _pipe["running"] = False
            _pipe["step_idx"] = 0
            _pipe["steps"] = []
            _pipe["history"] = ""
            _pipe["status"] = ""
            _pipe["error"] = ""
            _pipe["thread"] = None

        # Clear step marker
        context.scene["vmmcp_step_done"] = ""
        props.pipeline_running = False
        props.pipeline_status = "Reset — ready to start"

        # Clear all animation data on armatures linked to the mesh
        mesh_obj = bpy.data.objects.get(props.mesh_object)
        if mesh_obj:
            for mod in mesh_obj.modifiers:
                if mod.type == "ARMATURE" and mod.object:
                    arm = mod.object
                    if arm.animation_data and arm.animation_data.action:
                        bpy.data.actions.remove(arm.animation_data.action)
                        self.report({"INFO"}, f"Cleared animation on {arm.name}")

        # Clear step files and write reset notice
        _clear_steps_dir()
        d = _steps_dir()
        reset_path = os.path.join(d, "RESET.md")
        with open(reset_path, "w", encoding="utf-8") as f:
            f.write("# PIPELINE RESET\n\n")
            f.write("The user has reset the motion capture pipeline.\n")
            f.write("All previous steps are cancelled. Animation data has been cleared.\n")
            f.write("Forget all previous VMMCP step context.\n")
            f.write("Wait for the next step prompt — do not take any action now.\n")
            f.write("Confirm by executing via BlenderMCP:\n")
            f.write("  bpy.context.scene['vmmcp_step_done'] = 'RESET_OK'\n")

        context.window_manager.clipboard = f"Read and execute {reset_path}"
        self.report({"INFO"}, f"Pipeline reset — tell Claude to read {reset_path}")

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

        # Manual mode
        box = layout.box()
        box.label(text="Manual Mode", icon="COPYDOWN")
        box.operator("video_mocap.generate", icon="COPYDOWN", text="Generate Prompt")
        box.label(text="Paste in Claude Code + BlenderMCP", icon="INFO")
        box.operator("video_mocap.generate_steps", icon="FILE_TEXT",
                     text="Generate Step Files Only")

        layout.separator()

        # Pipeline — file-based steps
        box = layout.box()
        box.label(text="Pipeline", icon="PLAY")

        steps = _pipe.get("steps", [])
        idx = _pipe.get("step_idx", 0)

        if props.pipeline_status:
            err = "error" in props.pipeline_status.lower()
            done = "all" in props.pipeline_status.lower() and "done" in props.pipeline_status.lower()
            icon = "ERROR" if err else ("CHECKMARK" if done else "INFO")
            box.label(text=props.pipeline_status, icon=icon)

        if steps and idx > 0 and idx <= len(steps):
            prev_marker = steps[idx - 1][0]
            scene_marker = context.scene.get("vmmcp_step_done", "")
            if scene_marker == prev_marker:
                box.label(text="Claude finished — ready for next step", icon="CHECKMARK")
            else:
                box.label(text="Waiting for Claude to finish...", icon="TIME")

        if not steps or idx >= len(steps):
            box.operator("video_mocap.run_pipeline", icon="PLAY",
                         text="Generate Steps")
        if props.pipeline_running and idx < len(steps):
            row = box.row()
            row.enabled = VMMCP_OT_next_step.poll(context)
            row.operator("video_mocap.next_step", icon="FORWARD",
                         text=f"Next Step ({idx + 1}/{len(steps)}) →")

        # Show steps folder path
        if steps:
            box.label(text=f"Steps folder: VMMCP_Steps/", icon="FILE_FOLDER")
            box.label(text="Paste clipboard in Claude Code", icon="INFO")
            box.separator()
            box.operator("video_mocap.reset_pipeline", icon="FILE_REFRESH",
                         text="Reset (back to Step 0)")

        # Estimator info
        layout.separator()
        info = layout.box()
        info.label(text="Primary: MediaPipe IK  (~/mp_env)", icon="ARMATURE_DATA")
        info.label(text="Fallback: HMR2  (~/hmr2_env, MPS)")
        info.label(text="Output: .npz — 24 joints, axis-angle")


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------
classes = (
    VMMCP_Props,
    VMMCP_OT_generate,
    VMMCP_OT_generate_steps,
    VMMCP_OT_run_pipeline,
    VMMCP_OT_next_step,
    VMMCP_OT_stop_pipeline,
    VMMCP_OT_reset_pipeline,
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
