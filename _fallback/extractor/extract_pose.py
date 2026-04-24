#!/usr/bin/env python3
"""
Multi-view MediaPipe Pose extractor + triangulation.

Runs OUTSIDE Blender via subprocess. Accepts multiple videos (one per view),
extracts 2D landmarks from each, then triangulates into 3D world coordinates.

Usage:
    python extract_pose.py --views front=/path/front.mp4 left=/path/left.mp4 \\
                           --out landmarks.json \\
                           [--complexity 1] [--min-detection 0.5] [--smooth]

    Single video mode (backward compatible):
    python extract_pose.py --video /path/to/video.mp4 --out landmarks.json

Requirements:
    pip install mediapipe opencv-python numpy
"""

import argparse
import json
import os
import sys

import numpy as np


# MediaPipe Pose landmark names (33 landmarks)
LANDMARK_NAMES = [
    "NOSE",
    "LEFT_EYE_INNER", "LEFT_EYE", "LEFT_EYE_OUTER",
    "RIGHT_EYE_INNER", "RIGHT_EYE", "RIGHT_EYE_OUTER",
    "LEFT_EAR", "RIGHT_EAR",
    "MOUTH_LEFT", "MOUTH_RIGHT",
    "LEFT_SHOULDER", "RIGHT_SHOULDER",
    "LEFT_ELBOW", "RIGHT_ELBOW",
    "LEFT_WRIST", "RIGHT_WRIST",
    "LEFT_PINKY", "RIGHT_PINKY",
    "LEFT_INDEX", "RIGHT_INDEX",
    "LEFT_THUMB", "RIGHT_THUMB",
    "LEFT_HIP", "RIGHT_HIP",
    "LEFT_KNEE", "RIGHT_KNEE",
    "LEFT_ANKLE", "RIGHT_ANKLE",
    "LEFT_HEEL", "RIGHT_HEEL",
    "LEFT_FOOT_INDEX", "RIGHT_FOOT_INDEX",
]

# Camera direction vectors for each named view (used for triangulation)
# These define the camera's look-at direction (camera looks toward origin)
VIEW_DIRECTIONS = {
    "front":  np.array([0.0, 0.0, 1.0]),
    "back":   np.array([0.0, 0.0, -1.0]),
    "left":   np.array([1.0, 0.0, 0.0]),
    "right":  np.array([-1.0, 0.0, 0.0]),
    "top":    np.array([0.0, -1.0, 0.0]),
    "bottom": np.array([0.0, 1.0, 0.0]),
}

CAMERA_DISTANCE = 3.0  # meters, assumed distance from subject


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--video", help="Single video (backward compat)")
    p.add_argument("--views", nargs="+",
                   help="Multi-view: name=path pairs, e.g. front=vid.mp4 left=vid2.mp4")
    p.add_argument("--out", required=True)
    p.add_argument("--complexity", type=int, default=1, choices=[0, 1, 2])
    p.add_argument("--min-detection", type=float, default=0.5)
    p.add_argument("--min-tracking", type=float, default=0.5)
    p.add_argument("--smooth", action="store_true")
    return p.parse_args()


def extract_single_video(video_path, args):
    """Extract 2D normalized landmarks from a single video. Returns list of frames."""
    import cv2
    import mediapipe as mp

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"ERROR: cannot open video: {video_path}", file=sys.stderr)
        return None, 0, 0, 0, 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    nb_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"[extract] {video_path}: {width}x{height} @ {fps:.2f} fps, {nb_frames} frames")

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

        lms_2d = None
        lms_world = None

        if results.pose_landmarks:
            lms_2d = [
                [lm.x, lm.y, lm.visibility]
                for lm in results.pose_landmarks.landmark
            ]
        if results.pose_world_landmarks:
            lms_world = [
                [lm.x, lm.y, lm.z, lm.visibility]
                for lm in results.pose_world_landmarks.landmark
            ]

        frames_out.append({
            "frame": idx,
            "landmarks_2d": lms_2d,
            "landmarks_world": lms_world,
        })

        idx += 1
        if idx - last_report >= 30:
            print(f"[extract] {os.path.basename(video_path)}: {idx} frames", flush=True)
            last_report = idx

    cap.release()
    pose.close()
    return frames_out, fps, width, height, idx


