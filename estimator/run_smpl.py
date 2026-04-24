#!/usr/bin/env python3
"""
SMPL parameter estimator from monocular video.

Runs OUTSIDE Blender via subprocess. Wraps TRAM (default) or 4D-Humans
(fallback) to extract per-frame SMPL parameters from a single video.

Output: .npz file containing:
    - smpl_poses:    (N, 72) axis-angle rotations for 24 joints
    - smpl_betas:    (N, 10) or (10,) shape parameters
    - smpl_trans:    (N, 3)  root translation per frame
    - camera_params: (N, 3)  weak-perspective camera [s, tx, ty]
    - fps:           scalar  video frame rate
    - frame_count:   scalar  number of frames
    - estimator:     string  which estimator was used
    - joints_3d:     (N, 24, 3) optional reconstructed 3D joints

Usage:
    python run_smpl.py --video /path/to/video.mp4 --out motion.npz
    python run_smpl.py --video /path/to/video.mp4 --out motion.npz --method hmr2
    python run_smpl.py --video /path/to/video.mp4 --out motion.npz --method tram

Requirements (TRAM):
    pip install torch tram-python  # or clone TRAM repo

Requirements (4D-Humans / HMR2.0):
    pip install torch hmr2

This script auto-detects which estimator is available and uses the best one.
"""

import argparse
import json
import os
import sys
import time

import numpy as np


# SMPL joint names (24 joints, standard ordering)
SMPL_JOINT_NAMES = [
    "pelvis",           # 0
    "left_hip",         # 1
    "right_hip",        # 2
    "spine1",           # 3
    "left_knee",        # 4
    "right_knee",       # 5
    "spine2",           # 6
    "left_ankle",       # 7
    "right_ankle",      # 8
    "spine3",           # 9
    "left_foot",        # 10
    "right_foot",       # 11
    "neck",             # 12
    "left_collar",      # 13
    "right_collar",     # 14
    "head",             # 15
    "left_shoulder",    # 16
    "right_shoulder",   # 17
    "left_elbow",       # 18
    "right_elbow",      # 19
    "left_wrist",       # 20
    "right_wrist",      # 21
    "left_hand",        # 22
    "right_hand",       # 23
]

# SMPL kinematic tree: parent index for each joint
SMPL_PARENT = [
    -1,  # 0  pelvis (root)
    0,   # 1  left_hip -> pelvis
    0,   # 2  right_hip -> pelvis
    0,   # 3  spine1 -> pelvis
    1,   # 4  left_knee -> left_hip
    2,   # 5  right_knee -> right_hip
    3,   # 6  spine2 -> spine1
    4,   # 7  left_ankle -> left_knee
    5,   # 8  right_ankle -> right_knee
    6,   # 9  spine3 -> spine2
    7,   # 10 left_foot -> left_ankle
    8,   # 11 right_foot -> right_ankle
    9,   # 12 neck -> spine3
    9,   # 13 left_collar -> spine3
    9,   # 14 right_collar -> spine3
    12,  # 15 head -> neck
    13,  # 16 left_shoulder -> left_collar
    14,  # 17 right_shoulder -> right_collar
    16,  # 18 left_elbow -> left_shoulder
    17,  # 19 right_elbow -> right_shoulder
    18,  # 20 left_wrist -> left_elbow
    19,  # 21 right_wrist -> right_elbow
    20,  # 22 left_hand -> left_wrist
    21,  # 23 right_hand -> right_wrist
]


def parse_args():
    p = argparse.ArgumentParser(description="Extract SMPL parameters from video")
    p.add_argument("--video", required=True, help="Path to input video")
    p.add_argument("--out", required=True, help="Output .npz path")
    p.add_argument("--method", choices=["tram", "hmr2", "auto"], default="auto",
                   help="Estimation method (default: auto-detect best available)")
    p.add_argument("--batch-size", type=int, default=32,
                   help="Batch size for inference")
    p.add_argument("--device", default="auto",
                   help="Device: auto, cpu, cuda, mps")
    return p.parse_args()


