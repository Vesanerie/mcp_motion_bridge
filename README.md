# Video Mocap MCP - Blender add-on

Add-on Blender pour preparer une scene de rigging/animation pilotee par Claude
via BlenderMCP.

Le but n'est pas de faire une mocap MediaPipe locale. Le flux attendu est :

```text
Videos / image sequence
        +
Mesh Blender
        +
6 cameras d'analyse: top, bottom, front, back, left, right
        |
        v
Video Mocap MCP add-on
        |
        v
Requete structuree pour Claude / BlenderMCP
        |
        v
Claude inspecte la scene Blender, cree le rig, puis anime les memes bones
```

## Pipeline

### 1. Rig Mesh

Le bouton `Rig Mesh` :

- prend le mesh choisi dans le panneau `Mocap`;
- cree ou met a jour les cameras `VMMCP_FRONT_Camera`,
  `VMMCP_BACK_Camera`, `VMMCP_LEFT_Camera`, `VMMCP_RIGHT_Camera`,
  `VMMCP_TOP_Camera`, `VMMCP_BOTTOM_Camera`;
- collecte les infos utiles du mesh : vertex count, polygons, bounding box,
  transforms, modifiers, materials, shape keys;
- genere une requete MCP dans un text block Blender nomme
  `VMMCP_Rig_Mesh_Request`;
- copie aussi cette requete dans le presse-papiers.

Cette requete demande a Claude, via BlenderMCP, d'inspecter le mesh et les six
vues camera, puis de creer une armature adaptee au mesh. Le mesh doit etre lie
aux bones crees, avec des controles utilisables quand c'est pertinent.

### 2. Animate

Le bouton `Animate` :

- reprend le meme mesh;
- utilise les videos ou la suite d'images fournies dans le panneau;
- cree ou met a jour le setup camera si l'option est active;
- genere une requete MCP dans `VMMCP_Animate_Request`;
- copie la requete dans le presse-papiers.

Cette requete demande a Claude d'analyser les references video/image et
d'animer le rig existant sur la plage de frames choisie. L'animation doit etre
bakee sur les bones du rig du mesh, pas sur une armature separee.

## Utilisation

1. Installer et activer BlenderMCP.
2. Installer cet add-on dans Blender.
3. Ouvrir la scene contenant le mesh a rigger.
4. Dans `View3D > Sidebar > Mocap`, choisir le mesh.
5. Renseigner les sources disponibles :
   - `Front`
   - `Back`
   - `Left`
   - `Right`
   - `Top`
   - `Bottom`
   - ou `Image Sequence`
6. Cliquer sur `Rig Mesh`.
7. Envoyer la requete generee a Claude dans la conversation connectee a
   BlenderMCP.
8. Une fois le rig cree, cliquer sur `Animate`.
9. Envoyer la seconde requete a Claude.

## Operateurs exposes

| Idname | Action |
|--------|--------|
| `video_mocap.setup_cameras` | Cree ou met a jour les six cameras d'analyse autour du mesh |
| `video_mocap.rig_mesh` | Prepare la requete MCP pour que Claude cree le rig du mesh |
| `video_mocap.animate` | Prepare la requete MCP pour que Claude anime le rig depuis les videos/images |

## Propriete importante

Les proprietes sont disponibles dans `bpy.context.scene.vmmcp` :

- `mesh_object`
- `front_video`, `back_video`, `left_video`, `right_video`
- `top_video`, `bottom_video`
- `image_sequence_dir`
- `frame_start`, `frame_end`
- `create_camera_setup`
- `camera_distance`
- `request_text_name`

## Limite MCP importante

Un add-on Blender ne peut pas forcer Claude a executer une action tout seul.
Dans l'architecture BlenderMCP, Claude est le client MCP et Blender expose des
outils. Cet add-on prepare donc la requete et le contexte de scene; Claude doit
ensuite utiliser BlenderMCP pour executer le rigging ou l'animation dans Blender.

## Fichiers

- `__init__.py` - add-on Blender, panneau, boutons, setup cameras, generation
  des requetes MCP.
- `mediapipe_skeleton.py`, `retarget.py`, `extractor/extract_pose.py` -
  anciens fichiers de prototype MediaPipe. Ils ne sont plus appeles par
  l'add-on principal.
