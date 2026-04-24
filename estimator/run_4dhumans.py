#!/usr/bin/env python3
"""
4D-Humans (HMR2.0) SMPL estimator — macOS Apple Silicon compatible.

Runs OUTSIDE Blender in ~/hmr2_env.
Extracts per-frame SMPL parameters from a monocular video.

Setup (already done if you followed the install):
    ~/hmr2_env with torch, hmr2 (--no-deps), pytorch-lightning, smplx,
    timm, einops, yacs, opencv-python, numpy, scipy, omegaconf
    + patched chumpy stub + patched renderer imports + patched weights_only
    + SMPL_NEUTRAL.pkl cleaned of chumpy objects

Usage:
    PYTORCH_ENABLE_MPS_FALLBACK=1 ~/hmr2_env/bin/python run_4dhumans.py \\
        --video /path/to/video.mp4 --out motion.npz
"""

import argparse
import os
import sys
import time

import numpy as np


def parse_args():
    p = argparse.ArgumentParser(description="4D-Humans SMPL estimator")
    p.add_argument("--video", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--device", default="auto", help="auto, cpu, cuda, mps")
    return p.parse_args()


def _detect_device(requested):
    import torch
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        return "mps"
    return "cpu"


def main():
    args = parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 2

    import cv2
    import torch
    from hmr2.models import load_hmr2
    from hmr2.configs import get_config
    from hmr2.utils import recursive_to
    from hmr2.datasets.vitdet_dataset import ViTDetDataset, DEFAULT_MEAN, DEFAULT_STD
    from hmr2.utils.geometry import aa_to_rotmat

    device = _detect_device(args.device)
    print(f"[4dhumans] Device: {device}")

    # Load model
    print("[4dhumans] Loading HMR2 model...")
    model, model_cfg = load_hmr2()
    model = model.to(device)
    model.eval()
    print("[4dhumans] Model ready")

    # Open video
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[4dhumans] Video: {width}x{height} @ {fps:.1f} fps, ~{total} frames")

    img_size = model_cfg.MODEL.IMAGE_SIZE  # 256
    bbox_shape = model_cfg.MODEL.get("BBOX_SHAPE", [192, 256])

    all_poses = []
    all_betas = []
    all_trans = []
    frame_idx = 0
    t0 = time.time()

    # Center crop bbox (assume person centered in frame)
    cx, cy = width / 2, height / 2
    box_size = min(width, height) * 0.85
    center_bbox = np.array([cx - box_size/2, cy - box_size/2,
                            cx + box_size/2, cy + box_size/2])

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break

        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        # Crop and resize to model input size
        x1, y1, x2, y2 = center_bbox.astype(int)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        crop = frame_rgb[y1:y2, x1:x2]
        crop_resized = cv2.resize(crop, (img_size, img_size))

        # Normalize
        img_tensor = torch.from_numpy(crop_resized).float().permute(2, 0, 1) / 255.0
        for c in range(3):
            img_tensor[c] = (img_tensor[c] - DEFAULT_MEAN[c]) / DEFAULT_STD[c]

        batch = {
            "img": img_tensor.unsqueeze(0).to(device),
        }

        with torch.no_grad():
            out = model.forward_step(batch, train=False)

        # Extract SMPL params
        pred_pose = out["pred_smpl_params"]["body_pose"]   # (1, 23, 3, 3) rotmats
        pred_root = out["pred_smpl_params"]["global_orient"]  # (1, 1, 3, 3)
        pred_betas = out["pred_smpl_params"]["betas"]      # (1, 10)

        # Convert rotation matrices to axis-angle
        from scipy.spatial.transform import Rotation as R

        # Global orient (1 joint)
        root_mat = pred_root[0, 0].cpu().numpy()
        root_aa = R.from_matrix(root_mat).as_rotvec().astype(np.float32)

        # Body pose (23 joints)
        body_mats = pred_pose[0].cpu().numpy()  # (23, 3, 3)
        body_aa = np.zeros((23, 3), dtype=np.float32)
        for j in range(23):
            body_aa[j] = R.from_matrix(body_mats[j]).as_rotvec()

        # Concatenate: root (3) + body (69) = 72
        pose_72 = np.concatenate([root_aa, body_aa.flatten()])
        all_poses.append(pose_72)

        betas = pred_betas[0].cpu().numpy().astype(np.float32)
        all_betas.append(betas)

        # Camera translation (approximate)
        if "pred_cam_t_full" in out:
            trans = out["pred_cam_t_full"][0].cpu().numpy().astype(np.float32)
        elif "pred_cam" in out:
            cam = out["pred_cam"][0].cpu().numpy()
            # Weak-perspective to translation approximation
            trans = np.array([cam[1], cam[2], 2 * 5000.0 / (img_size * cam[0] + 1e-9)],
                            dtype=np.float32)
        else:
            trans = np.zeros(3, dtype=np.float32)
        all_trans.append(trans)

        frame_idx += 1
        if frame_idx % 30 == 0:
            elapsed = time.time() - t0
            rate = frame_idx / elapsed if elapsed > 0 else 0
            print(f"[4dhumans] Frame {frame_idx}/{total} ({rate:.1f} fps)", flush=True)

    cap.release()
    elapsed = time.time() - t0
    print(f"[4dhumans] Done: {frame_idx} frames in {elapsed:.1f}s "
          f"({frame_idx/elapsed:.1f} fps)")

    # Save
    sys.path.insert(0, os.path.dirname(__file__))
    from smpl_output import save_smpl_npz

    median_betas = np.median(np.array(all_betas), axis=0)
    output_path = args.out if args.out.endswith(".npz") else args.out + ".npz"

    save_smpl_npz(
        output_path=output_path,
        smpl_poses=np.array(all_poses),
        smpl_betas=median_betas,
        smpl_trans=np.array(all_trans),
        joints_3d=None,
        fps=fps,
        video_width=width,
        video_height=height,
        estimator_name="4dhumans",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
