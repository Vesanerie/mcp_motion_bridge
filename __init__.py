bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 1, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Video -> MediaPipe -> Rigify/Mixamo armature. Scriptable via BlenderMCP.",
    "category": "Animation",
}

import bpy
import json
import os
import sys
import subprocess
import tempfile

from bpy.props import StringProperty, BoolProperty, EnumProperty, FloatProperty, IntProperty
from bpy.types import Operator, Panel, PropertyGroup

from .mediapipe_skeleton import (
    MEDIAPIPE_BONES,
    MEDIAPIPE_PARENTS,
    create_mediapipe_armature,
    apply_landmarks_to_armature,
)
from .retarget import (
    RIGIFY_BONE_MAP,
    MIXAMO_BONE_MAP,
    retarget_animation,
)


# ------------------------------------------------------------------
# Properties
# ------------------------------------------------------------------
class VMMCP_Props(PropertyGroup):
    video_path: StringProperty(
        name="Video",
        description="Path to the reference video",
        subtype='FILE_PATH',
        default="",
    )
    landmarks_path: StringProperty(
        name="Landmarks JSON",
        description="Where the extractor writes landmarks (auto if empty)",
        subtype='FILE_PATH',
        default="",
    )
    python_exe: StringProperty(
        name="External Python",
        description="Python with mediapipe installed (leave empty to auto-detect)",
        subtype='FILE_PATH',
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
    target_rig: EnumProperty(
        name="Target rig",
        items=[
            ('NONE', "None (raw mediapipe armature only)", ""),
            ('RIGIFY', "Rigify", ""),
            ('MIXAMO', "Mixamo", ""),
        ],
        default='NONE',
    )
    target_armature: StringProperty(
        name="Target armature",
        description="Name of the target Rigify/Mixamo armature object",
        default="",
    )
    fps: FloatProperty(
        name="FPS",
        description="Frame rate of the source video (0 = read from JSON)",
        default=0.0, min=0.0,
    )


# ------------------------------------------------------------------
# Operator: extract landmarks from video (runs external python)
# ------------------------------------------------------------------
class VMMCP_OT_extract(Operator):
    """Run MediaPipe on the video and dump landmarks to JSON."""
    bl_idname = "video_mocap.extract"
    bl_label = "Extract landmarks"
    bl_options = {'REGISTER'}

    def execute(self, context):
        props = context.scene.vmmcp
        video = bpy.path.abspath(props.video_path)
        if not video or not os.path.isfile(video):
            self.report({'ERROR'}, f"Video not found: {video}")
            return {'CANCELLED'}

        # Default output next to the video
        out_json = bpy.path.abspath(props.landmarks_path) if props.landmarks_path else ""
        if not out_json:
            out_json = os.path.splitext(video)[0] + "_landmarks.json"
            props.landmarks_path = out_json

        py = bpy.path.abspath(props.python_exe) if props.python_exe else _auto_detect_python()
        if not py:
            self.report({'ERROR'}, "No external Python found. Set props.python_exe.")
            return {'CANCELLED'}

        script = os.path.join(os.path.dirname(__file__), "extractor", "extract_pose.py")
        cmd = [
            py, script,
            "--video", video,
            "--out", out_json,
            "--complexity", str(props.model_complexity),
            "--min-detection", str(props.min_detection_conf),
        ]
        if props.smooth_landmarks:
            cmd.append("--smooth")

        print("[video_mocap] running:", " ".join(cmd))
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 30)
        except Exception as e:
            self.report({'ERROR'}, f"Extractor failed to launch: {e}")
            return {'CANCELLED'}

        if proc.returncode != 0:
            print(proc.stdout)
            print(proc.stderr)
            self.report({'ERROR'}, f"Extractor failed (code {proc.returncode}). Check console.")
            return {'CANCELLED'}

        self.report({'INFO'}, f"Landmarks written: {out_json}")
        return {'FINISHED'}


