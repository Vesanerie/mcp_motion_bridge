#!/usr/bin/env python3
"""
Extract MediaPipe Pose world landmarks from a video and write them as JSON.

Runs OUTSIDE Blender (Blender's bundled Python doesn't play well with mediapipe
on most platforms). The Blender addon calls this script via subprocess.

Usage:
    python extract_pose.py --video path/to/video.mp4 --out path/to/out.json \\
                           [--complexity 1] [--min-detection 0.5] [--smooth]

Requirements:
    pip install mediapipe opencv-python
"""

import argparse
import json
import os
import sys


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--video", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--complexity", type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--min-detection", type=float, default=0.5)
    p.add_argument("--min-tracking", type=float, default=0.5)
    p.add_argument("--smooth", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()

    try:
        import cv2
    except ImportError:
        print("ERROR: opencv-python is not installed. `pip install opencv-python`", file=sys.stderr)
        return 2
    try:
        import mediapipe as mp
    except ImportError:
        print("ERROR: mediapipe is not installed. `pip install mediapipe`", file=sys.stderr)
        return 2

    if not os.path.isfile(args.video):
        print(f"ERROR: video not found: {args.video}", file=sys.stderr)
        return 2

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: cannot open video: {args.video}", file=sys.stderr)
        return 2

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nb_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[extract] video: {width}x{height} @ {fps:.2f} fps, {nb_frames} frames")

    mp_pose = mp.solutions.pose
    pose = mp_pose.Pose(
        static_image_mode=False,
        model_complexity=args.complexity,
        smooth_landmarks=args.smooth,
        enable_segmentation=False,
        min_detection_confidence=args.min_detection,
        min_tracking_confidence=args.min_tracking,
    )

    frames_out = []
    idx = 0
    last_report = 0
    while True:
        ok, frame_bgr = cap.read()
        if not ok:
            break
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        results = pose.process(frame_rgb)

        # Prefer world landmarks (meters, hip-centered); fall back to image lm
        lms = None
        if results.pose_world_landmarks:
            lms = [
                [lm.x, lm.y, lm.z, lm.visibility]
                for lm in results.pose_world_landmarks.landmark
            ]
        elif results.pose_landmarks:
            # Image-space landmarks: x/y normalized 0..1, z relative
            lms = [
                [lm.x, lm.y, lm.z, lm.visibility]
                for lm in results.pose_landmarks.landmark
            ]

        frames_out.append({
            "frame": idx,
            "landmarks": lms,
        })

        idx += 1
        if idx - last_report >= 30:
            print(f"[extract] processed {idx} frames", flush=True)
            last_report = idx

    cap.release()
    pose.close()

    payload = {
        "source_video": os.path.abspath(args.video),
        "fps": fps,
        "width": width,
        "height": height,
        "frame_count": idx,
        "model_complexity": args.complexity,
        "landmark_format": "pose_world_landmarks (xyz meters, visibility)",
        "frames": frames_out,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f)
    print(f"[extract] wrote {idx} frames to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
