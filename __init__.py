bl_info = {
    "name": "MCP_Motion_Bridge",
    "author": "You",
    "version": (1, 0, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Step-by-step motion capture pipeline for Claude Code via BlenderMCP.",
    "category": "Animation",
}

import json
import os
import shutil
import subprocess
import time

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
# Properties
# ------------------------------------------------------------------
class VMMCP_Props(PropertyGroup):
    mesh_source: EnumProperty(
        name="Mesh Source",
        items=[
            ("SCENE", "Scene Mesh", ""),
            ("SMPL", "SMPL Body", ""),
        ],
        default="SCENE",
    )
    mesh_object: StringProperty(name="Mesh", default="")
    smpl_model_path: StringProperty(name="SMPL Model", subtype="FILE_PATH", default="")
    smpl_gender: EnumProperty(
        name="Gender",
        items=[("neutral", "Neutral", ""), ("male", "Male", ""), ("female", "Female", "")],
        default="neutral",
    )
    video_mode: EnumProperty(
        name="Video Mode",
        items=[
            ("MULTI_FILE", "One File per View", ""),
            ("SINGLE_FILE", "Single File – Multi-Angle", ""),
        ],
        default="MULTI_FILE",
    )
    multi_angle_video: StringProperty(name="Multi-Angle Video", subtype="FILE_PATH", default="")
    video_layout: EnumProperty(
        name="Layout",
        items=[
            ("AUTO", "Auto-detect", ""),
            ("2x1", "2x1 side-by-side", ""),
            ("1x2", "1x2 stacked", ""),
            ("2x2", "2x2 grid", ""),
            ("3x2", "3x2 grid", ""),
            ("SEQUENTIAL", "Sequential", ""),
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
    user_camera: StringProperty(name="Protected Camera", default="")
    cleanup_cameras: bpy.props.BoolProperty(name="Delete Other Cameras", default=False)
    pipeline_status: bpy.props.StringProperty(name="Pipeline Status", default="")


# ------------------------------------------------------------------
# Helpers (unchanged)
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
    distance = 1.0
    ortho_scale = max(size.length * 2.0, 3.0)
    if props.cleanup_cameras:
        vmmcp_names = {f"VMMCP_{v.upper()}_Camera" for v, _ in CAMERA_SPECS}
        protected = props.user_camera.strip()
        for obj in [o for o in list(context.scene.objects)
                    if o.type == "CAMERA" and o.name not in vmmcp_names and o.name != protected]:
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
        cam_obj.data.sensor_fit = "AUTO"
        cam_obj.data.sensor_width = 36.0
        cam_obj.data.sensor_height = 36.0
        cameras[view_name] = cam_obj.name
    context.scene.render.resolution_x = 1024
    context.scene.render.resolution_y = 1024
    return cameras

def _mesh_summary(obj):
    mesh = obj.data
    center, min_v, max_v, size, _r = _world_bbox(obj)
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
        "modifiers": [{"name": m.name, "type": m.type} for m in obj.modifiers],
        "materials": [s.material.name for s in obj.material_slots if s.material],
        "shape_keys": [k.name for k in mesh.shape_keys.key_blocks] if mesh.shape_keys else [],
    }

def _video_sources(props):
    if props.video_mode == "SINGLE_FILE":
        path = _abspath(props.multi_angle_video)
        if path and os.path.isfile(path):
            return {"_single_file": path, "_layout": props.video_layout}
        return {}
    sources = {
        "front": _abspath(props.front_video), "back": _abspath(props.back_video),
        "left": _abspath(props.left_video), "right": _abspath(props.right_video),
        "top": _abspath(props.top_video), "bottom": _abspath(props.bottom_video),
    }
    return {k: v for k, v in sources.items() if v and os.path.isfile(v)}

def _import_smpl_mesh(context, model_path, gender):
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
        import pickle, numpy as np
        with open(model_path, "rb") as fh:
            model = pickle.load(fh, encoding="latin1")
        mesh_data = bpy.data.meshes.new(obj_name)
        mesh_data.from_pydata(model["v_template"].tolist(), [], model["f"].astype(int).tolist())
        mesh_data.update()
        obj = bpy.data.objects.new(obj_name, mesh_data)
        context.collection.objects.link(obj)
        return obj
    if ext == ".npz":
        import numpy as np
        data = np.load(model_path, allow_pickle=True)
        v = data["v_template"].tolist() if "v_template" in data else data["vertices"].tolist()
        f = data["f"].astype(int).tolist() if "f" in data else data["faces"].astype(int).tolist()
        mesh_data = bpy.data.meshes.new(obj_name)
        mesh_data.from_pydata(v, [], f)
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

def _validate_scene(context, props):
    if props.mesh_source == "SMPL":
        model_path = bpy.path.abspath(props.smpl_model_path)
        if not model_path or not os.path.isfile(model_path):
            raise ValueError("SMPL model file not found.")
        mesh = _import_smpl_mesh(context, model_path, props.smpl_gender)
        if mesh is None:
            raise ValueError("Failed to import SMPL mesh.")
        props.mesh_object = mesh.name
    else:
        mesh = _target_mesh(context, props)
        if mesh is None:
            raise ValueError("Select a mesh object first.")
    videos = _video_sources(props)
    if not videos:
        raise ValueError("Add at least one reference video.")
    return mesh, videos


# ------------------------------------------------------------------
# Pipeline — file-based, timer-driven, single path of execution
# ------------------------------------------------------------------

# Pipeline state — accessed ONLY from Blender main thread (timer + operators)
# No threading, no lock needed.
_pipe = {
    "running": False,
    "step_idx": 0,        # index of the NEXT step to send
    "steps": [],          # list of (marker, label, step_text)
    "context_text": "",   # shared context prepended to each step
    "scene_name": "",
    "last_sent_marker": "",  # marker of the step currently running in Claude
    "last_activity": 0.0,    # time.time() of last marker change (for stall detection)
}


def _steps_dir():
    base = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else bpy.app.tempdir
    d = os.path.join(base, "VMMCP_Steps")
    os.makedirs(d, exist_ok=True)
    return d


def _debug_log(msg):
    """Append a timestamped line to VMMCP_Steps/debug.log"""
    d = _steps_dir()
    log_path = os.path.join(d, "debug.log")
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"[VMMCP] {line.strip()}")  # also print to Blender console


def _clear_steps_dir():
    d = _steps_dir()
    for f in os.listdir(d):
        fp = os.path.join(d, f)
        if os.path.isfile(fp):
            os.unlink(fp)


def _write_step_file(step_idx, marker, label, full_prompt):
    d = _steps_dir()
    filename = f"step_{step_idx:02d}_{marker.replace('_OK', '')}.md"
    path = os.path.join(d, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {label}\n\n")
        f.write(full_prompt)
    return path


def _find_claude_cli():
    import sys
    candidates = [shutil.which("claude")]
    if sys.platform == "darwin":
        nvm_dir = os.path.expanduser("~/.nvm/versions/node")
        if os.path.isdir(nvm_dir):
            for v in sorted(os.listdir(nvm_dir), reverse=True):
                candidates.append(os.path.join(nvm_dir, v, "bin", "claude"))
        candidates.extend(["/usr/local/bin/claude", "/opt/homebrew/bin/claude"])
    elif sys.platform == "win32":
        candidates.extend([
            os.path.expandvars(r"%APPDATA%\npm\claude.cmd"),
            os.path.expandvars(r"%PROGRAMFILES%\nodejs\claude.cmd"),
        ])
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def _ensure_mcp_config(target_dir):
    mcp_path = os.path.join(target_dir, ".mcp.json")
    config = {"mcpServers": {"blender": {"command": "uvx", "args": ["blender-mcp"]}}}
    with open(mcp_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def _launch_claude_terminal(work_dir, first_msg):
    """Open a new terminal with claude CLI. Non-blocking."""
    import sys
    claude_path = _find_claude_cli()
    if not claude_path:
        raise RuntimeError("'claude' CLI not found.")
    _ensure_mcp_config(work_dir)

    # Escape single quotes in paths
    wd = work_dir.replace("'", "'\\''")
    cp = claude_path.replace("'", "'\\''")
    msg = first_msg.replace("'", "'\\''").replace('"', '\\"')

    if sys.platform == "darwin":
        script = (
            'tell application "Terminal"\n'
            '    activate\n'
            f'    do script "cd \\"{wd}\\" && \\"{cp}\\" \\"{msg}\\""\n'
            'end tell'
        )
        subprocess.Popen(["osascript", "-e", script])
    elif sys.platform == "win32":
        subprocess.Popen(
            f'start cmd /k "cd /d "{work_dir}" && "{claude_path}" "{first_msg}""',
            shell=True,
        )
    else:
        subprocess.Popen([
            "x-terminal-emulator", "-e",
            f'bash -c \'cd "{work_dir}" && "{claude_path}" "{first_msg}"\'',
        ])


def _send_msg_to_terminal(msg):
    """Send a message to the Claude terminal via clipboard paste. Non-blocking."""
    import sys
    _debug_log(f"SEND: platform={sys.platform}, msg_len={len(msg)}")
    if sys.platform == "darwin":
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(msg.encode("utf-8"))
        _debug_log(f"SEND: pbcopy done, returncode={proc.returncode}")
        script = (
            'tell application "Terminal"\n'
            '    activate\n'
            '    delay 0.8\n'
            '    tell application "System Events"\n'
            '        tell process "Terminal"\n'
            '            set frontmost to true\n'
            '            delay 0.3\n'
            '            keystroke "v" using command down\n'
            '            delay 0.5\n'
            '            keystroke return\n'
            '        end tell\n'
            '    end tell\n'
            'end tell'
        )
        proc = subprocess.Popen(["osascript", "-e", script])
        _debug_log(f"SEND: osascript launched, pid={proc.pid}")
    elif sys.platform == "win32":
        ps = (
            'Add-Type -AssemblyName System.Windows.Forms\n'
            f'[System.Windows.Forms.Clipboard]::SetText("{msg}")\n'
            'Start-Sleep -Milliseconds 300\n'
            '$wshell = New-Object -ComObject wscript.shell\n'
            '$wshell.AppActivate("Terminal")\n'
            'Start-Sleep -Milliseconds 500\n'
            '[System.Windows.Forms.SendKeys]::SendWait("^v")\n'
            'Start-Sleep -Milliseconds 300\n'
            '[System.Windows.Forms.SendKeys]::SendWait("{ENTER}")\n'
        )
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps])


def _pipeline_tick():
    """Blender timer — polls marker every 3 seconds, auto-advances steps."""
    if not _pipe["running"]:
        _debug_log("TICK: pipeline not running, stopping timer")
        return None

    scene = bpy.data.scenes.get(_pipe["scene_name"])
    if scene is None:
        _debug_log(f"TICK: scene '{_pipe['scene_name']}' not found, stopping")
        _pipe["running"] = False
        return None

    props = scene.vmmcp
    steps = _pipe["steps"]
    idx = _pipe["step_idx"]

    _debug_log(f"TICK: idx={idx}, total_steps={len(steps)}, "
               f"last_sent_marker='{_pipe['last_sent_marker']}', "
               f"scene_marker='{scene.get('vmmcp_step_done', '')}'")

    # All steps sent and confirmed?
    if idx > len(steps):
        _debug_log("TICK: all steps done, stopping")
        _pipe["running"] = False
        props.pipeline_status = f"Pipeline complete — {len(steps)} steps done!"
        return None

    # Waiting for the current step to finish?
    if _pipe["last_sent_marker"]:
        scene_marker = scene.get("vmmcp_step_done", "")
        if scene_marker != _pipe["last_sent_marker"]:
            elapsed = time.time() - _pipe["last_activity"]
            _debug_log(f"TICK: waiting for marker '{_pipe['last_sent_marker']}', "
                       f"scene has '{scene_marker}', elapsed={elapsed:.0f}s")
            props.pipeline_status = (
                f"Step {idx}/{len(steps)} — waiting for '{_pipe['last_sent_marker']}' "
                f"({elapsed:.0f}s)"
            )
            return 3.0
        else:
            _debug_log(f"TICK: marker '{_pipe['last_sent_marker']}' CONFIRMED!")
            _pipe["last_activity"] = time.time()
            _pipe["last_sent_marker"] = ""

    # Are there more steps?
    if idx >= len(steps):
        _debug_log("TICK: no more steps, completing")
        _pipe["step_idx"] = idx + 1
        _pipe["running"] = False
        props.pipeline_status = f"Pipeline complete — {len(steps)} steps done!"
        return None

    # --- SEND NEXT STEP ---
    marker, label, step_text = steps[idx]
    full_prompt = _pipe["context_text"] + "\n\n" + step_text

    step_path = _write_step_file(idx, marker, label, full_prompt)
    _debug_log(f"TICK: wrote step file {step_path}")

    scene["vmmcp_step_done"] = ""

    msg = f"Read and execute the prompt in {step_path}"
    _debug_log(f"TICK: sending to terminal: {msg}")
    _send_msg_to_terminal(msg)

    _pipe["step_idx"] = idx + 1
    _pipe["last_sent_marker"] = marker
    _pipe["last_activity"] = time.time()
    props.pipeline_status = f"Step {idx + 1}/{len(steps)}: {label} — running..."
    _debug_log(f"TICK: step {idx + 1} '{label}' sent, waiting for '{marker}'")

    return 3.0


# ------------------------------------------------------------------
# Step builders (unchanged content, cleaned wrap)
# ------------------------------------------------------------------
def _build_context(context, mesh, cameras, videos, props):
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
        "  MediaPipe X=right Y=down Z=away -> Blender: blender = (-mp.x, mp.z, -mp.y)\n"
        "FALLBACK if MediaPipe fails: ~/hmr2_env via run_4dhumans.py\n"
        "  WARNING: HMR2 on Apple MPS gives near-static poses on large movements\n"
        "  — verify output variance (expect std > 0.5 per joint) before trusting.\n"
        "VISUAL AIDS: grids / markers / strong lines on character -> hard constraints.\n"
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

    steps.append(wrap("STEP_2b_OK", "Step 2b — SMPL to Rig Mapping",
        "Using the bone list from Step 2a, match each SMPL joint to a rig bone.\n"
        "SMPL joints: pelvis, left_hip, right_hip, spine1, left_knee, right_knee,\n"
        "  spine2, left_ankle, right_ankle, spine3, left_foot, right_foot, neck,\n"
        "  left_collar, right_collar, head, left_shoulder, right_shoulder,\n"
        "  left_elbow, right_elbow, left_wrist, right_wrist, left_hand, right_hand\n"
        "Matching order (stop at first hit):\n"
        "  1. Exact name (case-insensitive, strip DEF-/ORG-/MCH- prefixes)\n"
        "  2. Partial name (thigh.L -> left_hip, forearm.R -> right_elbow)\n"
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
    ))

    steps.append(wrap("STEP_3_OK", "Step 3 — Animation Transfer",
        "Use smpl_to_rig (Step 2b) and root_motion_bone (Step 2c).\n"
        "Skip UNMAPPED joints — do NOT approximate with a wrong bone.\n\n"
        "BEFORE frame loop (mandatory, build once):\n"
        "  bpy.ops.object.mode_set(mode='EDIT')\n"
        "  rest_offsets = {b.name: b.matrix.to_quaternion()\n"
        "                  for b in armature.data.edit_bones}\n"
        "  bpy.ops.object.mode_set(mode='OBJECT')\n"
        "  rest_inv = {n: q.conjugated() for n, q in rest_offsets.items()}\n\n"
        "PER FRAME, PER MAPPED JOINT:\n"
        "  a) aa = smpl_poses[f, joint*3 : joint*3+3]\n"
        "  b) q  = Quaternion(Rotation.from_rotvec(aa).as_quat()[[3,0,1,2]])\n"
        "  c) q_bl = Quaternion((q.w, -q.x, q.z, -q.y))   # MediaPipe\n"
        "  d) final = rest_inv[rig] @ q_bl @ rest_offsets[rig]\n"
        "  e) pb.rotation_mode = 'QUATERNION'\n"
        "     pb.rotation_quaternion = final\n"
        "     pb.keyframe_insert('rotation_quaternion', frame=f)\n\n"
        "ROOT MOTION every frame:\n"
        "  t = smpl_trans[f]\n"
        "  root_pb.location = Vector((-t[0], t[2], -t[1]))\n"
        "  root_pb.keyframe_insert('location', frame=f)\n"
        "Report: bones animated, total keyframes.\n"
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
        "  b) Check every frame: limb penetration, impossible angles, floating feet\n"
        "  c) If contradiction found: fix those keyframes immediately\n"
        "  d) After fix: re-verify from ALL other viewpoints\n"
        "Loop until every viewpoint passes. Do NOT declare done before that.\n"
        "Report: viewpoints checked, issues found and fixed.\n"
    ))

    return steps


# ------------------------------------------------------------------
# The single prompt (for Copy Prompt Only)
# ------------------------------------------------------------------
def _build_prompt(context, mesh, cameras, videos, props):
    ctx = _build_context(context, mesh, cameras, videos, props)
    steps = _build_steps(videos, props)
    full = ctx + "\n\n"
    for _m, _l, step_text in steps:
        full += step_text + "\n\n"
    return full


# ------------------------------------------------------------------
# Operators
# ------------------------------------------------------------------
class VMMCP_OT_generate(Operator):
    """Copy the full prompt to clipboard."""
    bl_idname = "video_mocap.generate"
    bl_label = "Copy Prompt"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        try:
            mesh, videos = _validate_scene(context, props)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        cameras = _setup_cameras(context, mesh, props)
        prompt = _build_prompt(context, mesh, cameras, videos, props)
        _write_text("VMMCP_Prompt", prompt)
        context.window_manager.clipboard = prompt
        _export_txt(prompt, bpy.data.filepath)
        self.report({"INFO"}, "Prompt copied to clipboard")
        return {"FINISHED"}


class VMMCP_OT_launch(Operator):
    """Launch the pipeline: open Claude terminal + auto-advance through all steps."""
    bl_idname = "video_mocap.launch"
    bl_label = "Launch Pipeline"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp

        if _pipe["running"]:
            self.report({"WARNING"}, "Pipeline already running. Stop it first.")
            return {"CANCELLED"}

        try:
            mesh, videos = _validate_scene(context, props)
        except ValueError as exc:
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props)
        ctx_text = _build_context(context, mesh, cameras, videos, props)
        steps = _build_steps(videos, props)

        # Clear old files
        _clear_steps_dir()
        context.scene["vmmcp_step_done"] = ""
        steps_dir = _steps_dir()

        # Write first step
        marker, label, step_text = steps[0]
        first_path = _write_step_file(0, marker, label, ctx_text + "\n\n" + step_text)

        # Init pipeline state
        _pipe["running"] = True
        _pipe["step_idx"] = 1       # step 0 already sent
        _pipe["steps"] = steps
        _pipe["context_text"] = ctx_text
        _pipe["scene_name"] = context.scene.name
        _pipe["last_sent_marker"] = marker
        _pipe["last_activity"] = time.time()

        props.pipeline_status = f"Step 1/{len(steps)}: {label} — launching Claude..."

        # Launch terminal
        first_msg = f"Read and execute the prompt in {first_path}"
        _debug_log(f"LAUNCH: steps_dir={steps_dir}")
        _debug_log(f"LAUNCH: first_path={first_path}")
        _debug_log(f"LAUNCH: first_msg={first_msg}")
        _debug_log(f"LAUNCH: steps count={len(steps)}")
        _debug_log(f"LAUNCH: markers={[s[0] for s in steps]}")
        try:
            _launch_claude_terminal(steps_dir, first_msg)
            _debug_log("LAUNCH: terminal opened OK")
        except Exception as exc:
            _debug_log(f"LAUNCH: terminal FAILED: {exc}")
            context.window_manager.clipboard = first_msg
            props.pipeline_status = f"Terminal failed — paste clipboard in Claude Code"
            self.report({"WARNING"}, f"Terminal failed ({exc}), instruction in clipboard")

        # Start timer
        if not bpy.app.timers.is_registered(_pipeline_tick):
            bpy.app.timers.register(_pipeline_tick, first_interval=8.0)

        self.report({"INFO"}, f"Pipeline launched — {len(steps)} steps")
        return {"FINISHED"}


