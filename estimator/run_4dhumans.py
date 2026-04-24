#!/usr/bin/env python3
"""
4D-Humans (HMR2.0) SMPL estimator — macOS Apple Silicon compatible.

Runs OUTSIDE Blender in a dedicated Python environment.
Extracts per-frame SMPL parameters (pose, shape, translation) from a
monocular video using HMR2.0, a feed-forward transformer model.

Compatible with:
    - macOS Apple Silicon (M1/M2/M3/M4) via MPS backend
    - Linux/Windows with CUDA
    - CPU fallback (slow but works)

Usage:
    python run_4dhumans.py --video /path/to/video.mp4 --out motion.npz

Environment setup (macOS):
    python3.11 -m venv ~/hmr2_env
    source ~/hmr2_env/bin/activate
    pip install torch torchvision
    pip install hmr2 opencv-python numpy scipy
    # Set fallback for unsupported MPS ops:
    export PYTORCH_ENABLE_MPS_FALLBACK=1

Requirements: torch, torchvision, hmr2, opencv-python, numpy
RAM: 16 GB unified minimum on Apple Silicon
"""

import argparse
import os
import sys
import time

import numpy as np


def parse_args():
    p = argparse.ArgumentParser(description="4D-Humans SMPL estimator")
    p.add_argument("--video", required=True, help="Input video path")
    p.add_argument("--out", required=True, help="Output .npz path")
    p.add_argument("--batch-size", type=int, default=16,
                   help="Frames per batch (lower if RAM limited)")
    p.add_argument("--device", default="auto",
                   help="Device: auto, cpu, cuda, mps")
    return p.parse_args()


def _detect_device(requested):
    """Auto-detect best compute device, prefer MPS on Mac."""
    import torch
    if requested != "auto":
        return requested
    if torch.cuda.is_available():
        return "cuda"
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        # Set fallback env var in case it wasn't set externally
        os.environ.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
        return "mps"
    return "cpu"


def _get_video_info(video_path):
    """Get video metadata."""
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


