"""
Deterministic SMPL-to-rig animation transfer.

Reads an HMR2 .npz file and applies rotations to any rigged armature.
Handles axis conversion (SMPL Y-up → Blender Z-up), rest pose offsets,
height scaling, and chibi dampening.

Compatible with Blender 5.1 layered actions API.
Falls back to legacy API for Blender < 4.4.
"""

import numpy as np

try:
    import bpy
    from mathutils import Euler, Matrix, Quaternion, Vector
except ImportError:
    raise ImportError("This module must be run inside Blender's Python.")

from .smpl_constants import SMPL_JOINT_NAMES, SMPL_REF_HEIGHT


# ---------------------------------------------------------------------------
# Axis conversion: SMPL (Y-up, Z-toward-camera) → Blender (Z-up, -Y-forward)
# This is a -90° rotation around X.
# ---------------------------------------------------------------------------
_SMPL_TO_BLENDER = Quaternion((0.7071068, 0.7071068, 0, 0))  # +90° X


def _axis_conversion_quat() -> Quaternion:
    """Return the quaternion that converts SMPL coords to Blender coords."""
    return _SMPL_TO_BLENDER.copy()


def _convert_translation(t: np.ndarray, t0: np.ndarray) -> Vector:
    """Convert SMPL translation (Y-up) to Blender (Z-up).

    SMPL: (x_right, y_up, z_toward_camera)
    Blender: (x_right, -z_toward_camera, y_up) → (x, -z, y)

    The raw tz from HMR2 is a camera focal distance (typically 15-40m),
    not a scene position. We subtract frame 0 to get relative movement,
    which is what matters for animation.
    """
    rel = t - t0  # relative to first frame
    return Vector((float(rel[0]), -float(rel[2]), float(rel[1])))


# ---------------------------------------------------------------------------
# Rest pose capture
# ---------------------------------------------------------------------------
def _capture_bind_quats(armature_obj: "bpy.types.Object") -> dict:
    """
    Compute the bind quaternion for each bone.

    The bind quaternion C maps from SMPL's coordinate system to the
    Blender bone's local coordinate system:
        C = bone.matrix_local.to_quaternion().inverted() @ axis_conv

    This is used in the similarity transform:
        pose_q = C.inv() @ R_smpl @ C

    Which guarantees pose_q = identity when R_smpl = identity (rest pose).
    """
    axis_conv = _axis_conversion_quat()
    bind = {}
    for bone in armature_obj.data.bones:
        # matrix_local = bone's global rest orientation in armature space
        rest_q = bone.matrix_local.to_quaternion()
        bind[bone.name] = rest_q.inverted() @ axis_conv
    return bind


# ---------------------------------------------------------------------------
# Action creation — supports both layered (5.1+) and legacy APIs
# ---------------------------------------------------------------------------
def _has_layered_actions() -> bool:
    """Check if Blender supports the layered actions API (4.4+)."""
    major, minor = bpy.app.version[:2]
    return (major > 4) or (major == 4 and minor >= 4)


def _create_action(armature_obj: "bpy.types.Object", name: str):
    """Create and assign an action, using layered API if available."""
    action = bpy.data.actions.new(name=name)

    if _has_layered_actions():
        # Blender 4.4+ / 5.x layered actions
        try:
            slot = action.slots.new(for_id=armature_obj)
            layer = action.layers.new(name="Mocap")
            layer.strips.new(type='KEYFRAME')

            if not armature_obj.animation_data:
                armature_obj.animation_data_create()
            armature_obj.animation_data.action = action
            armature_obj.animation_data.action_slot = slot
            return action
        except (AttributeError, TypeError):
            # API changed between 4.4/5.0/5.1 — fall through to legacy
            pass

    # Legacy / fallback
    if not armature_obj.animation_data:
        armature_obj.animation_data_create()
    armature_obj.animation_data.action = action
    return action


