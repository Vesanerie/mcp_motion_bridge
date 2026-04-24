"""
SMPL output format specification.

Both run_4dhumans.py and any future cloud estimator (TRAM) must produce
a .npz file with this exact structure so the Blender addon and Claude
can consume it without changes.

Fields:
    smpl_poses      (N, 72)     float32   axis-angle rotations, 24 joints × 3
    smpl_betas      (10,)       float32   body shape parameters (constant)
    smpl_trans      (N, 3)      float32   root translation per frame
    joints_3d       (N, 24, 3)  float32   reconstructed 3D joint positions
    fps             scalar      float32   video frame rate
    frame_count     scalar      int32     number of frames
    video_width     scalar      int32     source video width
    video_height    scalar      int32     source video height
    estimator       string                "4dhumans" or "tram"

Joint ordering (SMPL standard):
    0  pelvis          12 neck
    1  left_hip        13 left_collar
    2  right_hip       14 right_collar
    3  spine1          15 head
    4  left_knee       16 left_shoulder
    5  right_knee      17 right_shoulder
    6  spine2          18 left_elbow
    7  left_ankle      19 right_elbow
    8  right_ankle     20 left_wrist
    9  spine3          21 right_wrist
    10 left_foot       22 left_hand
    11 right_foot      23 right_hand

Coordinate system:
    SMPL native: Y-up, X-right, Z-toward-camera
    Blender:     Z-up, X-right, -Y-forward
    Conversion:  (x, y, z) → (x, z, -y)
"""

import os
import numpy as np


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

SMPL_PARENTS = [
    -1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8,
    9, 9, 9, 12, 13, 14, 16, 17, 18, 19, 20, 21,
]


def save_smpl_npz(
    output_path,
    smpl_poses,
    smpl_betas,
    smpl_trans,
    joints_3d,
    fps,
    video_width,
    video_height,
    estimator_name,
):
    """Save SMPL parameters in the canonical .npz format."""
    data = {
        "smpl_poses": np.asarray(smpl_poses, dtype=np.float32),
        "smpl_betas": np.asarray(smpl_betas, dtype=np.float32),
        "smpl_trans": np.asarray(smpl_trans, dtype=np.float32),
        "fps": np.float32(fps),
        "frame_count": np.int32(len(smpl_poses)),
        "video_width": np.int32(video_width),
        "video_height": np.int32(video_height),
        "estimator": np.array(estimator_name),
        "joint_names": np.array(SMPL_JOINT_NAMES),
        "parent_indices": np.array(SMPL_PARENTS, dtype=np.int32),
    }
    if joints_3d is not None:
        data["joints_3d"] = np.asarray(joints_3d, dtype=np.float32)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    np.savez_compressed(output_path, **data)

    n = len(smpl_poses)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[smpl_output] Saved {n} frames to {output_path} ({size_mb:.1f} MB)")
    print(f"[smpl_output] Estimator: {estimator_name}")
    print(f"[smpl_output] Poses: {np.asarray(smpl_poses).shape}")
