# Video Mocap MCP - Blender add-on

Add-on Blender pour la capture de mouvement video pilotee par Claude via
BlenderMCP.

BlenderMCP est indispensable. MCP_motion_bridge ne peut pas fonctionner seul :
il prepare le contexte et les requetes, mais Claude doit executer les actions
dans Blender via BlenderMCP.

## Architecture

```
Video source (YouTube, film, danse, sport)
       |
       v
estimator/run_smpl.py  (Python externe, TRAM ou 4D-Humans)
       |
       v
motion_data.npz  (24 joints x rotations axis-angle + translation + shape)
       |
       v
__init__.py  (addon Blender, genere requete MCP structuree)
       |
       v
Claude via BlenderMCP
  1. Rig Mesh : inspecte mesh + 6 cameras, cree armature SMPL-compatible
  2. Animate  : lit rotations SMPL, retarget sur le rig, verif multi-angle
```

## Arbre de decision : quel estimateur utiliser ?

| Situation | Estimateur | Raison |
|-----------|------------|--------|
| Video quelconque (YouTube, film, sport, camera mobile) | **TRAM** (obligatoire) | Etat de l'art ECCV 2024, SLAM camera |
| Video simple (camera fixe, eclairage controle) | **4D-Humans (HMR2)** | Plus leger, bon pour poses statiques |
| Test rapide, video tres courte, pas besoin de precision | **MediaPipe** (fallback degrade) | 33 landmarks, z bruite |

## Pourquoi SMPL et pas MediaPipe ?

MediaPipe Pose extrait 33 keypoints independants. Problemes :
- Le Z (profondeur) est tres bruite depuis une seule camera
- Les longueurs d'os "respirent" (varient de ~5% par frame)
- Pas de rotations : il faut les deduire des positions, fragile
- Poses anatomiquement impossibles non filtrees

SMPL (Skinned Multi-Person Linear Model) resout tout ca :
- Sortie directe en rotations d'articulations (24 joints)
- Modele parametrique : longueurs d'os constantes par construction
- Contraintes anatomiques integrees
- Les rotations sont exactement ce que consomme un rig Blender

## Prerequis

### Python externe (estimateur SMPL)

L'estimateur tourne dans un process Python separe (pas dans Blender).

```bash
python3.11 -m venv ~/smpl_env
source ~/smpl_env/bin/activate

# PyTorch (adapte a ton GPU/CPU)
pip install torch torchvision

# TRAM (recommande — meilleure qualite)
git clone https://github.com/yufu-wang/tram
cd tram && pip install -e .

# OU 4D-Humans (alternative plus legere)
pip install hmr2

# Dependencies communes
pip install opencv-python numpy scipy
```

### Blender
- Blender 3.6+ (teste 4.x)
- BlenderMCP installe et fonctionnel (https://github.com/ahujasid/blender-mcp)

## Installation de l'addon

1. Zipper le dossier avec `__init__.py` + `estimator/` :
   ```bash
   zip -r video_mocap_mcp.zip video_mocap_mcp/
   ```
2. Blender → Edit → Preferences → Add-ons → Install → choisir le zip
3. Dans View3D, onglet `Mocap` de la sidebar (touche N)

## Pipeline SMPL (recommande)

1. **Mesh** : selectionner le mesh a animer
2. **Video** : chemin vers la video source du mouvement
3. **Method** : SMPL (TRAM/HMR2)
4. **External Python** : chemin vers le Python avec TRAM/HMR2
5. **Rig Target** : SMPL 24-joint (pour correspondance directe)
6. **Extract Motion** : lance l'estimateur, produit un `.npz`
7. **Rig Mesh** : genere la requete MCP → coller dans Claude
8. Claude cree l'armature via BlenderMCP
9. **Animate** : genere la requete MCP → coller dans Claude
10. Claude applique les rotations SMPL au rig

## Nombre de bones

| Rig Target | Bones | Usage |
|------------|-------|-------|
| SMPL 24-joint | 24 | Correspondance directe SMPL (recommande) |
| Custom | 65 | General, a ajuster |
| Rigify | 80-120 | Blender avec controles FK/IK |
| Unreal | 50-70 | Export game engine |

## Role des 6 cameras

Les cameras VMMCP_* ne font PAS de stereo sur la video source
(la video est monoculaire). Elles servent a :

1. Donner a Claude un contexte visuel du mesh Blender pour le rigging
2. Verifier l'animation depuis chaque angle apres application
3. Detecter les contradictions (bras qui traverse le torse vu de profil)

## Pieges geres dans les requetes MCP

Les requetes generees pour Claude mentionnent explicitement :

1. **Coordinate system** : SMPL Y-up → Blender Z-up (via scipy)
2. **Rest pose mismatch** : SMPL T-pose vs rig custom → offset compose
3. **Foot skating** : IK sur les pieds en contact sol
4. **Lissage** : quaternions SLERP, jamais Euler (gimbal lock)
5. **Verification multi-angle** : Claude inspecte depuis les 6 cameras

## Operateurs exposes

| Idname | Action |
|--------|--------|
| `video_mocap.extract` | Lance TRAM/HMR2/MediaPipe, produit .npz ou .json |
| `video_mocap.rig_mesh` | Genere requete MCP pour le rigging |
| `video_mocap.animate` | Genere requete MCP pour l'animation |
| `video_mocap.run_all` | Enchaine extract + rig_mesh |
| `video_mocap.setup_cameras` | Place les 6 cameras autour du mesh |
| `video_mocap.copy_request_to_txt` | Exporte la requete en .txt |

## Proprietes (bpy.context.scene.vmmcp)

- `mesh_object`, `video_path`
- `estimation_method`, `python_exe`, `smpl_method`
- `front_video`, `back_video`, `left_video`, `right_video`, `top_video`, `bottom_video`
- `image_sequence_dir`
- `rig_preset`, `requested_bone_count`
- `frame_start`, `frame_end`
- `create_camera_setup`, `camera_distance`
- `motion_data_path`, `request_text_name`, `request_txt_path`

## Fichiers

- `__init__.py` — addon Blender, UI, operateurs, generation requetes MCP
- `estimator/run_smpl.py` — script externe TRAM/HMR2, produit .npz
- `_fallback/` — anciens fichiers MediaPipe (prototype, degrade)
- `PROGRESS.md` — suivi de progression du projet