def _read_video_frames(video_path):
    """Read all frames from video as RGB numpy arrays."""
    import cv2
    cap = cv2.VideoCapture(video_path)
    frames = []
    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break
        frames.append(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
    cap.release()
    return frames


def _detect_persons(frames, device, batch_size):
    """
    Detect persons in frames using ViTPose or a simple detector.
    Returns bounding boxes per frame.
    Falls back to center-crop if no detector is available.
    """
    # Try using a person detector
    try:
        from hmr2.utils.utils_detectron2 import DefaultPredictor_Lazy
        from hmr2.configs import DETECTRON2_CONFIG
        detector = DefaultPredictor_Lazy(DETECTRON2_CONFIG)
        bboxes = []
        for i, frame in enumerate(frames):
            outputs = detector(frame[:, :, ::-1])  # RGB to BGR for detectron
            instances = outputs["instances"]
            if len(instances) > 0:
                # Take the person with highest confidence
                scores = instances.scores.cpu().numpy()
                boxes = instances.pred_boxes.tensor.cpu().numpy()
                classes = instances.pred_classes.cpu().numpy()
                person_mask = classes == 0  # person class
                if person_mask.any():
                    person_scores = scores[person_mask]
                    person_boxes = boxes[person_mask]
                    best = person_scores.argmax()
                    bboxes.append(person_boxes[best])
                else:
                    bboxes.append(None)
            else:
                bboxes.append(None)
            if i % 30 == 0:
                print(f"[detect] frame {i}/{len(frames)}", flush=True)
        return bboxes
    except (ImportError, Exception) as e:
        print(f"[detect] Detector not available ({e}), using center crop")
        return None


def _center_crop_bbox(width, height):
    """Generate a center bounding box assuming person is centered."""
    cx, cy = width / 2, height / 2
    size = min(width, height) * 0.8
    return np.array([
        cx - size / 2, cy - size / 2,
        cx + size / 2, cy + size / 2,
    ])


def run_hmr2(video_path, output_path, device, batch_size):
    """
    Run 4D-Humans (HMR2.0) on a video.

    HMR2 is a Vision Transformer that predicts SMPL body parameters
    from a single image crop. We run it per-frame.

    Output is camera-relative (no global trajectory).
    """
    import torch
    import cv2

    video_info = _get_video_info(video_path)
    if video_info is None:
        print(f"[4dhumans] ERROR: cannot open video: {video_path}", file=sys.stderr)
        return False

    print(f"[4dhumans] Video: {video_info['width']}x{video_info['height']} "
          f"@ {video_info['fps']:.1f} fps, ~{video_info['frame_count']} frames")
    print(f"[4dhumans] Device: {device}")
    print(f"[4dhumans] Batch size: {batch_size}")

    # Load HMR2 model
    print("[4dhumans] Loading HMR2 model...")
    try:
        from hmr2.models import HMR2
        model = HMR2.from_pretrained().to(device).eval()
    except ImportError:
        try:
            # Alternative import path
            from hmr2 import HMR2Model
            model = HMR2Model(device=device)
        except ImportError:
            print("[4dhumans] ERROR: Cannot import HMR2. Install: pip install hmr2",
                  file=sys.stderr)
            return False

    # Read frames
    print("[4dhumans] Reading video frames...")
    frames = _read_video_frames(video_path)
    if not frames:
        print("[4dhumans] ERROR: no frames read", file=sys.stderr)
        return False
    print(f"[4dhumans] Read {len(frames)} frames")

    # Detect persons
    bboxes = _detect_persons(frames, device, batch_size)
    use_center_crop = bboxes is None
    if use_center_crop:
        default_bbox = _center_crop_bbox(video_info["width"], video_info["height"])

    # Process frames
    all_poses = []
    all_betas = []
    all_trans = []
    all_joints = []

    t0 = time.time()

    for i, frame in enumerate(frames):
        bbox = default_bbox if use_center_crop else (bboxes[i] if bboxes[i] is not None else default_bbox)

        try:
            with torch.no_grad():
                result = model.predict(frame, bbox=bbox)

            if result is not None and "smpl_poses" in result:
                all_poses.append(result["smpl_poses"][0].cpu().numpy()
                                 if torch.is_tensor(result["smpl_poses"][0])
                                 else result["smpl_poses"][0])
                all_betas.append(result["smpl_betas"][0].cpu().numpy()
                                 if torch.is_tensor(result["smpl_betas"][0])
                                 else result["smpl_betas"][0])
                all_trans.append(result["smpl_trans"][0].cpu().numpy()
                                 if torch.is_tensor(result["smpl_trans"][0])
                                 else result["smpl_trans"][0])
                if "joints_3d" in result:
                    j = result["joints_3d"][0]
                    all_joints.append(j.cpu().numpy() if torch.is_tensor(j) else j)
            else:
                # No detection: hold previous pose
                if all_poses:
                    all_poses.append(all_poses[-1])
                    all_betas.append(all_betas[-1])
                    all_trans.append(all_trans[-1])
                    if all_joints:
                        all_joints.append(all_joints[-1])
                else:
                    all_poses.append(np.zeros(72, dtype=np.float32))
                    all_betas.append(np.zeros(10, dtype=np.float32))
                    all_trans.append(np.zeros(3, dtype=np.float32))

        except Exception as e:
            print(f"[4dhumans] Frame {i} failed: {e}")
            if all_poses:
                all_poses.append(all_poses[-1])
                all_betas.append(all_betas[-1])
                all_trans.append(all_trans[-1])
                if all_joints:
                    all_joints.append(all_joints[-1])
            else:
                all_poses.append(np.zeros(72, dtype=np.float32))
                all_betas.append(np.zeros(10, dtype=np.float32))
                all_trans.append(np.zeros(3, dtype=np.float32))

        if i % 30 == 0:
            elapsed = time.time() - t0
            fps_proc = (i + 1) / elapsed if elapsed > 0 else 0
            print(f"[4dhumans] Frame {i}/{len(frames)} ({fps_proc:.1f} fps)", flush=True)

    elapsed = time.time() - t0
    print(f"[4dhumans] Done: {len(frames)} frames in {elapsed:.1f}s "
          f"({len(frames)/elapsed:.1f} fps)")

    # Use median betas (shape should be constant across frames)
    betas_array = np.array(all_betas)
    median_betas = np.median(betas_array, axis=0)

    # Save
    from smpl_output import save_smpl_npz
    save_smpl_npz(
        output_path=output_path,
        smpl_poses=np.array(all_poses),
        smpl_betas=median_betas,
        smpl_trans=np.array(all_trans),
        joints_3d=np.array(all_joints) if all_joints else None,
        fps=video_info["fps"],
        video_width=video_info["width"],
        video_height=video_info["height"],
        estimator_name="4dhumans",
    )
    return True


def main():
    args = parse_args()

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 2

    # Check dependencies
    try:
        import cv2  # noqa: F401
    except ImportError:
        print("ERROR: pip install opencv-python", file=sys.stderr)
        return 2
    try:
        import torch  # noqa: F401
    except ImportError:
        print("ERROR: pip install torch torchvision", file=sys.stderr)
        print("See https://pytorch.org/get-started/", file=sys.stderr)
        return 2

    device = _detect_device(args.device)
    print(f"[4dhumans] Platform: {sys.platform}")
    print(f"[4dhumans] Device: {device}")
    if device == "mps":
        print("[4dhumans] Apple Silicon MPS backend enabled")
        print("[4dhumans] PYTORCH_ENABLE_MPS_FALLBACK =",
              os.environ.get("PYTORCH_ENABLE_MPS_FALLBACK", "not set"))

    output_path = args.out
    if not output_path.endswith(".npz"):
        output_path += ".npz"

    success = run_hmr2(args.video, output_path, device, args.batch_size)
    if not success:
        print("ERROR: estimation failed", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
