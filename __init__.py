bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 2, 2),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Prepare mesh rigging and animation requests for Claude via BlenderMCP.",
    "category": "Animation",
}

import json
import os

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, StringProperty
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
}


def _update_rig_preset(self, _context):
    preset = RIG_PRESET_GUIDE.get(self.rig_preset)
    if preset:
        self.requested_bone_count = preset["default_bones"]


class VMMCP_Props(PropertyGroup):
    mesh_object: StringProperty(
        name="Mesh",
        description="Mesh object Claude must rig and animate",
        default="",
    )
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
    frame_start: IntProperty(name="Start", default=1, min=1)
    frame_end: IntProperty(name="End", default=250, min=1)
    rig_preset: EnumProperty(
        name="Rig Target",
        description="Target rig style Claude should use when creating the armature",
        items=[
            ("CUSTOM", "Custom", "General-purpose rig driven by the requested bone count"),
            ("RIGIFY", "Rigify", "Blender/Rigify-style rig with animator controls"),
            ("UNREAL", "Unreal", "Game-engine-friendly humanoid skeleton for Unreal export"),
        ],
        default="CUSTOM",
        update=_update_rig_preset,
    )
    requested_bone_count: IntProperty(
        name="Bones",
        description="Target number of bones Claude should create for the armature",
        default=65,
        min=8,
        max=512,
    )
    camera_distance: FloatProperty(
        name="Camera Distance",
        description="Multiplier based on the mesh bounding box size",
        default=2.4,
        min=0.5,
        max=20.0,
    )
    create_camera_setup: BoolProperty(
        name="Create/Update 6 Cameras",
        description="Create top, bottom, front, back, left and right cameras around the mesh",
        default=True,
    )
    request_text_name: StringProperty(
        name="Last Request",
        description="Name of the Blender text block containing the latest MCP request",
        default="",
    )
    request_txt_path: StringProperty(
        name="Request TXT",
        description="Path where the latest MCP request is exported as a .txt file",
        subtype="FILE_PATH",
        default="",
    )


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


def _existing_camera_map():
    cameras = [obj for obj in bpy.data.objects if obj.type == "CAMERA"]
    if not cameras:
        return {}

    unused = sorted(cameras, key=lambda obj: obj.name.lower())
    mapped = {}
    for view_name, _axis in CAMERA_SPECS:
        named = next((cam for cam in unused if view_name in cam.name.lower()), None)
        if named is not None:
            mapped[view_name] = named
            unused.remove(named)

    for view_name, _axis in CAMERA_SPECS:
        if view_name in mapped or not unused:
            continue
        mapped[view_name] = unused.pop(0)

    return mapped


def _setup_cameras(context, mesh, props):
    center, _min_v, _max_v, _size, radius = _world_bbox(mesh)
    distance = radius * props.camera_distance
    cameras = {}
    existing = _existing_camera_map()

    for view_name, axis in CAMERA_SPECS:
        cam_obj = existing.get(view_name)
        if cam_obj is None and not existing:
            cam_name = f"VMMCP_{view_name.upper()}_Camera"
            cam_obj = bpy.data.objects.get(cam_name)
            if cam_obj is not None and cam_obj.type != "CAMERA":
                continue
            if cam_obj is None:
                cam_data = bpy.data.cameras.new(cam_name)
                cam_obj = bpy.data.objects.new(cam_name, cam_data)
                context.collection.objects.link(cam_obj)
        elif cam_obj is None:
            continue

        if cam_obj.type != "CAMERA":
            continue

        cam_obj.location = center + axis.normalized() * distance
        _look_at(cam_obj, center)
        cam_obj.data.lens = 50
        cam_obj.data.clip_end = max(distance * 10.0, 1000.0)
        cam_obj.data.type = "ORTHO"
        cam_obj.data.ortho_scale = max(radius * 2.4, 0.1)
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
        "best_practice_ranges": {
            "simple_prop_or_rigid_object": "8-25 bones",
            "game_humanoid_unreal": "50-70 deformation bones",
            "rigify_humanoid_controls": "80-120 total bones including controls",
            "creature_or_complex_character": "90-180 bones depending on limbs, face and appendages",
            "facial_or_hand_heavy_character": "120+ bones or combine bones with shape keys",
        },
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


