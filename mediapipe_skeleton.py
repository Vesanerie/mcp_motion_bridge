"""
MediaPipe Pose landmark -> Blender armature helpers.

MediaPipe Pose gives 33 3D landmarks (world coordinates in meters when using
results.pose_world_landmarks). We build a simple armature whose bones connect
anatomically meaningful pairs of landmarks, then bake the animation by setting
each frame the head/tail of every bone in edit mode... no, that doesn't work
per-frame. Instead we attach an Empty per landmark, animate the empties'
locations, and the armature bones use Copy Location / Damped Track style
constraints, OR we directly keyframe the armature's bones in pose mode using
Matrix maths. The latter gives a clean, baked animation.

Implementation chosen: empties per landmark (for visualization & easy retarget)
+ an armature whose bones are rebuilt once in rest pose, then the pose bones
are rotated each frame using Damped Track style vector math so the bone points
from landmark[parent] to landmark[child]. Root bone (hips) position is also
keyframed so the character translates in world space.

Bone naming is MP_<LANDMARK_NAME> so retarget mapping is trivial.
"""

import bpy
import math
from mathutils import Vector, Matrix, Quaternion


# MediaPipe Pose landmark indices (official)
# https://ai.google.dev/edge/mediapipe/solutions/vision/pose_landmarker
MP = {
    "NOSE": 0,
    "LEFT_EYE_INNER": 1, "LEFT_EYE": 2, "LEFT_EYE_OUTER": 3,
    "RIGHT_EYE_INNER": 4, "RIGHT_EYE": 5, "RIGHT_EYE_OUTER": 6,
    "LEFT_EAR": 7, "RIGHT_EAR": 8,
    "MOUTH_LEFT": 9, "MOUTH_RIGHT": 10,
    "LEFT_SHOULDER": 11, "RIGHT_SHOULDER": 12,
    "LEFT_ELBOW": 13, "RIGHT_ELBOW": 14,
    "LEFT_WRIST": 15, "RIGHT_WRIST": 16,
    "LEFT_PINKY": 17, "RIGHT_PINKY": 18,
    "LEFT_INDEX": 19, "RIGHT_INDEX": 20,
    "LEFT_THUMB": 21, "RIGHT_THUMB": 22,
    "LEFT_HIP": 23, "RIGHT_HIP": 24,
    "LEFT_KNEE": 25, "RIGHT_KNEE": 26,
    "LEFT_ANKLE": 27, "RIGHT_ANKLE": 28,
    "LEFT_HEEL": 29, "RIGHT_HEEL": 30,
    "LEFT_FOOT_INDEX": 31, "RIGHT_FOOT_INDEX": 32,
}

# Anatomical bones: (bone_name, head_landmark_idx, tail_landmark_idx, parent_bone_name_or_None)
# The root bone "HIPS" is a virtual midpoint (LEFT_HIP + RIGHT_HIP) / 2.
MEDIAPIPE_BONES = [
    # Name,              head,               tail,               parent
    ("HIPS",             None,               None,               None),   # virtual root, computed
    ("SPINE",            "HIPS",             "CHEST",            "HIPS"),
    ("NECK",             "CHEST",            "HEAD",             "SPINE"),

    ("L_SHOULDER",       "CHEST",            "LEFT_SHOULDER",    "SPINE"),
    ("L_UPPERARM",       "LEFT_SHOULDER",    "LEFT_ELBOW",       "L_SHOULDER"),
    ("L_FOREARM",        "LEFT_ELBOW",       "LEFT_WRIST",       "L_UPPERARM"),
    ("L_HAND",           "LEFT_WRIST",       "LEFT_INDEX",       "L_FOREARM"),

    ("R_SHOULDER",       "CHEST",            "RIGHT_SHOULDER",   "SPINE"),
    ("R_UPPERARM",       "RIGHT_SHOULDER",   "RIGHT_ELBOW",      "R_SHOULDER"),
    ("R_FOREARM",        "RIGHT_ELBOW",      "RIGHT_WRIST",      "R_UPPERARM"),
    ("R_HAND",           "RIGHT_WRIST",      "RIGHT_INDEX",      "R_FOREARM"),

    ("L_UPPERLEG",       "LEFT_HIP",         "LEFT_KNEE",        "HIPS"),
    ("L_LOWERLEG",       "LEFT_KNEE",        "LEFT_ANKLE",       "L_UPPERLEG"),
    ("L_FOOT",           "LEFT_ANKLE",       "LEFT_FOOT_INDEX",  "L_LOWERLEG"),

    ("R_UPPERLEG",       "RIGHT_HIP",        "RIGHT_KNEE",       "HIPS"),
    ("R_LOWERLEG",       "RIGHT_KNEE",       "RIGHT_ANKLE",      "R_UPPERLEG"),
    ("R_FOOT",           "RIGHT_ANKLE",      "RIGHT_FOOT_INDEX", "R_LOWERLEG"),
]