def _auto_detect_python():
    """Try to find a python with mediapipe installed."""
    # Priority: env var, then common system pythons
    env = os.environ.get("VIDEO_MOCAP_PYTHON")
    if env and os.path.isfile(env):
        return env
    candidates = []
    if sys.platform == "win32":
        candidates = ["python", "python3"]
    else:
        candidates = ["python3", "python"]
    for c in candidates:
        try:
            r = subprocess.run(
                [c, "-c", "import mediapipe; print(mediapipe.__version__)"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return c
        except Exception:
            pass
    return ""


# ------------------------------------------------------------------
# Operator: build mediapipe armature from JSON
# ------------------------------------------------------------------
class VMMCP_OT_build_armature(Operator):
    """Read landmarks JSON and create/animate a MediaPipe armature."""
    bl_idname = "video_mocap.build_armature"
    bl_label = "Build MediaPipe armature"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vmmcp
        path = bpy.path.abspath(props.landmarks_path)
        if not path or not os.path.isfile(path):
            self.report({'ERROR'}, f"Landmarks JSON not found: {path}")
            return {'CANCELLED'}

        with open(path, "r") as f:
            data = json.load(f)

        fps = props.fps if props.fps > 0 else float(data.get("fps", 30.0))
        frames = data.get("frames", [])
        if not frames:
            self.report({'ERROR'}, "No frames in JSON.")
            return {'CANCELLED'}

        arm_obj = create_mediapipe_armature(name="MP_Armature")
        apply_landmarks_to_armature(arm_obj, frames, fps=fps, scene=context.scene)

        # Store name for later retargeting
        context.scene.vmmcp_source_armature = arm_obj.name
        self.report({'INFO'}, f"Armature '{arm_obj.name}' built ({len(frames)} frames @ {fps:.2f} fps).")
        return {'FINISHED'}


# ------------------------------------------------------------------
# Operator: retarget to Rigify / Mixamo
# ------------------------------------------------------------------
class VMMCP_OT_retarget(Operator):
    """Retarget MediaPipe armature animation to a Rigify/Mixamo armature."""
    bl_idname = "video_mocap.retarget"
    bl_label = "Retarget"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        props = context.scene.vmmcp
        if props.target_rig == 'NONE':
            self.report({'WARNING'}, "Target rig is NONE, nothing to retarget.")
            return {'CANCELLED'}

        src_name = getattr(context.scene, "vmmcp_source_armature", "")
        src = bpy.data.objects.get(src_name)
        if src is None:
            self.report({'ERROR'}, f"Source armature '{src_name}' not found. Build it first.")
            return {'CANCELLED'}

        tgt = bpy.data.objects.get(props.target_armature)
        if tgt is None or tgt.type != 'ARMATURE':
            self.report({'ERROR'}, f"Target armature '{props.target_armature}' not found.")
            return {'CANCELLED'}

        mapping = RIGIFY_BONE_MAP if props.target_rig == 'RIGIFY' else MIXAMO_BONE_MAP
        retarget_animation(src, tgt, mapping)
        self.report({'INFO'}, f"Retargeted {src.name} -> {tgt.name} ({props.target_rig}).")
        return {'FINISHED'}


# ------------------------------------------------------------------
# Operator: run the whole pipeline in one click
# ------------------------------------------------------------------
class VMMCP_OT_run_all(Operator):
    """Extract + build armature + (optional) retarget."""
    bl_idname = "video_mocap.run_all"
    bl_label = "Run full pipeline"
    bl_options = {'REGISTER'}

    def execute(self, context):
        if bpy.ops.video_mocap.extract() != {'FINISHED'}:
            return {'CANCELLED'}
        if bpy.ops.video_mocap.build_armature() != {'FINISHED'}:
            return {'CANCELLED'}
        if context.scene.vmmcp.target_rig != 'NONE':
            bpy.ops.video_mocap.retarget()
        return {'FINISHED'}


# ------------------------------------------------------------------
# Panel
# ------------------------------------------------------------------
class VMMCP_PT_panel(Panel):
    bl_label = "Video Mocap (MCP)"
    bl_idname = "VMMCP_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Mocap'

    def draw(self, context):
        layout = self.layout
        p = context.scene.vmmcp

        box = layout.box()
        box.label(text="Source", icon='FILE_MOVIE')
        box.prop(p, "video_path")
        box.prop(p, "landmarks_path")
        box.prop(p, "python_exe")
        box.prop(p, "fps")

        box = layout.box()
        box.label(text="MediaPipe", icon='ARMATURE_DATA')
        box.prop(p, "model_complexity")
        box.prop(p, "min_detection_conf")
        box.prop(p, "smooth_landmarks")

        box = layout.box()
        box.label(text="Retarget", icon='OUTLINER_OB_ARMATURE')
        box.prop(p, "target_rig")
        if p.target_rig != 'NONE':
            box.prop_search(p, "target_armature", bpy.data, "objects")

        layout.separator()
        col = layout.column(align=True)
        col.operator("video_mocap.extract", icon='PLAY')
        col.operator("video_mocap.build_armature", icon='ARMATURE_DATA')
        col.operator("video_mocap.retarget", icon='CON_ARMATURE')
        layout.separator()
        layout.operator("video_mocap.run_all", icon='SEQ_SEQUENCER')


# ------------------------------------------------------------------
# Register
# ------------------------------------------------------------------
classes = (
    VMMCP_Props,
    VMMCP_OT_extract,
    VMMCP_OT_build_armature,
    VMMCP_OT_retarget,
    VMMCP_OT_run_all,
    VMMCP_PT_panel,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.vmmcp = bpy.props.PointerProperty(type=VMMCP_Props)
    bpy.types.Scene.vmmcp_source_armature = bpy.props.StringProperty(default="")


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.vmmcp
    del bpy.types.Scene.vmmcp_source_armature


if __name__ == "__main__":
    register()
