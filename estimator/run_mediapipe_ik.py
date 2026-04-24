#!/usr/bin/env python3
"""
MediaPipe Pose + Inverse Kinematics → SMPL-compatible rotations.

Extracts 33 3D landmarks via MediaPipe Pose, then computes joint
rotations for 24 SMPL-equivalent joints using analytical IK.
Outputs a .npz file in the same format as run_4dhumans.py.

This is the estimator that WORKS on macOS Apple Silicon without
CUDA or problematic dependencies (no chumpy, no detectron2).

Dependencies: mediapipe, opencv-python, numpy, scipy
Install:
    python3.11 -m venv ~/mp_env
    source ~/mp_env/bin/activate
    pip install mediapipe opencv-python numpy scipy

Usage:
    python run_mediapipe_ik.py --video /path/to/video.mp4 --out motion.npz
"""

import argparse
import os
import sys
import time

import numpy as np
from scipy.spatial.transform import Rotation


# MediaPipe landmark indices
MP_LANDMARKS = {
    "NOSE": 0,
    "LEFT_SHOULDER": 11, "RIGHT_SHOULDER": 12,
    "LEFT_ELBOW": 13, "RIGHT_ELBOW": 14,
    "LEFT_WRIST": 15, "RIGHT_WRIST": 16,
    "LEFT_HIP": 23, "RIGHT_HIP": 24,
    "LEFT_KNEE": 25, "RIGHT_KNEE": 26,
    "LEFT_ANKLE": 27, "RIGHT_ANKLE": 28,
    "LEFT_HEEL": 29, "RIGHT_HEEL": 30,
    "LEFT_FOOT_INDEX": 31, "RIGHT_FOOT_INDEX": 32,
    "LEFT_EAR": 7, "RIGHT_EAR": 8,
    "LEFT_INDEX": 19, "RIGHT_INDEX": 20,
}

# Mapping from SMPL joint index to how we compute it from MediaPipe
# Each entry: (parent_landmark, child_landmark) defining the bone direction
SMPL_FROM_MP = {
    0:  ("MID_HIP", "MID_SHOULDER"),        # pelvis → spine direction
    1:  ("LEFT_HIP", "LEFT_KNEE"),           # left_hip
    2:  ("RIGHT_HIP", "RIGHT_KNEE"),         # right_hip
    3:  ("MID_HIP", "MID_SHOULDER"),         # spine1
    4:  ("LEFT_KNEE", "LEFT_ANKLE"),          # left_knee
    5:  ("RIGHT_KNEE", "RIGHT_ANKLE"),        # right_knee
    6:  ("MID_HIP", "MID_SHOULDER"),         # spine2
    7:  ("LEFT_ANKLE", "LEFT_FOOT_INDEX"),    # left_ankle
    8:  ("RIGHT_ANKLE", "RIGHT_FOOT_INDEX"),  # right_ankle
    9:  ("MID_SHOULDER", "MID_EAR"),         # spine3
    10: ("LEFT_ANKLE", "LEFT_FOOT_INDEX"),    # left_foot
    11: ("RIGHT_ANKLE", "RIGHT_FOOT_INDEX"),  # right_foot
    12: ("MID_SHOULDER", "MID_EAR"),         # neck
    13: ("MID_SHOULDER", "LEFT_SHOULDER"),    # left_collar
    14: ("MID_SHOULDER", "RIGHT_SHOULDER"),   # right_collar
    15: ("MID_EAR", "NOSE"),                 # head
    16: ("LEFT_SHOULDER", "LEFT_ELBOW"),      # left_shoulder
    17: ("RIGHT_SHOULDER", "RIGHT_ELBOW"),    # right_shoulder
    18: ("LEFT_ELBOW", "LEFT_WRIST"),         # left_elbow
    19: ("RIGHT_ELBOW", "RIGHT_WRIST"),       # right_elbow
    20: ("LEFT_WRIST", "LEFT_INDEX"),         # left_wrist
    21: ("RIGHT_WRIST", "RIGHT_INDEX"),       # right_wrist
    22: ("LEFT_WRIST", "LEFT_INDEX"),         # left_hand
    23: ("RIGHT_WRIST", "RIGHT_INDEX"),       # right_hand
}

