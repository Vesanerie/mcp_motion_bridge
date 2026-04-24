"""
Retargeting: MediaPipe armature -> Rigify or Mixamo.

Strategy: for each target pose bone that has a mapped source bone, add a
'Copy Rotation' constraint pointing to the source armature + source bone.
Then bake the visual transform into keyframes and remove the constraints.

This is more robust than computing rotations analytically because it respects
the target rig's rest orientations and bone roll.
"""

import bpy


# Source bone name -> Rigify metarig/Rigify deformation bone name
# We target the RIG (not the metarig): Rigify controls typically use
# 'upper_arm_fk.L' etc. We map to FK controls so the baked animation is editable.
RIGIFY_BONE_MAP = {
    "HIPS":        "torso",             # main torso control
    "SPINE":       "spine_fk.001",
    "NECK":        "neck",

    "L_UPPERARM":  "upper_arm_fk.L",
    "L_FOREARM":   "forearm_fk.L",
    "L_HAND":      "hand_fk.L",
    "R_UPPERARM":  "upper_arm_fk.R",
    "R_FOREARM":   "forearm_fk.R",
    "R_HAND":      "hand_fk.R",

    "L_UPPERLEG":  "thigh_fk.L",
    "L_LOWERLEG":  "shin_fk.L",
    "L_FOOT":      "foot_fk.L",
    "R_UPPERLEG":  "thigh_fk.R",
    "R_LOWERLEG":  "shin_fk.R",
    "R_FOOT":      "foot_fk.R",
}


# Source bone name -> Mixamo bone name (Mixamo prefix usually 'mixamorig:' or
# 'mixamorig1:' on re-imports; we strip/handle both).
MIXAMO_BONE_MAP = {
    "HIPS":        "Hips",
    "SPINE":       "Spine",
    "NECK":        "Neck",

    "L_UPPERARM":  "LeftArm",
    "L_FOREARM":   "LeftForeArm",
    "L_HAND":      "LeftHand",
    "R_UPPERARM":  "RightArm",
    "R_FOREARM":   "RightForeArm",
    "R_HAND":      "RightHand",

    "L_UPPERLEG":  "LeftUpLeg",
    "L_LOWERLEG":  "LeftLeg",
    "L_FOOT":      "LeftFoot",
    "R_UPPERLEG":  "RightUpLeg",
    "R_LOWERLEG":  "RightLeg",
    "R_FOOT":      "RightFoot",
}


def _find_bone_fuzzy(armature_obj, desired_name):
    """Find a bone by exact or suffix match (Mixamo prefixes vary)."""
    bones = armature_obj.pose.bones
    if desired_name in bones:
        return bones[desired_name]
    # Try suffix match for Mixamo: e.g. 'mixamorig:Hips', 'mixamorig1:Hips'
    lower = desired_name.lower()
    for pb in bones:
        n = pb.name.lower()
        if n.endswith(":" + lower) or n.endswith("_" + lower) or n.endswith(lower):
            return pb
    return None


def retarget_animation(src_arm, tgt_arm, mapping, bake=True):
    """Apply Copy Rotation / Copy Location constraints then bake."""
    scene = bpy.context.scene

    # Make target active
    bpy.ops.object.mode_set(mode='OBJECT')
    for obj in bpy.data.objects:
        obj.select_set(False)
    tgt_arm.select_set(True)
    bpy.context.view_layer.objects.active = tgt_arm
    bpy.ops.object.mode_set(mode='POSE')

    added_constraints = []

    for src_name, tgt_name in mapping.items():
        src_pb = src_arm.pose.bones.get(src_name)
        tgt_pb = _find_bone_fuzzy(tgt_arm, tgt_name)
        if src_pb is None or tgt_pb is None:
            print(f"[retarget] skip {src_name} -> {tgt_name} (not found)")
            continue

        # Copy rotation
        c = tgt_pb.constraints.new(type='COPY_ROTATION')
        c.name = "_mp_copy_rot"
        c.target = src_arm
        c.subtarget = src_pb.name
        c.target_space = 'LOCAL'
        c.owner_space = 'LOCAL'
        c.mix_mode = 'REPLACE'
        added_constraints.append((tgt_pb, c.name))

        # Copy location for root bone only (hips)
        if src_name == "HIPS":
            cl = tgt_pb.constraints.new(type='COPY_LOCATION')
            cl.name = "_mp_copy_loc"
            cl.target = src_arm
            cl.subtarget = src_pb.name
            cl.target_space = 'LOCAL'
            cl.owner_space = 'LOCAL'
            added_constraints.append((tgt_pb, cl.name))

    if not bake:
        return

    # Select all pose bones that have our constraints so bake_action operates on them
    for pb in tgt_arm.pose.bones:
        pb.bone.select = False
    for pb, _ in added_constraints:
        pb.bone.select = True

    frame_start = scene.frame_start
    frame_end = scene.frame_end

    # Bake visual keys into the target armature
    try:
        bpy.ops.nla.bake(
            frame_start=frame_start,
            frame_end=frame_end,
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,  # we'll clear manually to be specific
            clear_parents=False,
            use_current_action=True,
            bake_types={'POSE'},
        )
    except RuntimeError as e:
        print(f"[retarget] bake failed: {e}")

    # Remove only the constraints we added
    for pb, cname in added_constraints:
        c = pb.constraints.get(cname)
        if c is not None:
            pb.constraints.remove(c)

    bpy.ops.object.mode_set(mode='OBJECT')
