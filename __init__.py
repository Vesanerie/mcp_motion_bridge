bl_info = {
    "name": "Video Mocap MCP",
    "author": "You",
    "version": (0, 2, 0),
    "blender": (3, 6, 0),
    "location": "View3D > Sidebar > Mocap",
    "description": "Prepare mesh rigging and animation requests for Claude via BlenderMCP.",
    "category": "Animation",
}

import json

import bpy
from bpy.props import BoolProperty, FloatProperty, IntProperty, StringProperty
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


def _write_text(name, content):
    text = bpy.data.texts.get(name) or bpy.data.texts.new(name)
    text.clear()
    text.write(content)
    return text


def _base_payload(context, mesh, props, cameras):
    return {
        "addon": "video_mocap_mcp",
        "blend_file": bpy.data.filepath,
        "scene": context.scene.name,
        "mesh": _mesh_summary(mesh),
        "camera_setup": cameras,
        "media_sources": _media_sources(props),
        "frame_range": {
            "start": props.frame_start,
            "end": props.frame_end,
            "fps": context.scene.render.fps / context.scene.render.fps_base,
        },
    }


def _request_text(kind, payload):
    if kind == "rig":
        task = (
            "Use BlenderMCP to inspect the mesh and the six scene cameras "
            "(top, bottom, front, back, left, right). Create an armature that "
            "matches the mesh anatomy/topology, place bones inside the mesh, "
            "parent/deform the mesh to that armature, create usable IK/FK "
            "controls where appropriate, and leave the rig ready for animation."
        )
    else:
        task = (
            "Use BlenderMCP to inspect the supplied videos or image sequence "
            "from the six reference views. Animate the existing rig created for "
            "the mesh so the bones follow the performance. Bake clean keyframes "
            "on the rig over the requested frame range and keep the mesh bound "
            "to those same bones."
        )

    return (
        "MCP request for Claude / BlenderMCP\n"
        "===================================\n\n"
        f"Task: {task}\n\n"
        "Important constraints:\n"
        "- Do not generate a separate MediaPipe skeleton.\n"
        "- The final animation must be applied to the mesh rig bones.\n"
        "- Use the existing Blender scene as source of truth.\n"
        "- Use the camera objects listed in the payload as analysis views.\n\n"
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
        context.window_manager.clipboard = content
        self.report({"INFO"}, "Rig Mesh MCP request copied and written to Text Editor.")
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
        context.window_manager.clipboard = content
        self.report({"INFO"}, "Animate MCP request copied and written to Text Editor.")
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
        box.prop(props, "create_camera_setup")
        box.prop(props, "camera_distance")
        box.operator("video_mocap.setup_cameras", icon="CAMERA_DATA")

        layout.separator()
        col = layout.column(align=True)
        col.operator("video_mocap.rig_mesh", icon="ARMATURE_DATA")
        col.operator("video_mocap.animate", icon="PLAY")
        if props.request_text_name:
            layout.label(text=f"Request: {props.request_text_name}", icon="TEXT")


classes = (
    VMMCP_Props,
    VMMCP_OT_setup_cameras,
    VMMCP_OT_rig_mesh,
    VMMCP_OT_animate,
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