# SMPL rest pose directions (T-pose, Y-up)
# These define the "default" bone direction in SMPL T-pose
REST_DIRECTIONS = {
    0:  np.array([0, 1, 0]),     # pelvis: up
    1:  np.array([0, -1, 0]),    # left_hip: down
    2:  np.array([0, -1, 0]),    # right_hip: down
    3:  np.array([0, 1, 0]),     # spine1: up
    4:  np.array([0, -1, 0]),    # left_knee: down
    5:  np.array([0, -1, 0]),    # right_knee: down
    6:  np.array([0, 1, 0]),     # spine2: up
    7:  np.array([0, 0, 1]),     # left_ankle: forward
    8:  np.array([0, 0, 1]),     # right_ankle: forward
    9:  np.array([0, 1, 0]),     # spine3: up
    10: np.array([0, 0, 1]),     # left_foot: forward
    11: np.array([0, 0, 1]),     # right_foot: forward
    12: np.array([0, 1, 0]),     # neck: up
    13: np.array([-1, 0, 0]),    # left_collar: left
    14: np.array([1, 0, 0]),     # right_collar: right
    15: np.array([0, 1, 0]),     # head: up
    16: np.array([-1, 0, 0]),    # left_shoulder: left
    17: np.array([1, 0, 0]),     # right_shoulder: right
    18: np.array([-1, 0, 0]),    # left_elbow: left
    19: np.array([1, 0, 0]),     # right_elbow: right
    20: np.array([-1, 0, 0]),    # left_wrist: left
    21: np.array([1, 0, 0]),     # right_wrist: right
    22: np.array([-1, 0, 0]),    # left_hand: left
    23: np.array([1, 0, 0]),     # right_hand: right
}