# ---------------------------------------------------------------------------
# Main transfer function
# ---------------------------------------------------------------------------
def transfer_smpl_to_rig(
    npz_path: str,
    target_armature: "bpy.types.Object",
    bone_mapping: dict,
    height_ratio: float = 1.0,
    floor_offset: float = 0.0,
    bone_dampening: dict = None,
    action_name: str = "MocapTransfer",
) -> "bpy.types.Action":
    """
    Apply SMPL rotations from an HMR2 .npz to a target armature.

    Args:
        npz_path: Path to .npz with smpl_poses (N,72), smpl_trans (N,3), fps.
        target_armature: Blender armature object.
        bone_mapping: {smpl_joint_index: rig_bone_name or None}
        height_ratio: chibi_height / smpl_height (scales root translation).
        floor_offset: Vertical offset added to root Z every frame.
        bone_dampening: {bone_name: float 0-1} to reduce rotation intensity.
        action_name: Name for the created action.

    Returns:
        The created bpy.types.Action.
    """
    bone_dampening = bone_dampening or {}

    # --- Load motion data ---
    data = np.load(npz_path)
    poses = data["smpl_poses"]    # (N, 72) axis-angle
    trans = data["smpl_trans"]    # (N, 3)
    fps = float(data["fps"])
    num_frames = poses.shape[0]

    print(f"[transfer] Loading {num_frames} frames @ {fps} fps from {npz_path}")
    print(f"[transfer] Height ratio: {height_ratio:.3f}, floor offset: {floor_offset:.3f}")

    # --- Set scene FPS ---
    bpy.context.scene.render.fps = int(round(fps))
    bpy.context.scene.render.fps_base = 1.0
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_end = num_frames

    # --- Capture bind quaternions for similarity transform ---
    bind_quats = _capture_bind_quats(target_armature)

    # --- Resolve mapped bones ---
    mapped = {}  # {smpl_idx: (pose_bone, bind_quat, bind_quat_inv)}
    root_bone_name = bone_mapping.get(0)  # pelvis = root

    for smpl_idx in range(24):
        bone_name = bone_mapping.get(smpl_idx)
        if bone_name is None:
            continue
        pb = target_armature.pose.bones.get(bone_name)
        if pb is None:
            print(f"[transfer] WARNING: bone '{bone_name}' not found in armature")
            continue
        bq = bind_quats.get(bone_name, Quaternion((1, 0, 0, 0)))
        mapped[smpl_idx] = (pb, bq, bq.inverted())

    print(f"[transfer] Mapped {len(mapped)}/24 SMPL joints")

    # --- Ensure pose mode ---
    bpy.context.view_layer.objects.active = target_armature
    bpy.ops.object.mode_set(mode='POSE')

    # Set all mapped bones to quaternion mode
    for smpl_idx, (pb, _, _) in mapped.items():
        pb.rotation_mode = 'QUATERNION'

    # --- Create action ---
    action = _create_action(target_armature, action_name)

    # --- Axis conversion quaternion ---
    axis_conv = _axis_conversion_quat()

    # --- Reference frame for relative translation ---
    trans_ref = trans[0].copy()

    # --- Frame loop ---
    for f in range(num_frames):
        frame_num = f + 1

        # Root translation (only on pelvis bone)
        if root_bone_name and 0 in mapped:
            root_pb = mapped[0][0]
            t = trans[f]
            t_bl = _convert_translation(t, trans_ref)
            t_bl *= height_ratio
            t_bl.z += floor_offset
            root_pb.location = t_bl
            root_pb.keyframe_insert(data_path="location", frame=frame_num)

        # Rotations for each mapped joint
        for smpl_idx, (pb, bind_q, bind_inv) in mapped.items():
            aa = poses[f, smpl_idx * 3 : smpl_idx * 3 + 3]
            angle = float(np.linalg.norm(aa))

            if angle < 1e-8:
                q_smpl = Quaternion((1, 0, 0, 0))
            else:
                axis = aa / angle
                q_smpl = Quaternion(axis.tolist(), angle)

            # Similarity transform (conjugation):
            #   pose_q = C.inv() @ R_smpl @ C
            #
            # Where C = bone.matrix_local.inv() @ axis_conv
            # maps from SMPL world frame to Blender bone local frame.
            #
            # This guarantees: R_smpl = identity → pose_q = identity
            # (chibi stays in T-pose when SMPL is at rest)
            #
            # The conjugation re-expresses the SMPL rotation (which is
            # in SMPL's coordinate system) in the Blender bone's local
            # coordinate system.
            q_local = bind_inv @ q_smpl @ bind_q

            # Dampening (optional, for chibi big-head etc.)
            damp = bone_dampening.get(pb.name, 1.0)
            if damp < 1.0:
                identity = Quaternion((1, 0, 0, 0))
                q_local = identity.slerp(q_local, damp)

            pb.rotation_quaternion = q_local
            pb.keyframe_insert(
                data_path="rotation_quaternion", frame=frame_num
            )

        if frame_num % 100 == 0:
            print(f"[transfer] Frame {frame_num}/{num_frames}")

    print(f"[transfer] Done — {num_frames} frames applied to "
          f"{len(mapped)} bones")

    bpy.ops.object.mode_set(mode='OBJECT')
    return action


# ---------------------------------------------------------------------------
# Convenience: measure rig height for automatic height_ratio
# ---------------------------------------------------------------------------
def measure_rig_height(armature_obj: "bpy.types.Object") -> float:
    """
    Estimate character height from armature bone positions.
    Measures from lowest foot bone to highest head bone (Z axis).
    """
    bones = armature_obj.data.bones
    if not bones:
        return SMPL_REF_HEIGHT

    z_values = []
    for b in bones:
        z_values.append(b.head_local.z)
        z_values.append(b.tail_local.z)

    height = max(z_values) - min(z_values)
    return max(height, 0.1)  # avoid zero


def compute_height_ratio(armature_obj: "bpy.types.Object") -> float:
    """Compute height_ratio = rig_height / smpl_reference_height."""
    h = measure_rig_height(armature_obj)
    ratio = h / SMPL_REF_HEIGHT
    print(f"[transfer] Rig height: {h:.3f}m, SMPL ref: {SMPL_REF_HEIGHT}m, "
          f"ratio: {ratio:.3f}")
    return ratio