def _rotation_from_direction(direction):
    """Build a 3x3 rotation matrix for a camera looking toward `direction`."""
    forward = direction / np.linalg.norm(direction)
    # Pick an up vector that isn't parallel to forward
    up_hint = np.array([0.0, 1.0, 0.0])
    if abs(np.dot(forward, up_hint)) > 0.99:
        up_hint = np.array([0.0, 0.0, 1.0])
    right = np.cross(forward, up_hint)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    # R rows: right, -up, forward (OpenCV convention: X right, Y down, Z forward)
    R = np.array([right, -up, forward])
    return R


def _build_projection(view_name, width, height):
    """Build a 3x4 projection matrix for a named view."""
    direction = VIEW_DIRECTIONS[view_name]
    R = _rotation_from_direction(direction)
    # Camera position: opposite of look direction, at CAMERA_DISTANCE
    cam_pos = -direction * CAMERA_DISTANCE
    t = -R @ cam_pos
    # Intrinsics: approximate with focal = max(w,h), principal point at center
    f = max(width, height)
    K = np.array([
        [f, 0, width / 2.0],
        [0, f, height / 2.0],
        [0, 0, 1],
    ])
    Rt = np.hstack([R, t.reshape(3, 1)])
    P = K @ Rt
    return P


def triangulate_landmarks(views_data, view_names, widths, heights):
    """
    Triangulate 3D landmarks from multiple 2D views using DLT.

    views_data: dict of view_name -> list of frames (each frame has landmarks_2d)
    Returns: list of frames with triangulated 3D landmarks
    """
    # Build projection matrices
    projections = {}
    for vname in view_names:
        idx = view_names.index(vname)
        projections[vname] = _build_projection(vname, widths[idx], heights[idx])

    # Find the minimum frame count across views
    min_frames = min(len(views_data[v]) for v in view_names)
    num_landmarks = 33

    frames_3d = []

    for f_idx in range(min_frames):
        # Collect 2D observations for each landmark from each view
        landmarks_3d = []

        for lm_idx in range(num_landmarks):
            points_2d = []
            proj_mats = []

            for vname in view_names:
                frame = views_data[vname][f_idx]
                lms = frame.get("landmarks_2d")
                if lms is None or len(lms) <= lm_idx:
                    continue
                lm = lms[lm_idx]
                vis = lm[2] if len(lm) > 2 else 1.0
                if vis < 0.3:
                    continue

                v_idx = view_names.index(vname)
                w, h = widths[v_idx], heights[v_idx]
                # Convert normalized coords to pixel coords
                px = lm[0] * w
                py = lm[1] * h
                points_2d.append(np.array([px, py]))
                proj_mats.append(projections[vname])

            if len(points_2d) >= 2:
                # DLT triangulation
                pt_3d = _triangulate_dlt(points_2d, proj_mats)
                landmarks_3d.append({
                    "name": LANDMARK_NAMES[lm_idx],
                    "x": round(float(pt_3d[0]), 6),
                    "y": round(float(pt_3d[1]), 6),
                    "z": round(float(pt_3d[2]), 6),
                    "views_used": len(points_2d),
                })
            elif len(points_2d) == 1:
                # Fallback: use world landmarks from the view that saw it
                for vname in view_names:
                    frame = views_data[vname][f_idx]
                    lms_w = frame.get("landmarks_world")
                    if lms_w and len(lms_w) > lm_idx:
                        lw = lms_w[lm_idx]
                        landmarks_3d.append({
                            "name": LANDMARK_NAMES[lm_idx],
                            "x": round(float(lw[0]), 6),
                            "y": round(float(lw[1]), 6),
                            "z": round(float(lw[2]), 6),
                            "views_used": 1,
                            "fallback": True,
                        })
                        break
            else:
                landmarks_3d.append({
                    "name": LANDMARK_NAMES[lm_idx],
                    "x": 0.0, "y": 0.0, "z": 0.0,
                    "views_used": 0,
                })

        frames_3d.append({
            "frame": f_idx,
            "landmarks_3d": landmarks_3d,
        })

        if f_idx % 30 == 0:
            print(f"[triangulate] frame {f_idx}/{min_frames}", flush=True)

    return frames_3d