MEDIAPIPE_PARENTS = {b[0]: b[3] for b in MEDIAPIPE_BONES}


def _get_landmark(frame_lms, key):
    """key can be a real landmark name or a virtual one (HIPS, CHEST, HEAD)."""
    if key == "HIPS":
        l = frame_lms[MP["LEFT_HIP"]]
        r = frame_lms[MP["RIGHT_HIP"]]
        return Vector(((l[0] + r[0]) * 0.5, (l[1] + r[1]) * 0.5, (l[2] + r[2]) * 0.5))
    if key == "CHEST":
        l = frame_lms[MP["LEFT_SHOULDER"]]
        r = frame_lms[MP["RIGHT_SHOULDER"]]
        return Vector(((l[0] + r[0]) * 0.5, (l[1] + r[1]) * 0.5, (l[2] + r[2]) * 0.5))
    if key == "HEAD":
        # Midpoint of ears gives a decent head pivot
        l = frame_lms[MP["LEFT_EAR"]]
        r = frame_lms[MP["RIGHT_EAR"]]
        return Vector(((l[0] + r[0]) * 0.5, (l[1] + r[1]) * 0.5, (l[2] + r[2]) * 0.5))
    idx = MP[key]
    p = frame_lms[idx]
    return Vector((p[0], p[1], p[2]))


def _mp_to_blender(v):
    """MediaPipe uses X right, Y down, Z forward (camera looking -Z).
    Blender uses Z up, -Y forward. Convert: (x, -z, -y)."""
    return Vector((v.x, -v.z, -v.y))


def create_mediapipe_armature(name="MP_Armature"):
    """Create the rest-pose armature using the first meaningful frame shape.
    Rest pose is a neutral T-ish pose built analytically so retargeting is stable.
    """
    # Remove any existing armature with same name
    if name in bpy.data.objects:
        old = bpy.data.objects[name]
        bpy.data.objects.remove(old, do_unlink=True)

    arm_data = bpy.data.armatures.new(name)
    arm_obj = bpy.data.objects.new(name, arm_data)
    bpy.context.collection.objects.link(arm_obj)

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='EDIT')

    # Canonical rest-pose positions (in meters, Blender coords: Z up, -Y forward)
    # Rough human-ish proportions, doesn't matter exactly: bones get pose-rotated.
    rest = {
        "HIPS":             Vector((0.0,  0.0, 1.00)),
        "CHEST":            Vector((0.0,  0.0, 1.40)),
        "HEAD":             Vector((0.0,  0.0, 1.70)),
        "LEFT_SHOULDER":    Vector((0.20, 0.0, 1.45)),
        "LEFT_ELBOW":       Vector((0.45, 0.0, 1.45)),
        "LEFT_WRIST":       Vector((0.70, 0.0, 1.45)),
        "LEFT_INDEX":       Vector((0.78, 0.0, 1.45)),
        "RIGHT_SHOULDER":   Vector((-0.20, 0.0, 1.45)),
        "RIGHT_ELBOW":      Vector((-0.45, 0.0, 1.45)),
        "RIGHT_WRIST":      Vector((-0.70, 0.0, 1.45)),
        "RIGHT_INDEX":      Vector((-0.78, 0.0, 1.45)),
        "LEFT_HIP":         Vector((0.10, 0.0, 1.00)),
        "LEFT_KNEE":        Vector((0.10, 0.0, 0.55)),
        "LEFT_ANKLE":       Vector((0.10, 0.0, 0.10)),
        "LEFT_FOOT_INDEX":  Vector((0.10, -0.15, 0.05)),
        "RIGHT_HIP":        Vector((-0.10, 0.0, 1.00)),
        "RIGHT_KNEE":       Vector((-0.10, 0.0, 0.55)),
        "RIGHT_ANKLE":      Vector((-0.10, 0.0, 0.10)),
        "RIGHT_FOOT_INDEX": Vector((-0.10, -0.15, 0.05)),
    }

    def pt(key):
        if key in rest:
            return rest[key]
        if key == "HIPS":
            return rest["HIPS"]
        if key == "CHEST":
            return rest["CHEST"]
        if key == "HEAD":
            return rest["HEAD"]
        raise KeyError(key)

    ebones = arm_data.edit_bones

    # HIPS: small vertical bone at pelvis
    hips = ebones.new("HIPS")
    hips.head = rest["HIPS"]
    hips.tail = rest["HIPS"] + Vector((0, 0, 0.1))

    for name_, head_key, tail_key, parent_name in MEDIAPIPE_BONES:
        if name_ == "HIPS":
            continue
        b = ebones.new(name_)
        b.head = pt(head_key)
        b.tail = pt(tail_key)
        # Avoid zero-length bones
        if (b.tail - b.head).length < 1e-4:
            b.tail = b.head + Vector((0, 0, 0.05))
        if parent_name and parent_name in ebones:
            b.parent = ebones[parent_name]
            # Don't connect: child heads may not match parent tails
            b.use_connect = False

    bpy.ops.object.mode_set(mode='OBJECT')
    return arm_obj