def _base_payload(context, mesh, props, cameras):
    return {
        "addon": "video_mocap_mcp",
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "mesh": _mesh_summary(mesh),
        "camera_setup": cameras,
        "camera_policy": {
            "reuse_existing_cameras": True,
            "create_cameras_only_when_scene_has_none": True,
            "framing_requirement": "Each listed camera must be adjusted to see the entire mesh, including all animated limbs, across the full frame range.",
        },
        "media_sources": _media_sources(props),
        "rig_settings": _rig_settings(props),
        "frame_range": {
            "start": props.frame_start,
            "end": props.frame_end,
            "fps": context.scene.render.fps / context.scene.render.fps_base,
        },
    }


def _request_text(kind, payload):
    if kind == "rig":
        task = (
            "Use BlenderMCP to inspect the mesh and the listed scene cameras "
            "(top, bottom, front, back, left, right when available). Create an armature that "
            "matches the mesh anatomy/topology, place bones inside the mesh, "
            "honor the requested rig target and approximate bone count, "
            "parent/deform the mesh to that armature, create usable IK/FK "
            "controls where appropriate, and leave the rig ready for animation."
        )
    else:
        task = (
            "Use BlenderMCP to inspect the supplied videos or image sequence "
            "from the six reference views. Animate the existing rig created for "
            "the mesh so the bones follow the performance. Bake clean keyframes "
            "on the rig over the requested frame range and keep the mesh bound "
            "to those same bones. Ignore every object, person, prop, background "
            "element or motion cue that is not present in the supplied references."
        )

    return (
        "PROMPT TO PASTE IN A NEW CLAUDE CONVERSATION\n"
        "===========================================\n\n"
        "You are connected to Blender through BlenderMCP. If the BlenderMCP tools "
        "are not available in this conversation, stop and ask the user to reopen "
        "the conversation with BlenderMCP enabled before doing any rigging or "
        "animation work.\n\n"
        "Use the payload below as the source of truth for the Blender scene, mesh, "
        "camera setup, media references and production constraints. Execute the "
        "work in Blender through BlenderMCP, not as a generic text-only answer.\n\n"
        "MCP request for Claude / BlenderMCP\n"
        "===================================\n\n"
        f"Task: {task}\n\n"
        "Important constraints:\n"
        "- Do not generate a separate MediaPipe skeleton.\n"
        "- The final animation must be applied to the mesh rig bones.\n"
        "- Follow the rig_settings payload for target platform and bone count.\n"
        "- Use the existing Blender scene as source of truth.\n"
        "- Use the camera objects listed in the payload as analysis views.\n\n"
        "Camera setup requirement:\n"
        "- Do not add extra cameras if camera_setup already lists scene cameras.\n"
        "- Reuse and adjust the listed cameras instead.\n"
        "- Move/rotate/set focal length or orthographic scale so every listed camera "
        "sees the entire target mesh, including limbs and root motion, for the full "
        "animation frame range.\n"
        "- If a camera cannot see the full mesh, fix the camera framing before using "
        "that angle for rigging or motion verification.\n\n"
        "Reference filtering:\n"
        "- Analyze motion only from the supplied videos or image sequence.\n"
        "- Ignore any object, character, prop, background element, lighting cue or "
        "movement not present in the provided references.\n"
        "- If the Blender scene contains extra objects that are not part of the "
        "target mesh, rig, cameras or supplied references, exclude them from the "
        "motion analysis.\n\n"
        "Per-angle validation requirement:\n"
        "- For every major pose keyframe, compare the animated Blender pose against "
        "each available reference angle: front, back, left, right, top and bottom.\n"
        "- Use the matching VMMCP_* camera view to check silhouette, limb direction, "
        "root motion, contact points and timing against that angle.\n"
        "- If a pose matches one angle but contradicts another available angle, "
        "adjust the rig until the pose is coherent across the reference views.\n"
        "- Do this comparison before considering the rigging or animation pass done.\n\n"
        "Payload JSON:\n"
        f"{json.dumps(payload, indent=2)}\n"
    )


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


