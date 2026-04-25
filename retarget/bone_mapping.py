"""
Fuzzy bone mapping: SMPL 24 joints → arbitrary rig bones.

Scores each rig bone against each SMPL joint using synonym tables,
substring matching, and suffix normalization (.L/.R variants).
Returns a dict {smpl_index: bone_name_or_None}.
"""

from .smpl_constants import SMPL_JOINT_NAMES, SYNONYMS

# Side suffix patterns: (suffix_to_strip, canonical_form)
_SIDE_PATTERNS = [
    (".l", ".l"), (".r", ".r"),
    ("_l", ".l"), ("_r", ".r"),
    (".left", ".l"), (".right", ".r"),
    ("_left", ".l"), ("_right", ".r"),
    ("left", ".l"), ("right", ".r"),
    (" l", ".l"), (" r", ".r"),
]

# Prefixes to strip (Rigify DEF-/ORG-/MCH- layers)
_STRIP_PREFIXES = ["def-", "org-", "mch-", "ctrl-"]


def _normalize(name: str) -> str:
    """Lowercase, strip known prefixes."""
    n = name.lower().strip()
    for pfx in _STRIP_PREFIXES:
        if n.startswith(pfx):
            n = n[len(pfx):]
    return n


def _score(smpl_joint: str, rig_bone_name: str) -> int:
    """Score how well a rig bone matches an SMPL joint (0-100)."""
    norm = _normalize(rig_bone_name)
    synonyms = SYNONYMS.get(smpl_joint, [])

    # Exact match with any synonym
    for syn in synonyms:
        if norm == syn.lower():
            return 100

    # Exact match with SMPL name itself
    if norm == smpl_joint.lower():
        return 100

    # Substring: synonym appears in bone name or vice versa
    for syn in synonyms:
        sl = syn.lower()
        if sl in norm or norm in sl:
            return 60

    # Substring with SMPL name
    smpl_lower = smpl_joint.lower()
    if smpl_lower in norm or norm in smpl_lower:
        return 50

    return 0


def auto_map_bones(armature_obj) -> dict:
    """
    Auto-map SMPL 24 joints to rig bones.

    Args:
        armature_obj: bpy.types.Object with type='ARMATURE'

    Returns:
        dict {smpl_index: rig_bone_name or None}
        Also returns confidence dict {smpl_index: score}
    """
    bone_names = [b.name for b in armature_obj.data.bones]

    mapping = {}
    confidence = {}
    used_bones = set()

    for smpl_idx, smpl_joint in enumerate(SMPL_JOINT_NAMES):
        best_score = 0
        best_bone = None

        for bname in bone_names:
            if bname in used_bones:
                continue
            s = _score(smpl_joint, bname)
            if s > best_score:
                best_score = s
                best_bone = bname

        if best_score >= 50:
            mapping[smpl_idx] = best_bone
            confidence[smpl_idx] = best_score
            used_bones.add(best_bone)
        else:
            mapping[smpl_idx] = None
            confidence[smpl_idx] = best_score

    return mapping, confidence


def print_mapping(mapping: dict, confidence: dict) -> str:
    """Pretty-print the mapping for debugging."""
    lines = []
    for smpl_idx, smpl_joint in enumerate(SMPL_JOINT_NAMES):
        bone = mapping.get(smpl_idx)
        conf = confidence.get(smpl_idx, 0)
        status = "OK" if bone else "UNMAPPED"
        marker = "✓" if conf >= 80 else ("~" if conf >= 50 else "✗")
        lines.append(
            f"  {smpl_idx:2d} {smpl_joint:20s} → {str(bone):30s} "
            f"[{conf:3d}%] {marker}"
        )
    return "\n".join(lines)