def apply_landmarks_to_armature(arm_obj, frames, fps=30.0, scene=None):
    """Keyframe the armature in pose mode.
    For each bone, compute the world-space direction from its head landmark to
    its tail landmark, and compute the rotation that takes the rest-pose bone
    direction to that new direction (shortest arc quaternion).
    Root bone ('HIPS') also gets its location keyframed so the character moves.
    """
    if scene is None:
        scene = bpy.context.scene

    bpy.context.view_layer.objects.active = arm_obj
    bpy.ops.object.mode_set(mode='POSE')

    # Rest-pose bone directions in armature-local space
    rest_dirs = {}
    rest_heads_local = {}
    for b in arm_obj.data.bones:
        rest_dirs[b.name] = (b.tail_local - b.head_local).normalized()
        rest_heads_local[b.name] = b.head_local.copy()

    # Use rotation_quaternion mode for all bones
    for pb in arm_obj.pose.bones:
        pb.rotation_mode = 'QUATERNION'

    scene.frame_start = 1
    scene.frame_end = max(1, len(frames))
    # Try to match the scene fps
    try:
        scene.render.fps = int(round(fps))
        scene.render.fps_base = 1.0 if abs(fps - round(fps)) < 1e-3 else float(scene.render.fps) / fps
    except Exception:
        pass

    arm_inv = arm_obj.matrix_world.inverted()

    # First pass: compute root translation based on HIPS landmark across frames,
    # using first frame as origin so character starts at current rest HIPS.
    first_hips_world = None

    for f_idx, frame in enumerate(frames):
        lms = frame.get("landmarks")
        if not lms or len(lms) < 33:
            continue
        scene.frame_set(f_idx + 1)

        # Compute HIPS world position from landmarks, converted to Blender space
        hips_mp = _get_landmark(lms, "HIPS")
        hips_world = _mp_to_blender(hips_mp)
        if first_hips_world is None:
            first_hips_world = hips_world.copy()
        # Translation of HIPS from its rest position
        delta = hips_world - first_hips_world
        # Move in armature object space (assumes arm_obj has identity world transform
        # for simplicity; if user moves it, we still keyframe pose bone location
        # which is armature-local)
        hips_pb = arm_obj.pose.bones.get("HIPS")
        if hips_pb is not None:
            hips_pb.location = delta
            hips_pb.keyframe_insert(data_path="location", frame=f_idx + 1)

        # Compute each bone's target direction
        for bone_name, head_key, tail_key, parent_name in MEDIAPIPE_BONES:
            if bone_name == "HIPS":
                continue
            pb = arm_obj.pose.bones.get(bone_name)
            if pb is None:
                continue
            head_world = _mp_to_blender(_get_landmark(lms, head_key))
            tail_world = _mp_to_blender(_get_landmark(lms, tail_key))
            direction = tail_world - head_world
            if direction.length < 1e-5:
                continue
            direction.normalize()

            # Rest direction of this bone in armature-local space
            rest_dir = rest_dirs[bone_name]

            # Target direction must be in the *parent pose bone's local space*
            # because pose bone rotations are expressed relative to their parent.
            parent_pb = pb.parent
            if parent_pb is not None:
                # parent's current matrix (channel matrix includes animated rotation)
                parent_mat = parent_pb.matrix  # world-space armature matrix
                parent_inv = (arm_obj.matrix_world @ parent_mat).inverted()
                local_dir = (parent_inv.to_3x3() @ direction).normalized()
                # rest dir relative to parent too
                rest_parent_mat = parent_pb.bone.matrix_local
                rest_parent_inv = rest_parent_mat.inverted()
                rest_local = (rest_parent_inv.to_3x3() @ rest_dirs[bone_name]).normalized()
            else:
                local_dir = (arm_inv.to_3x3() @ direction).normalized()
                rest_local = rest_dirs[bone_name]

            # Shortest-arc quaternion from rest_local to local_dir
            q = rest_local.rotation_difference(local_dir)
            pb.rotation_quaternion = q
            pb.keyframe_insert(data_path="rotation_quaternion", frame=f_idx + 1)

    bpy.ops.object.mode_set(mode='OBJECT')