def _detect_device(requested):
    """Auto-detect the best available compute device."""
    import torch
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def _get_video_info(video_path):
    """Get video metadata without heavy dependencies."""
    import cv2
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        return None
    info = {
        "fps": cap.get(cv2.CAP_PROP_FPS) or 30.0,
        "frame_count": int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0,
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
    cap.release()
    return info


def _detect_method():
    """Auto-detect best available SMPL estimator."""
    # Try TRAM first (best quality)
    try:
        import tram  # noqa: F401
        print("[estimator] TRAM detected")
        return "tram"
    except ImportError:
        pass

    # Try 4D-Humans / HMR2.0
    try:
        import hmr2  # noqa: F401
        print("[estimator] 4D-Humans (HMR2.0) detected")
        return "hmr2"
    except ImportError:
        pass

    # Try via installed packages with different import names
    try:
        from TRAM.lib.pipeline import TRAMPipeline  # noqa: F401
        print("[estimator] TRAM detected (from TRAM package)")
        return "tram"
    except ImportError:
        pass

    try:
        from hmr2.models import HMR2  # noqa: F401
        print("[estimator] HMR2 detected (from hmr2 package)")
        return "hmr2"
    except ImportError:
        pass

    return None


def run_tram(video_path, output_path, device, batch_size):
    """
    Run TRAM estimation pipeline.

    TRAM (ECCV 2024) reconstructs:
    - SMPL body parameters (pose, shape, translation)
    - Camera trajectory via DPVO/SLAM
    - Global human trajectory in world coordinates
    """
    print(f"[estimator] Running TRAM on {video_path}")
    print(f"[estimator] Device: {device}, Batch size: {batch_size}")

    import torch
    import cv2

    # Try different TRAM import paths
    tram_pipeline = None
    try:
        from tram import TRAMPipeline
        tram_pipeline = TRAMPipeline(device=device)
    except ImportError:
        try:
            from TRAM.lib.pipeline import TRAMPipeline
            tram_pipeline = TRAMPipeline(device=device)
        except ImportError:
            # Manual TRAM pipeline using individual components
            print("[estimator] TRAM package not found as module, attempting manual pipeline")
            return _run_tram_manual(video_path, output_path, device, batch_size)

    t0 = time.time()
    results = tram_pipeline.process_video(video_path)
    elapsed = time.time() - t0
    print(f"[estimator] TRAM inference: {elapsed:.1f}s")

    # Extract SMPL parameters from TRAM output
    smpl_poses = results["smpl_poses"]      # (N, 72)
    smpl_betas = results["smpl_betas"]      # (N, 10) or (10,)
    smpl_trans = results["smpl_trans"]      # (N, 3)
    camera_params = results.get("camera", np.zeros((len(smpl_poses), 3)))
    joints_3d = results.get("joints_3d", None)

    video_info = _get_video_info(video_path)

    _save_npz(output_path, smpl_poses, smpl_betas, smpl_trans,
              camera_params, joints_3d, video_info, "tram")
    return True


def _run_tram_manual(video_path, output_path, device, batch_size):
    """
    Manual TRAM-style pipeline using ViTPose + HMR2 + DPVO components.
    This is the fallback when TRAM isn't installed as a single package.
    """
    print("[estimator] Manual TRAM pipeline not yet implemented.")
    print("[estimator] Please install TRAM: git clone https://github.com/yufu-wang/tram")
    print("[estimator] Falling back to HMR2...")
    return False


def run_hmr2(video_path, output_path, device, batch_size):
    """
    Run 4D-Humans (HMR2.0) estimation pipeline.

    HMR2.0 is a feed-forward transformer that predicts SMPL parameters
    from single images. We run it per-frame on the video.
    Good for: static poses, partial occlusions, simpler setup.
    Limitation: no global trajectory (camera-relative only).
    """
    print(f"[estimator] Running 4D-Humans (HMR2.0) on {video_path}")
    print(f"[estimator] Device: {device}, Batch size: {batch_size}")

    import torch
    import cv2

    # Try import
    hmr2_model = None
    try:
        from hmr2.models import HMR2
        from hmr2.utils import recursive_to
        hmr2_model = HMR2.from_pretrained().to(device).eval()
    except ImportError:
        try:
            from hmr2 import HMR2Model
            hmr2_model = HMR2Model(device=device)
        except ImportError:
            print("[estimator] ERROR: Cannot import 4D-Humans / HMR2")
            return False

    # Read video frames
    cap = cv2.VideoCapture(video_path)
    video_info = _get_video_info(video_path)
    if video_info is None:
        print(f"[estimator] ERROR: cannot open video: {video_path}")
        return False

    all_poses = []
    all_betas = []
    all_trans = []
    all_camera = []
    all_joints = []
    frame_idx = 0
    last_report = 0

    print(f"[estimator] Video: {video_info['width']}x{video_info['height']} "
          f"@ {video_info['fps']:.1f} fps, ~{video_info['frame_count']} frames")

    t0 = time.time()
    batch_frames = []

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        batch_frames.append(frame_rgb)
        frame_idx += 1

        if len(batch_frames) >= batch_size or not ok:
            # Process batch
            with torch.no_grad():
                for frame in batch_frames:
                    # Detect person + predict SMPL
                    result = hmr2_model.predict(frame)
                    if result is not None:
                        all_poses.append(result["smpl_poses"][0])    # (72,)
                        all_betas.append(result["smpl_betas"][0])    # (10,)
                        all_trans.append(result["smpl_trans"][0])    # (3,)
                        all_camera.append(result.get("camera", np.zeros(3))[0])
                        if "joints_3d" in result:
                            all_joints.append(result["joints_3d"][0])
                    else:
                        # No person detected: repeat last pose or zero
                        if all_poses:
                            all_poses.append(all_poses[-1])
                            all_betas.append(all_betas[-1])
                            all_trans.append(all_trans[-1])
                            all_camera.append(all_camera[-1])
                            if all_joints:
                                all_joints.append(all_joints[-1])
                        else:
                            all_poses.append(np.zeros(72))
                            all_betas.append(np.zeros(10))
                            all_trans.append(np.zeros(3))
                            all_camera.append(np.zeros(3))

            batch_frames = []

        if frame_idx - last_report >= 30:
            print(f"[estimator] Processed {frame_idx} frames", flush=True)
            last_report = frame_idx

    cap.release()
    elapsed = time.time() - t0
    print(f"[estimator] HMR2 inference: {elapsed:.1f}s for {frame_idx} frames")

    smpl_poses = np.array(all_poses)
    smpl_betas = np.array(all_betas)
    smpl_trans = np.array(all_trans)
    camera_params = np.array(all_camera)
    joints_3d = np.array(all_joints) if all_joints else None

    _save_npz(output_path, smpl_poses, smpl_betas, smpl_trans,
              camera_params, joints_3d, video_info, "hmr2")
    return True


def _save_npz(output_path, smpl_poses, smpl_betas, smpl_trans,
              camera_params, joints_3d, video_info, estimator_name):
    """Save SMPL parameters to .npz with full metadata."""
    data = {
        "smpl_poses": np.asarray(smpl_poses, dtype=np.float32),
        "smpl_betas": np.asarray(smpl_betas, dtype=np.float32),
        "smpl_trans": np.asarray(smpl_trans, dtype=np.float32),
        "camera_params": np.asarray(camera_params, dtype=np.float32),
        "fps": np.float32(video_info["fps"]),
        "frame_count": np.int32(len(smpl_poses)),
        "video_width": np.int32(video_info["width"]),
        "video_height": np.int32(video_info["height"]),
        "estimator": np.array(estimator_name),
        "joint_names": np.array(SMPL_JOINT_NAMES),
        "parent_indices": np.array(SMPL_PARENT, dtype=np.int32),
    }
    if joints_3d is not None:
        data["joints_3d"] = np.asarray(joints_3d, dtype=np.float32)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    np.savez_compressed(output_path, **data)

    n = len(smpl_poses)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[estimator] Saved {n} frames to {output_path} ({size_mb:.1f} MB)")
    print(f"[estimator] Estimator: {estimator_name}")
    print(f"[estimator] Poses shape: {np.asarray(smpl_poses).shape}")
    print(f"[estimator] Betas shape: {np.asarray(smpl_betas).shape}")
    print(f"[estimator] Trans shape: {np.asarray(smpl_trans).shape}")


def main():
    args = parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 2

    # Check basic dependencies
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("ERROR: opencv-python not installed. `pip install opencv-python`",
              file=sys.stderr)
        return 2
    try:
        import torch  # noqa: F401
    except ImportError:
        print("ERROR: PyTorch not installed. See https://pytorch.org/get-started/",
              file=sys.stderr)
        return 2

    device = _detect_device(args.device)
    print(f"[estimator] Using device: {device}")

    # Select method
    method = args.method
    if method == "auto":
        method = _detect_method()
        if method is None:
            print("ERROR: No SMPL estimator found.", file=sys.stderr)
            print("Install one of:", file=sys.stderr)
            print("  TRAM:       git clone https://github.com/yufu-wang/tram", file=sys.stderr)
            print("  4D-Humans:  pip install hmr2", file=sys.stderr)
            return 2

    output_path = args.out
    if not output_path.endswith(".npz"):
        output_path += ".npz"

    success = False
    if method == "tram":
        success = run_tram(args.video, output_path, device, args.batch_size)
        if not success:
            print("[estimator] TRAM failed, trying HMR2 fallback...")
            success = run_hmr2(args.video, output_path, device, args.batch_size)
    elif method == "hmr2":
        success = run_hmr2(args.video, output_path, device, args.batch_size)

    if not success:
        print("ERROR: estimation failed", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
