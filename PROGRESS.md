# Video Mocap MCP - Suivi de progression

## Architecture cible

```
Video source (YouTube, film, danse...)
       |
       v
estimator/run_smpl.py  (Python externe, TRAM ou 4D-Humans)
       |
       v
motion_data.npz  (theta 72 rotations, beta 10 shape, tau translation, camera)
       |
       v
__init__.py  (addon Blender, lit le .npz, genere requete MCP)
       |
       v
Claude via BlenderMCP
  - Rig Mesh : inspecte mesh + 6 cameras, cree armature
  - Animate  : lit rotations SMPL, retarget sur le rig, boucle verif 6 cameras
```

## Decisions techniques

| Decision | Choix | Raison |
|----------|-------|--------|
| Estimateur par defaut | TRAM (ECCV 2024) | Etat de l'art in-the-wild, SLAM camera |
| Estimateur fallback | 4D-Humans (HMR2.0) | Plus leger, bon sur poses statiques |
| Fallback simple | MediaPipe | Videos courtes, eclairage controle |
| Format sortie | .npz | Portable, pas de risque pickle, lisible numpy |
| Paradigme mouvement | SMPL rotations | Pas de landmarks 33 pts, rotations = ce que mange un rig |
| 6 cameras | Analyse mesh + verif animation | PAS de stereo sur la video source |
| Lissage | Quaternions (slerp/log-quat) | Jamais Euler (gimbal lock) |

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
- [x] Ancien run_smpl.py conserve (TRAM si CUDA dispo)

## Pieges documentes

1. **Coordinate system** : SMPL Y-up, Blender Z-up → scipy.spatial.transform
2. **Rest pose mismatch** : SMPL T-pose vs rig A-pose → composer offset
3. **Foot skating** : contraintes IK pieds en post-traitement
4. **Longueurs d'os** : invariantes dans le temps (SMPL les garantit)
5. **Lissage temporel** : quaternions slerp, jamais Euler

## Arbre de decision estimateur

```
Video quelconque (YouTube, film, sport)
  → Camera mobile, occlusions, mouvement complexe
  → TRAM (obligatoire)

Video perso, eclairage controle, mouvement simple
  → Camera fixe, sujet bien visible
  → 4D-Humans (recommande) ou MediaPipe (fallback degrade)
```
