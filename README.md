# Video Mocap MCP — Blender addon

Vidéo → MediaPipe → armature Blender → retarget Rigify / Mixamo, pilotable
depuis Claude via BlenderMCP.

## Architecture

```
  ┌──────────────┐  MCP   ┌──────────────┐ socket ┌─────────────────────┐
  │ Claude (chat)│───────▶│ blender-mcp  │───────▶│  Blender (addon.py) │
  └──────────────┘        │   server     │        │  + video_mocap_mcp  │
                          └──────────────┘        └──────────┬──────────┘
                                                             │ subprocess
                                                             ▼
                                                   ┌──────────────────┐
                                                   │ external python  │
                                                   │  (mediapipe)     │
                                                   └──────────────────┘
                                                             │ JSON
                                                             ▼
                                                     landmarks.json
                                                             │
                                                             ▼
                                                Armature + animation
                                                             │
                                                             ▼
                                                   Retarget → Rigify/Mixamo
```

`video_mocap_mcp` est un addon Blender classique. BlenderMCP (ahujasid) a
déjà l'outil `execute_blender_code`, donc Claude peut appeler les opérateurs
de l'addon comme n'importe quel code Blender. Pas besoin d'écrire un serveur
MCP dédié.

## Prérequis

1. **Blender 3.6+** (testé 4.x aussi).
2. **Un Python externe avec mediapipe** (le Python embarqué de Blender n'est pas
   recommandé pour mediapipe). Au choix :
   ```bash
   python3 -m venv ~/mp_env
   source ~/mp_env/bin/activate        # Windows: ~\mp_env\Scripts\activate
   pip install mediapipe opencv-python
   ```
   Note le chemin de l'exécutable Python (`which python3` / `where python`),
   ou définis la variable d'env `VIDEO_MOCAP_PYTHON`.
3. **BlenderMCP** installé et fonctionnel (https://github.com/ahujasid/blender-mcp).

## Installation de l'addon

1. Zipper le dossier `video_mocap_mcp/` :
   ```bash
   cd /path/to/parent
   zip -r video_mocap_mcp.zip video_mocap_mcp
   ```
2. Blender → Edit → Preferences → Add-ons → Install → choisir le zip → cocher
   "Video Mocap MCP".
3. Dans la vue 3D, onglet `Mocap` de la sidebar (touche N).

## Utilisation manuelle (sans Claude)

1. Renseigner **Video** (chemin absolu vers ton MP4).
2. Renseigner **External Python** (chemin vers le Python qui a mediapipe).
3. (Optionnel) Choisir **Target rig** = Rigify ou Mixamo, et sélectionner
   l'objet armature cible.
4. Cliquer sur **Run full pipeline**.

## Utilisation via Claude (BlenderMCP)

Une fois BlenderMCP connecté, demande à Claude par exemple :

> "Sur ma vidéo `/Users/me/dance.mp4`, génère la mocap avec MediaPipe
> (complexity=2), puis retargette sur mon armature Rigify appelée `rig`."

Claude appellera `execute_blender_code` avec un snippet du genre :

```python
import bpy
p = bpy.context.scene.vmmcp
p.video_path = "/Users/me/dance.mp4"
p.python_exe = "/Users/me/mp_env/bin/python"
p.model_complexity = 2
p.smooth_landmarks = True
p.target_rig = 'RIGIFY'
p.target_armature = "rig"
bpy.ops.video_mocap.run_all()
```

Ou pas à pas :

```python
# Step 1: extract
bpy.ops.video_mocap.extract()
# Step 2: build MP armature
bpy.ops.video_mocap.build_armature()
# Step 3: retarget
bpy.ops.video_mocap.retarget()
```

## Opérateurs exposés

| Idname | Action |
|--------|--------|
| `video_mocap.extract` | Lance MediaPipe sur la vidéo, produit un JSON de landmarks |
| `video_mocap.build_armature` | Construit `MP_Armature` et bake l'animation des 33 landmarks |
| `video_mocap.retarget` | Retargette `MP_Armature` → Rigify / Mixamo via Copy Rotation + bake |
| `video_mocap.run_all` | Enchaîne les trois |

Propriétés dans `bpy.context.scene.vmmcp` :
- `video_path`, `landmarks_path`, `python_exe`, `fps`
- `model_complexity` (0/1/2), `min_detection_conf`, `smooth_landmarks`
- `target_rig` ('NONE' | 'RIGIFY' | 'MIXAMO'), `target_armature`

## Mapping des bones

Le MediaPipe Pose ne fournit pas les doigts ni la rotation intrinsèque du bras
autour de son axe (roll). Le retarget utilise des contraintes `Copy Rotation`
en espace LOCAL, ce qui respecte le roll de l'armature cible. Bones mappés :

- Torse : HIPS, SPINE, NECK
- Bras gauche/droit : upperarm, forearm, hand
- Jambes gauche/droite : upperleg, lowerleg, foot

Pas de doigts (MediaPipe Pose ne les donne pas — il faudrait combiner Hand
Landmarker).

## Limites connues

- Pas de reconstruction du roll des os longs (impossible depuis un seul flux).
- Mouvements rapides → lissage recommandé (`smooth_landmarks=True`).
- Profondeur (Z) de MediaPipe est bruyante, l'animation peut jitter en
  perspective → appliquer un filtre Butterworth via l'éditeur graphique après
  coup si besoin.
- Rigify : le mapping cible les contrôles FK. Si tu veux de l'IK, bake ensuite
  avec l'outil "Snap FK → IK" de Rigify.
- Mixamo : les noms de bones varient (`mixamorig:Hips` vs `mixamorig1:Hips`).
  Le retargeter fait un match par suffixe pour tolérer ça.

## Fichiers

- `__init__.py` — addon, opérateurs, panneau, propriétés
- `mediapipe_skeleton.py` — construction de l'armature MP + keyframing
- `retarget.py` — mapping + contraintes + bake
- `extractor/extract_pose.py` — script externe qui lance mediapipe