def parse_args():
    p = argparse.ArgumentParser(description="MediaPipe + IK estimator")
    p.add_argument("--video", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--complexity", type=int, default=2, choices=[0, 1, 2])
    p.add_argument("--smooth", action="store_true", default=True)
    return p.parse_args()


def _get_landmark_pos(world_landmarks, name):
    """Get 3D position of a named landmark or virtual landmark."""
    if name == "MID_HIP":
        l = world_landmarks[MP_LANDMARKS["LEFT_HIP"]]
        r = world_landmarks[MP_LANDMARKS["RIGHT_HIP"]]
        return np.array([(l.x + r.x) / 2, (l.y + r.y) / 2, (l.z + r.z) / 2])
    elif name == "MID_SHOULDER":
        l = world_landmarks[MP_LANDMARKS["LEFT_SHOULDER"]]
        r = world_landmarks[MP_LANDMARKS["RIGHT_SHOULDER"]]
        return np.array([(l.x + r.x) / 2, (l.y + r.y) / 2, (l.z + r.z) / 2])
    elif name == "MID_EAR":
        l = world_landmarks[MP_LANDMARKS["LEFT_EAR"]]
        r = world_landmarks[MP_LANDMARKS["RIGHT_EAR"]]
        return np.array([(l.x + r.x) / 2, (l.y + r.y) / 2, (l.z + r.z) / 2])
    else:
        idx = MP_LANDMARKS[name]
        lm = world_landmarks[idx]
        return np.array([lm.x, lm.y, lm.z])


def _direction_to_rotation(rest_dir, target_dir):
    """
    Compute the rotation (as axis-angle) that takes rest_dir to target_dir.
    Uses shortest-arc quaternion via scipy.
    """
    rest = rest_dir / (np.linalg.norm(rest_dir) + 1e-8)
    target = target_dir / (np.linalg.norm(target_dir) + 1e-8)

    # Cross product for axis
    cross = np.cross(rest, target)
    cross_norm = np.linalg.norm(cross)

    if cross_norm < 1e-7:
        # Vectors are parallel
        dot = np.dot(rest, target)
        if dot > 0:
            return np.zeros(3)  # Same direction, no rotation
        else:
            # Opposite direction: 180 degree rotation around any perpendicular
            perp = np.array([1, 0, 0]) if abs(rest[0]) < 0.9 else np.array([0, 1, 0])
            axis = np.cross(rest, perp)
            axis /= np.linalg.norm(axis)
            return axis * np.pi

    axis = cross / cross_norm
    dot = np.clip(np.dot(rest, target), -1, 1)
    angle = np.arccos(dot)
    return axis * angle


def _smooth_rotations(all_poses, window=3):
    """Smooth axis-angle rotations by converting to quaternions and averaging."""
    n_frames, n_params = all_poses.shape
    smoothed = np.copy(all_poses)

    for joint in range(24):
        # Extract this joint's rotations
        joint_aa = all_poses[:, joint*3:joint*3+3]

        # Convert to quaternions
        quats = []
        for aa in joint_aa:
            norm = np.linalg.norm(aa)
            if norm < 1e-8:
                quats.append(np.array([1, 0, 0, 0]))
            else:
                r = Rotation.from_rotvec(aa)
                quats.append(r.as_quat())  # [x, y, z, w]
        quats = np.array(quats)

        # Fix quaternion sign flips (ensure shortest path)
        for i in range(1, len(quats)):
            if np.dot(quats[i], quats[i-1]) < 0:
                quats[i] = -quats[i]

        # Moving average on quaternions
        half = window // 2
        smoothed_quats = np.copy(quats)
        for i in range(half, len(quats) - half):
            avg = np.mean(quats[i-half:i+half+1], axis=0)
            avg /= np.linalg.norm(avg) + 1e-8
            smoothed_quats[i] = avg

        # Convert back to axis-angle
        for i in range(len(smoothed_quats)):
            r = Rotation.from_quat(smoothed_quats[i])
            smoothed[i, joint*3:joint*3+3] = r.as_rotvec()

    return smoothed


def main():
    args = parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 2

    try:
        import cv2
    except ImportError:
        print("ERROR: pip install opencv-python", file=sys.stderr)
        return 2
    try:
        import mediapipe as mp
    except ImportError:
        print("ERROR: pip install mediapipe", file=sys.stderr)
        return 2

    # Open video
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open video: {args.video}", file=sys.stderr)
        return 2

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[mediapipe_ik] Video: {width}x{height} @ {fps:.1f} fps, ~{total_frames} frames")

    # Init MediaPipe Pose
    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(
        static_image_mode=False,
        model_complexity=args.complexity,
        smooth_landmarks=args.smooth,
        enable_segmentation=False,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    all_poses = []      # (N, 72) axis-angle
    all_trans = []       # (N, 3) root translation
    all_joints_3d = []   # (N, 24, 3) joint positions

    frame_idx = 0
    last_report = 0
    t0 = time.time()
    first_hip = None

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        results = pose.process(frame_rgb)

        if results.pose_world_landmarks:
            wl = results.pose_world_landmarks.landmark

            # Compute root translation (hip center)
            hip_pos = _get_landmark_pos(wl, "MID_HIP")
            if first_hip is None:
                first_hip = hip_pos.copy()
            root_trans = hip_pos - first_hip

            # Compute rotations for each SMPL joint
            joint_rotations = np.zeros(72, dtype=np.float32)
            joint_positions = np.zeros((24, 3), dtype=np.float32)

            for joint_idx, (parent_name, child_name) in SMPL_FROM_MP.items():
                parent_pos = _get_landmark_pos(wl, parent_name)
                child_pos = _get_landmark_pos(wl, child_name)

                # Bone direction in world space
                bone_dir = child_pos - parent_pos
                bone_length = np.linalg.norm(bone_dir)
                if bone_length < 1e-5:
                    continue

                bone_dir_normalized = bone_dir / bone_length

                # Get rest direction for this joint
                rest_dir = REST_DIRECTIONS[joint_idx]

                # Compute rotation from rest to current
                rot_aa = _direction_to_rotation(rest_dir, bone_dir_normalized)
                joint_rotations[joint_idx*3:joint_idx*3+3] = rot_aa

                # Store joint position (midpoint for reference)
                joint_positions[joint_idx] = parent_pos

            all_poses.append(joint_rotations)
            all_trans.append(root_trans)
            all_joints_3d.append(joint_positions)

        else:
            # No detection: hold previous frame
            if all_poses:
                all_poses.append(all_poses[-1])
                all_trans.append(all_trans[-1])
                all_joints_3d.append(all_joints_3d[-1])
            else:
                all_poses.append(np.zeros(72, dtype=np.float32))
                all_trans.append(np.zeros(3, dtype=np.float32))
                all_joints_3d.append(np.zeros((24, 3), dtype=np.float32))

        frame_idx += 1
        if frame_idx - last_report >= 30:
            elapsed = time.time() - t0
            fps_proc = frame_idx / elapsed if elapsed > 0 else 0
            print(f"[mediapipe_ik] Frame {frame_idx}/{total_frames} "
                  f"({fps_proc:.1f} fps)", flush=True)
            last_report = frame_idx

    cap.release()
    pose.close()

    elapsed = time.time() - t0
    print(f"[mediapipe_ik] Done: {frame_idx} frames in {elapsed:.1f}s")

    if not all_poses:
        print("ERROR: no frames processed", file=sys.stderr)
        return 1

    # Convert to arrays
    poses_array = np.array(all_poses, dtype=np.float32)
    trans_array = np.array(all_trans, dtype=np.float32)
    joints_array = np.array(all_joints_3d, dtype=np.float32)

    # Smooth rotations
    print("[mediapipe_ik] Smoothing rotations (quaternion averaging)...")
    poses_array = _smooth_rotations(poses_array, window=5)

    # Compute median "betas" from bone lengths (fake but consistent format)
    betas = np.zeros(10, dtype=np.float32)

    # Save using smpl_output format
    sys.path.insert(0, os.path.dirname(__file__))
    from smpl_output import save_smpl_npz

    output_path = args.out
    if not output_path.endswith(".npz"):
        output_path += ".npz"

    save_smpl_npz(
        output_path=output_path,
        smpl_poses=poses_array,
        smpl_betas=betas,
        smpl_trans=trans_array,
        joints_3d=joints_array,
        fps=fps,
        video_width=width,
        video_height=height,
        estimator_name="mediapipe_ik",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
