"""
SMPL constants for retargeting.

24 joints, canonical ordering, parent hierarchy, and synonym tables
for fuzzy bone matching against arbitrary rigs.
"""

# Canonical SMPL joint names (index = joint ID)
SMPL_JOINT_NAMES = [
    "pelvis",          # 0
    "left_hip",        # 1
    "right_hip",       # 2
    "spine1",          # 3
    "left_knee",       # 4
    "right_knee",      # 5
    "spine2",          # 6
    "left_ankle",      # 7
    "right_ankle",     # 8
    "spine3",          # 9
    "left_foot",       # 10
    "right_foot",      # 11
    "neck",            # 12
    "left_collar",     # 13
    "right_collar",    # 14
    "head",            # 15
    "left_shoulder",   # 16
    "right_shoulder",  # 17
    "left_elbow",      # 18
    "right_elbow",     # 19
    "left_wrist",      # 20
    "right_wrist",     # 21
    "left_hand",       # 22
    "right_hand",      # 23
]

# Parent index for each joint (-1 = root)
SMPL_PARENTS = [
    -1,  # 0  pelvis
     0,  # 1  left_hip
     0,  # 2  right_hip
     0,  # 3  spine1
     1,  # 4  left_knee
     2,  # 5  right_knee
     3,  # 6  spine2
     4,  # 7  left_ankle
     5,  # 8  right_ankle
     6,  # 9  spine3
     7,  # 10 left_foot
     8,  # 11 right_foot
     9,  # 12 neck
     9,  # 13 left_collar
     9,  # 14 right_collar
    12,  # 15 head
    13,  # 16 left_shoulder
    14,  # 17 right_shoulder
    16,  # 18 left_elbow
    17,  # 19 right_elbow
    18,  # 20 left_wrist
    19,  # 21 right_wrist
    20,  # 22 left_hand
    21,  # 23 right_hand
]

# Synonyms for fuzzy matching SMPL joint names to rig bone names.
# Each key is an SMPL joint; values are common rig bone names (case-insensitive).
# Left/right suffixes (.L/.R, _l/_r, Left/Right, _left/_right) are handled
# separately by the matching code — these entries use the LEFT form as template.
SYNONYMS = {
    "pelvis": [
        "pelvis", "hips", "hip", "root", "cog", "center_of_gravity",
        "torso", "spine",  # Rigify metarig uses "spine" for pelvis
    ],
    "left_hip": [
        "thigh.l", "upper_leg.l", "upperleg.l", "leg.l", "hip.l",
        "thigh_l", "upper_leg_l", "upleg.l",
    ],
    "right_hip": [
        "thigh.r", "upper_leg.r", "upperleg.r", "leg.r", "hip.r",
        "thigh_r", "upper_leg_r", "upleg.r",
    ],
    "spine1": [
        "spine.001", "spine1", "spine_01", "spine_1",
        "lower_back", "back",
    ],
    "spine2": [
        "spine.002", "spine2", "spine_02", "spine_2",
        "chest", "mid_back",
    ],
    "spine3": [
        "spine.003", "spine3", "spine_03", "spine_3",
        "upper_chest", "upper_back",
    ],
    "neck": [
        "spine.004", "neck", "neck.001", "neck_01", "neck1",
    ],
    "head": [
        "spine.005", "spine.006", "head", "head.001",
    ],
    "left_collar": [
        "shoulder.l", "clavicle.l", "collarbone.l", "collar.l",
    ],
    "right_collar": [
        "shoulder.r", "clavicle.r", "collarbone.r", "collar.r",
    ],
    "left_shoulder": [
        "upper_arm.l", "upperarm.l", "arm.l", "shoulder.l",
        "upper_arm_l", "uparm.l",
    ],
    "right_shoulder": [
        "upper_arm.r", "upperarm.r", "arm.r", "shoulder.r",
        "upper_arm_r", "uparm.r",
    ],
    "left_elbow": [
        "forearm.l", "lower_arm.l", "lowerarm.l", "elbow.l",
        "forearm_l", "loarm.l",
    ],
    "right_elbow": [
        "forearm.r", "lower_arm.r", "lowerarm.r", "elbow.r",
        "forearm_r", "loarm.r",
    ],
    "left_wrist": [
        "hand.l", "wrist.l", "hand_l",
    ],
    "right_wrist": [
        "hand.r", "wrist.r", "hand_r",
    ],
    "left_hand": [
        "palm.l", "hand_end.l",
    ],
    "right_hand": [
        "palm.r", "hand_end.r",
    ],
    "left_knee": [
        "shin.l", "calf.l", "lower_leg.l", "lowerleg.l", "knee.l",
        "shin_l", "loleg.l",
    ],
    "right_knee": [
        "shin.r", "calf.r", "lower_leg.r", "lowerleg.r", "knee.r",
        "shin_r", "loleg.r",
    ],
    "left_ankle": [
        "foot.l", "ankle.l", "foot_l",
    ],
    "right_ankle": [
        "foot.r", "ankle.r", "foot_r",
    ],
    "left_foot": [
        "toe.l", "ball.l", "toes.l", "foot_end.l",
    ],
    "right_foot": [
        "toe.r", "ball.r", "toes.r", "foot_end.r",
    ],
}

# SMPL reference height (pelvis to head top) in meters
SMPL_REF_HEIGHT = 1.70
