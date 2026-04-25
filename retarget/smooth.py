"""
Post-transfer smoothing via Blender's native FCurve modifiers.

Uses Gaussian Smooth modifier (non-destructive, adjustable after the fact).
Compatible with Blender 5.1 layered actions and legacy API.
"""

try:
    import bpy
except ImportError:
    raise ImportError("This module must be run inside Blender's Python.")


def _get_fcurves(action):
    """Get fcurves from an action, handling both layered (5.1+) and legacy API."""
    # Try legacy first (direct access)
    if hasattr(action, 'fcurves') and action.fcurves is not None:
        try:
            _ = len(action.fcurves)
            return action.fcurves
        except Exception:
            pass

    # Layered actions (Blender 4.4+/5.x): action.layers[].strips[].channelbags[].fcurves
    fcurves = []
    if hasattr(action, 'layers'):
        for layer in action.layers:
            for strip in layer.strips:
                if hasattr(strip, 'channelbags'):
                    for cbag in strip.channelbags:
                        fcurves.extend(cbag.fcurves)
    return fcurves


def apply_gaussian_smooth(
    action: "bpy.types.Action",
    sigma: float = 1.0,
    only_rotation: bool = True,
):
    """
    Add a Gaussian Smooth modifier to all FCurves in the action.

    Args:
        action: The action to smooth.
        sigma: Gaussian sigma (higher = smoother, default 1.0).
        only_rotation: If True, only smooth rotation curves (not location).
    """
    fcurves = _get_fcurves(action)
    if not fcurves:
        print("[smooth] No fcurves found in action")
        return 0

    count = 0
    for fcurve in fcurves:
        if only_rotation and "rotation" not in fcurve.data_path:
            continue
        # Don't double-add
        has_gauss = any(m.type == 'FNGAUSSSMOOTH' for m in fcurve.modifiers)
        if has_gauss:
            continue
        try:
            mod = fcurve.modifiers.new(type='FNGAUSSSMOOTH')
            mod.sigma = sigma
            count += 1
        except Exception:
            pass

    print(f"[smooth] Applied Gaussian smooth (sigma={sigma}) "
          f"to {count} fcurves")
    return count