class VMMCP_OT_stop(Operator):
    """Stop the pipeline."""
    bl_idname = "video_mocap.stop"
    bl_label = "Stop"
    bl_options = {"REGISTER"}

    def execute(self, context):
        _pipe["running"] = False
        context.scene.vmmcp.pipeline_status = "Stopped."
        return {"FINISHED"}


class VMMCP_OT_reset(Operator):
    """Reset: clear pipeline state and animations."""
    bl_idname = "video_mocap.reset"
    bl_label = "Reset"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        _pipe["running"] = False
        _pipe["step_idx"] = 0
        _pipe["steps"] = []
        _pipe["context_text"] = ""
        _pipe["last_sent_marker"] = ""
        context.scene["vmmcp_step_done"] = ""
        props.pipeline_status = ""

        mesh_obj = bpy.data.objects.get(props.mesh_object)
        if mesh_obj:
            for mod in mesh_obj.modifiers:
                if mod.type == "ARMATURE" and mod.object:
                    arm = mod.object
                    if arm.animation_data and arm.animation_data.action:
                        bpy.data.actions.remove(arm.animation_data.action)

        _clear_steps_dir()
        self.report({"INFO"}, "Pipeline reset")
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
            for v in ("front", "back", "left", "right", "top", "bottom"):
                box.prop(props, f"{v}_video")

        # Settings
        box = layout.box()
        box.label(text="Settings", icon="PREFERENCES")
        row = box.row(align=True)
        row.prop(props, "frame_start")
        row.prop(props, "frame_end")
        box.prop(props, "cleanup_cameras")
        if props.cleanup_cameras:
            box.prop_search(props, "user_camera", bpy.data, "objects", icon="CAMERA_DATA")

        layout.separator()

        # Pipeline
        box = layout.box()
        box.label(text="Pipeline", icon="PLAY")

        if props.pipeline_status:
            is_err = "error" in props.pipeline_status.lower() or "failed" in props.pipeline_status.lower()
            is_done = "complete" in props.pipeline_status.lower()
            icon = "ERROR" if is_err else ("CHECKMARK" if is_done else "TIME")
            box.label(text=props.pipeline_status, icon=icon)

        if _pipe["running"]:
            box.operator("video_mocap.stop", icon="CANCEL")
        else:
            box.operator("video_mocap.launch", icon="PLAY", text="Launch Pipeline")

        box.separator()
        box.operator("video_mocap.generate", icon="COPYDOWN", text="Copy Prompt Only")
        box.operator("video_mocap.reset", icon="FILE_REFRESH", text="Reset")

        layout.separator()
        info = layout.box()
        info.label(text="Primary: MediaPipe IK  (~/mp_env)", icon="ARMATURE_DATA")
        info.label(text="Fallback: HMR2  (~/hmr2_env, MPS)")


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------
classes = (
    VMMCP_Props,
    VMMCP_OT_generate,
    VMMCP_OT_launch,
    VMMCP_OT_stop,
    VMMCP_OT_reset,
    VMMCP_PT_panel,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.vmmcp = bpy.props.PointerProperty(type=VMMCP_Props)

def unregister():
    # Stop timer if running
    if bpy.app.timers.is_registered(_pipeline_tick):
        bpy.app.timers.unregister(_pipeline_tick)
    _pipe["running"] = False
    if hasattr(bpy.types.Scene, "vmmcp"):
        del bpy.types.Scene.vmmcp
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
