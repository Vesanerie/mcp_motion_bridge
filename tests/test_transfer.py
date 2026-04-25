"""
End-to-end test: load chibi .blend + HMR2 .npz → animated chibi .blend

Usage (from repo root):
    /Applications/Blender.app/Contents/MacOS/Blender \
        --background \
        "/Users/mardoukhaevvalentin/Documents/Blender/chibi vesanerie Rigged_Backup.blend" \
        --python tests/test_transfer.py

Or with custom paths:
    blender --background <blend> --python tests/test_transfer.py -- \
        --npz /path/to/motion.npz --out /path/to/output.blend
"""

import os
import sys

# Add repo root to path so we can import retarget
repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

import bpy

from retarget.bone_mapping import auto_map_bones, print_mapping
from retarget.transfer import (
    compute_height_ratio,
    transfer_smpl_to_rig,
)
from retarget.smooth import apply_gaussian_smooth


def parse_args():
    """Parse args after '--' separator."""
    argv = sys.argv
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []

    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--npz", default="/tmp/walk_cycle_hmr2.npz")
    p.add_argument("--out", default="/tmp/chibi_animated.blend")
    return p.parse_args(argv)


def main():
    args = parse_args()

    print("=" * 60)
    print("TEST: SMPL-to-Chibi Transfer")
    print("=" * 60)

    # Find the armature
    armature = None
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE':
            armature = obj
            break

    if armature is None:
        print("ERROR: No armature found in the scene!")
        print("Objects in scene:")
        for obj in bpy.data.objects:
            print(f"  {obj.type}: {obj.name}")
        sys.exit(1)

    print(f"\nArmature: {armature.name} ({len(armature.data.bones)} bones)")

    # Step 1: Auto bone mapping
    print("\n--- Step 1: Auto Bone Mapping ---")
    mapping, confidence = auto_map_bones(armature)
    print(print_mapping(mapping, confidence))

    mapped_count = sum(1 for v in mapping.values() if v is not None)
    unmapped = [
        f"  {i}: {name}"
        for i, name in enumerate(
            __import__('retarget.smpl_constants', fromlist=['SMPL_JOINT_NAMES']).SMPL_JOINT_NAMES
        )
        if mapping.get(i) is None
    ]
    print(f"\nMapped: {mapped_count}/24")
    if unmapped:
        print(f"Unmapped ({len(unmapped)}):")
        for u in unmapped:
            print(u)

    # Step 2: Compute height ratio
    print("\n--- Step 2: Height Ratio ---")
    height_ratio = compute_height_ratio(armature)

    # Step 3: Transfer
    print("\n--- Step 3: Animation Transfer ---")
    if not os.path.isfile(args.npz):
        print(f"ERROR: .npz file not found: {args.npz}")
        sys.exit(1)

    action = transfer_smpl_to_rig(
        npz_path=args.npz,
        target_armature=armature,
        bone_mapping=mapping,
        height_ratio=height_ratio,
        floor_offset=0.0,
        bone_dampening={"spine.006": 0.5},  # Dampen chibi big head
        action_name="WalkCycleTest",
    )

    print(f"\nAction created: {action.name}")
    try:
        print(f"  FCurves: {len(action.fcurves)}")
        frame_range = action.frame_range
        print(f"  Frame range: {frame_range[0]:.0f} - {frame_range[1]:.0f}")
    except Exception as e:
        print(f"  (fcurves access: {e} — layered actions API)")

    # Step 4: Smooth
    print("\n--- Step 4: Gaussian Smooth ---")
    apply_gaussian_smooth(action, sigma=1.0, only_rotation=True)

    # Step 5: Save
    print(f"\n--- Step 5: Save to {args.out} ---")
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=args.out)
    size_mb = os.path.getsize(args.out) / (1024 * 1024)
    print(f"Saved: {args.out} ({size_mb:.1f} MB)")

    print("\n" + "=" * 60)
    print("TEST COMPLETE")
    print(f"Open in Blender: {args.out}")
    print(f"Press Space to play animation (should be a walk cycle)")
    print("=" * 60)


if __name__ == "__main__":
    main()
