#!/usr/bin/env python3
"""
4D-Humans (HMR2.0) SMPL estimator — macOS Apple Silicon compatible.

Extracts per-frame SMPL parameters (24 joint rotations + shape + translation)
from a monocular video using HMR2.0.

Usage:
    PYTORCH_ENABLE_MPS_FALLBACK=1 ~/hmr2_env/bin/python run_4dhumans.py \
        --video /path/to/video.mp4 --out motion.npz

Outputs .npz with:
    smpl_poses  (N, 72)  axis-angle rotations for 24 joints
    smpl_betas  (10,)    body shape (median across frames)
    smpl_trans  (N, 3)   camera-relative translation
    fps         scalar   video frame rate
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
    from scipy.spatial.transform import Rotation

    device = _detect_device(args.device)
    print(f"[4dhumans] Device: {device}")

    # Load model
    print("[4dhumans] Loading HMR2 model...")
    from hmr2.models import load_hmr2
    model, model_cfg = load_hmr2()
    model = model.to(device)
    model.eval()

    img_size = model_cfg.MODEL.IMAGE_SIZE  # 256

    # HMR2 normalization constants
    MEAN = [0.485, 0.456, 0.406]
    STD = [0.229, 0.224, 0.225]

    # Open video
    cap = cv2.VideoCapture(args.video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[4dhumans] Video: {width}x{height} @ {fps:.1f} fps, ~{total} frames")

    # Center crop: assume person is roughly centered
    crop_size = min(width, height)
    x_off = (width - crop_size) // 2
    y_off = (height - crop_size) // 2

    all_poses = []
    all_betas = []
    all_trans = []
    frame_idx = 0
    t0 = time.time()

    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break

        # Center crop + resize to model input
        crop = frame_bgr[y_off:y_off + crop_size, x_off:x_off + crop_size]
        crop_resized = cv2.resize(crop, (img_size, img_size))
        crop_rgb = cv2.cvtColor(crop_resized, cv2.COLOR_BGR2RGB)

        # Normalize: [0,255] -> [0,1] -> ImageNet normalize
        img_tensor = torch.from_numpy(crop_rgb).float().permute(2, 0, 1) / 255.0
        for c in range(3):
            img_tensor[c] = (img_tensor[c] - MEAN[c]) / STD[c]

        batch = {"img": img_tensor.unsqueeze(0).to(device)}

        with torch.no_grad():
            out = model.forward_step(batch, train=False)

        smpl_params = out["pred_smpl_params"]

        # Global orient: (1, 1, 3, 3) rotation matrix -> axis-angle (3,)
        global_mat = smpl_params["global_orient"][0, 0].cpu().numpy()
        global_aa = Rotation.from_matrix(global_mat).as_rotvec().astype(np.float32)

        # Body pose: (1, 23, 3, 3) rotation matrices -> axis-angle (69,)
        body_mats = smpl_params["body_pose"][0].cpu().numpy()  # (23, 3, 3)
        body_aa = np.zeros(69, dtype=np.float32)
        for j in range(23):
            body_aa[j*3:j*3+3] = Rotation.from_matrix(body_mats[j]).as_rotvec()

        # Concatenate: global (3) + body (69) = 72
        pose_72 = np.concatenate([global_aa, body_aa])
        all_poses.append(pose_72)

        # Betas: (1, 10)
        betas = smpl_params["betas"][0].cpu().numpy().astype(np.float32)
        all_betas.append(betas)

        # Camera translation: weak-perspective -> approximate 3D translation
        pred_cam = out["pred_cam"][0].cpu().numpy()  # (3,) [scale, tx, ty]
        s, tx, ty = pred_cam
        # Approximate translation: tz from scale, tx/ty from camera params
        focal = 5000.0  # HMR2 default focal length
        tz = 2.0 * focal / (img_size * s + 1e-9)
        trans = np.array([tx, ty, tz], dtype=np.float32)
        all_trans.append(trans)

        frame_idx += 1
        if frame_idx % 30 == 0:
            elapsed = time.time() - t0
            rate = frame_idx / elapsed if elapsed > 0 else 0
            print(f"[4dhumans] Frame {frame_idx}/{total} ({rate:.1f} fps)", flush=True)

    cap.release()
    elapsed = time.time() - t0
    print(f"[4dhumans] Done: {frame_idx} frames in {elapsed:.1f}s "
          f"({frame_idx / max(elapsed, 0.1):.1f} fps)")

    if not all_poses:
        print("ERROR: no frames processed", file=sys.stderr)
        return 1

    # Use median betas (body shape should be constant)
    median_betas = np.median(np.array(all_betas), axis=0).astype(np.float32)

    # Save
    output_path = args.out if args.out.endswith(".npz") else args.out + ".npz"
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)

    np.savez_compressed(
        output_path,
        smpl_poses=np.array(all_poses, dtype=np.float32),
        smpl_betas=median_betas,
        smpl_trans=np.array(all_trans, dtype=np.float32),
        fps=np.float32(fps),
        frame_count=np.int32(frame_idx),
        video_width=np.int32(width),
        video_height=np.int32(height),
        estimator=np.array("4dhumans"),
    )

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[4dhumans] Saved {frame_idx} frames to {output_path} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
