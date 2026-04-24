# Video Mocap MCP - Suivi de progression

## Architecture actuelle

```
Video source (YouTube, film, danse...)
       |
       v
estimator/run_mediapipe_ik.py  (Python externe, mediapipe + IK)
       |
       v
motion_data.npz  (24 joints x rotations axis-angle + translation)
       |
       v
__init__.py  (addon Blender, genere prompt pour Claude Code)
       |
       v
Claude Code via BlenderMCP
  - Lit le .npz
  - Cree armature 24 bones SMPL sur le mesh
  - Applique les rotations frame par frame
  - Lisse en quaternions, corrige foot skating
  - Verifie depuis les 6 cameras
```

## Decisions techniques

| Decision | Choix | Raison |
|----------|-------|--------|
| Estimateur principal | MediaPipe + IK analytique | Fonctionne sur macOS Apple Silicon, pas de CUDA |
| Estimateur premium (futur) | TRAM cloud | CUDA requis, prevu en remote GPU |
| Estimateur abandonne | 4D-Humans (HMR2) | chumpy incompatible Python 3.11 |
| Format sortie | .npz | Portable, lisible numpy, meme format SMPL |
| Paradigme mouvement | Rotations axis-angle 24 joints | Compatible rig Blender directement |
| 6 cameras | Analyse mesh + verif animation | PAS de stereo sur la video source |
| Lissage | Quaternions (slerp/log-quat) | Jamais Euler (gimbal lock) |
| Plateforme | macOS Apple Silicon | Pas de CUDA, MPS non suffisant pour HMR2 |
| UI | Un seul bouton "Generate Prompt" | L'utilisateur met ses videos, clique, colle dans Claude Code |

## Historique

### 2026-04-24 - Iteration 1 : Setup initial
- [x] Repo GitHub cree (Vesanerie/mcp_motion_bridge)
- [x] Addon v0.1 : pipeline MediaPipe mono-vue
- [x] Addon v0.2 : requetes MCP pures (sans MediaPipe local)
- [x] Addon v0.3 : multi-view MediaPipe + triangulation
- [x] Venv Python 3.11 avec mediapipe installe

### 2026-04-24 - Iteration 2 : Passage SMPL
- [x] PROGRESS.md cree
- [x] Anciens fichiers deplaces dans _fallback/
- [x] estimator/run_smpl.py (wrapper TRAM + HMR2 fallback)
- [x] __init__.py refonte pipeline SMPL (merge avec features v0.2.2)
- [x] Fallback MediaPipe dans l'UI (avec warning degrade)
- [x] README mis a jour (arbre de decision, architecture, pieges)
- [x] Zip addon + test Blender

### 2026-04-24 - Iteration 3 : macOS Apple Silicon
- [x] Contrainte plateforme : pas de CUDA, MPS only
- [x] estimator/run_4dhumans.py (HMR2, MPS compatible)
- [x] estimator/smpl_output.py (format .npz canonique)
- [x] estimator/README.md (setup env macOS + Linux)
- [x] Prompt mis a jour : 4D-Humans local, TRAM cloud coming soon
- [x] UI : infos estimateur dans le panel

### 2026-04-24 - Iteration 4 : MediaPipe + IK (solution qui marche)
- [x] HMR2 echoue a l'install (chumpy incompatible Python 3.11)
- [x] estimator/run_mediapipe_ik.py : MediaPipe landmarks → rotations via IK
- [x] Lissage quaternion integre (window=5)
- [x] Sort .npz meme format que SMPL (24 joints, axis-angle)
- [x] scipy installe dans ~/mp_env
- [x] Prompt et UI mis a jour
- [x] UI simplifiee : un seul bouton Generate Prompt

## Contrainte plateforme

macOS Apple Silicon (M1/M2/M3/M4) :
- Pas de CUDA → TRAM et WHAM impossibles en local
- MPS (Metal) insuffisant pour HMR2 (chumpy bloque)
- Solution : MediaPipe (pur CPU, compatible partout) + IK pour rotations

## Env Python

```
~/mp_env (Python 3.11)
├── mediapipe
├── opencv-python
├── numpy
└── scipy
```

## Pieges documentes

1. **Coordinate system** : MediaPipe Y-down, Blender Z-up → conversion dans le prompt
2. **Rest pose mismatch** : T-pose reference vs rig custom → offset compose
3. **Foot skating** : contraintes IK pieds en post-traitement
4. **Longueurs d'os** : IK ne les garantit pas → contrainte explicite dans le prompt
5. **Lissage temporel** : quaternions slerp, jamais Euler
6. **Profondeur MediaPipe** : bruitee en mono → lissage + verif multi-angle

## Fichiers

```
mcp_motion_bridge/
├── __init__.py                         # addon Blender v0.7, UI + prompt
├── estimator/
│   ├── __init__.py
│   ├── run_mediapipe_ik.py             # PRINCIPAL : MediaPipe + IK → .npz
│   ├── run_4dhumans.py                 # reserve si HMR2 installable un jour
│   ├── run_smpl.py                     # reserve si TRAM/CUDA dispo
│   ├── smpl_output.py                  # format .npz canonique
│   └── README.md                       # setup env
├── _fallback/
│   ├── mediapipe_skeleton.py           # ancien prototype
│   ├── retarget.py                     # ancien prototype
│   └── extractor/extract_pose.py       # ancien prototype
├── video_mocap_mcp.zip                 # addon pret a installer
├── PROGRESS.md                         # ce fichier
└── README.md                           # doc utilisateur
```