class VMMCP_OT_rig_mesh(Operator):
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
        payload = _base_payload(context, mesh, props, cameras)
        content = _request_text("rig", payload)
        text = _write_text("VMMCP_Rig_Mesh_Request", content)
        props.request_text_name = text.name
        props.request_txt_path = ""
        context.window_manager.clipboard = content
        _export_request_to_txt(context, props)
        self.report({"INFO"}, "Rig Mesh MCP request copied, written to Text Editor and exported to txt.")
        return {"FINISHED"}


class VMMCP_OT_animate(Operator):
    bl_idname = "video_mocap.animate"
    bl_label = "Animate"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        props = context.scene.vmmcp
        mesh = _target_mesh(context, props)
        if mesh is None:
            self.report({"ERROR"}, "Select or choose a mesh object first.")
            return {"CANCELLED"}

        media = _media_sources(props)
        if not media:
            self.report({"ERROR"}, "Add at least one video or image sequence source.")
            return {"CANCELLED"}

        cameras = _setup_cameras(context, mesh, props) if props.create_camera_setup else {}
        payload = _base_payload(context, mesh, props, cameras)
        content = _request_text("animate", payload)
        text = _write_text("VMMCP_Animate_Request", content)
        props.request_text_name = text.name
        props.request_txt_path = ""
        context.window_manager.clipboard = content
        _export_request_to_txt(context, props)
        self.report({"INFO"}, "Animate MCP request copied, written to Text Editor and exported to txt.")
        return {"FINISHED"}


class VMMCP_OT_copy_request_to_txt(Operator):
    bl_idname = "video_mocap.copy_request_to_txt"
    bl_label = "Copy Request to txt"
    bl_options = {"REGISTER"}

    def execute(self, context):
        props = context.scene.vmmcp
        if not props.request_text_name:
            self.report({"ERROR"}, "No request generated yet. Use Rig Mesh or Animate first.")
            return {"CANCELLED"}

        out_path = _export_request_to_txt(context, props)
        if not out_path:
            self.report({"ERROR"}, f"Request text block not found: {props.request_text_name}")
            return {"CANCELLED"}

        self.report({"INFO"}, f"Request exported: {out_path}")
        return {"FINISHED"}


class VMMCP_PT_panel(Panel):
    bl_label = "Video Mocap MCP"
    bl_idname = "VMMCP_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Mocap"

    def draw(self, context):
        layout = self.layout
        props = context.scene.vmmcp

        box = layout.box()
        box.label(text="Mesh", icon="MESH_DATA")
        box.prop_search(props, "mesh_object", bpy.data, "objects")

        box = layout.box()
        box.label(text="Reference Media", icon="FILE_MOVIE")
        box.prop(props, "front_video")
        box.prop(props, "back_video")
        box.prop(props, "left_video")
        box.prop(props, "right_video")
        box.prop(props, "top_video")
        box.prop(props, "bottom_video")
        box.prop(props, "image_sequence_dir")

        box = layout.box()
        box.label(text="MCP Setup", icon="CAMERA_DATA")
        row = box.row(align=True)
        row.prop(props, "frame_start")
        row.prop(props, "frame_end")
        box.prop(props, "rig_preset")
        box.prop(props, "requested_bone_count")
        box.prop(props, "create_camera_setup")
        box.prop(props, "camera_distance")
        box.operator("video_mocap.setup_cameras", icon="CAMERA_DATA")

        guide = layout.box()
        guide.label(text="Bone Count Guide", icon="INFO")
        guide.label(text="Custom default: 65 bones")
        guide.label(text="Unreal humanoid: 50-70 deformation bones")
        guide.label(text="Rigify humanoid: 80-120 bones incl. controls")
        guide.label(text="Creature/complex: 90-180 depending anatomy")

        layout.separator()
        col = layout.column(align=True)
        col.operator("video_mocap.rig_mesh", icon="ARMATURE_DATA")
        col.operator("video_mocap.animate", icon="PLAY")
        if props.request_text_name:
            layout.label(text=f"Request: {props.request_text_name}", icon="TEXT")
            layout.prop(props, "request_txt_path")
            layout.operator("video_mocap.copy_request_to_txt", icon="FILE_TEXT")


classes = (
    VMMCP_Props,
    VMMCP_OT_setup_cameras,
    VMMCP_OT_rig_mesh,
    VMMCP_OT_animate,
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
