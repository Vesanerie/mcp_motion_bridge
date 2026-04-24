# Video Mocap MCP - Blender add-on

Add-on Blender pour la capture de mouvement video pilotee par Claude Code
via BlenderMCP.

## Comment ca marche

1. Dans Blender : selectionne ton mesh, mets tes videos de reference, clique **Generate Prompt**
2. Colle le prompt dans **Claude Code** connecte a BlenderMCP
3. Claude Code fait tout : extraction mouvement, rigging, animation, verification

## Architecture

```
Video(s) de reference (front, back, left, right...)
       |
       v
estimator/run_mediapipe_ik.py  (Python externe, ~/mp_env)
       |  MediaPipe extrait les landmarks
       |  IK calcule les rotations articulaires
       v
motion_data.npz  (24 joints x rotations axis-angle)
       |
       v
Claude Code via BlenderMCP
  - Cree armature 24 bones sur le mesh
  - Applique les rotations frame par frame
  - Lisse en quaternions
  - Corrige le foot skating
  - Verifie depuis 6 cameras
```

## Prerequis

### Python externe (estimateur)

```bash
python3.11 -m venv ~/mp_env
source ~/mp_env/bin/activate
pip install mediapipe opencv-python numpy scipy
```

### Blender
- Blender 3.6+ (teste 4.x)
- BlenderMCP installe (https://github.com/ahujasid/blender-mcp)

## Installation

1. Blender → Edit → Preferences → Add-ons → Install → `video_mocap_mcp.zip`
2. Activer "Video Mocap MCP"
3. Onglet `Mocap` dans la sidebar (touche N)

## Utilisation

1. **Mesh** : selectionne ou choisis ton personnage 3D
2. **Videos** : mets tes videos dans les slots (front, back, left, right...)
   - Pas besoin de remplir les 6, mets ce que tu as
   - Au minimum une video
3. **Generate Prompt** : clique le bouton
4. **Colle** le prompt dans Claude Code connecte a BlenderMCP
5. Claude Code fait tout le reste

## Role des 6 cameras

Les cameras VMMCP_* sont placees automatiquement autour du mesh.
Elles ne font PAS de stereo sur la video source. Elles servent a :

1. Donner a Claude un contexte visuel du mesh pour le rigging
2. Verifier l'animation depuis chaque angle
3. Detecter les contradictions (bras qui traverse le torse)

## Qualite du mouvement

L'estimateur extrait des **rotations articulaires** (pas des positions
brutes) via MediaPipe + Inverse Kinematics. Le prompt demande a Claude :

- Conversion coordonnees MediaPipe → Blender (Y-down → Z-up)
- Gestion du rest pose offset (T-pose vs pose du rig)
- Lissage en quaternions SLERP (jamais Euler)
- Correction du foot skating (IK sur les pieds)
- Verification multi-angle depuis les 6 cameras

## Fichiers

- `__init__.py` — addon Blender, UI simplifiee, generation du prompt
- `estimator/run_mediapipe_ik.py` — extraction MediaPipe + IK → .npz
- `estimator/smpl_output.py` — format .npz canonique
- `estimator/run_4dhumans.py` — reserve (HMR2, si installable)
- `estimator/run_smpl.py` — reserve (TRAM, si CUDA disponible)
- `_fallback/` — anciens prototypes MediaPipe
- `PROGRESS.md` — suivi du projet