def _triangulate_dlt(points_2d, proj_mats):
    """Direct Linear Transform triangulation from N views."""
    n = len(points_2d)
    A = np.zeros((2 * n, 4))
    for i in range(n):
        P = proj_mats[i]
        x, y = points_2d[i]
        A[2 * i]     = x * P[2] - P[0]
        A[2 * i + 1] = y * P[2] - P[1]

    _, _, Vt = np.linalg.svd(A)
    X = Vt[-1]
    X = X[:3] / X[3]
    return X


def main():
    args = parse_args()

    try:
        import cv2  # noqa: F401
    except ImportError:
        print("ERROR: opencv-python not installed. `pip install opencv-python`", file=sys.stderr)
        return 2
    try:
        import mediapipe  # noqa: F401
    except ImportError:
        print("ERROR: mediapipe not installed. `pip install mediapipe`", file=sys.stderr)
        return 2

    # Parse view inputs
    videos = {}
    if args.views:
        for v in args.views:
            if "=" not in v:
                print(f"ERROR: --views expects name=path format, got: {v}", file=sys.stderr)
                return 2
            name, path = v.split("=", 1)
            name = name.lower().strip()
            if name not in VIEW_DIRECTIONS:
                print(f"WARNING: unknown view '{name}', accepted: {list(VIEW_DIRECTIONS.keys())}")
            if not os.path.isfile(path):
                print(f"ERROR: video not found: {path}", file=sys.stderr)
                return 2
            videos[name] = path
    elif args.video:
        if not os.path.isfile(args.video):
            print(f"ERROR: video not found: {args.video}", file=sys.stderr)
            return 2
        videos["front"] = args.video
    else:
        print("ERROR: provide --video or --views", file=sys.stderr)
        return 2

    # Extract landmarks from each video
    views_data = {}
    view_names = []
    widths = []
    heights = []
    fps_ref = 30.0

    for view_name, video_path in videos.items():
        print(f"\n[extract] === Processing view: {view_name} ===")
        frames, fps, w, h, count = extract_single_video(video_path, args)
        if frames is None:
            return 2
        views_data[view_name] = frames
        view_names.append(view_name)
        widths.append(w)
        heights.append(h)
        fps_ref = fps
        print(f"[extract] {view_name}: {count} frames extracted")

    # Multi-view triangulation or single-view passthrough
    if len(view_names) >= 2:
        print(f"\n[triangulate] === Triangulating from {len(view_names)} views ===")
        frames_3d = triangulate_landmarks(views_data, view_names, widths, heights)
        mode = "multi_view_triangulated"
    else:
        # Single view: use world landmarks directly
        vname = view_names[0]
        frames_3d = []
        for frame in views_data[vname]:
            lms = frame.get("landmarks_world") or frame.get("landmarks_2d")
            if lms:
                landmarks = []
                for i, lm in enumerate(lms):
                    landmarks.append({
                        "name": LANDMARK_NAMES[i] if i < len(LANDMARK_NAMES) else f"LM_{i}",
                        "x": round(float(lm[0]), 6),
                        "y": round(float(lm[1]), 6),
                        "z": round(float(lm[2]), 6) if len(lm) > 2 else 0.0,
                        "views_used": 1,
                    })
            else:
                landmarks = []
            frames_3d.append({
                "frame": frame["frame"],
                "landmarks_3d": landmarks,
            })
        mode = "single_view_world"

    payload = {
        "mode": mode,
        "views": {
            vname: {
                "video": os.path.abspath(videos[vname]),
                "width": widths[i],
                "height": heights[i],
            }
            for i, vname in enumerate(view_names)
        },
        "fps": fps_ref,
        "frame_count": len(frames_3d),
        "landmark_names": LANDMARK_NAMES,
        "landmark_format": "xyz meters (triangulated)" if mode == "multi_view_triangulated"
                          else "xyz meters (mediapipe world)",
        "frames": frames_3d,
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(payload, f)
    print(f"\n[extract] wrote {len(frames_3d)} frames to {args.out} (mode: {mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
