# Video Mocap MCP - Blender add-on

Add-on Blender pour preparer une scene de rigging/animation pilotee par Claude
via BlenderMCP.

BlenderMCP est indispensable. MCP_motion_bridge ne peut pas fonctionner seul :
il prepare le contexte et les requetes, mais Claude doit executer les actions
dans Blender via BlenderMCP.

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
- reutilise les cameras deja presentes dans la scene et ne rajoute aucune
  camera si au moins une camera existe deja;
- cree les cameras `VMMCP_FRONT_Camera`, `VMMCP_BACK_Camera`,
  `VMMCP_LEFT_Camera`, `VMMCP_RIGHT_Camera`, `VMMCP_TOP_Camera`,
  `VMMCP_BOTTOM_Camera` uniquement si la scene ne contient aucune camera;
- place ou ajuste les cameras pour cadrer le mesh entier;
- collecte les infos utiles du mesh : vertex count, polygons, bounding box,
  transforms, modifiers, materials, shape keys;
- transmet a Claude le type de rig cible et le nombre de bones demande;
- genere une requete MCP dans un text block Blender nomme
  `VMMCP_Rig_Mesh_Request`;
- copie aussi cette requete dans le presse-papiers;
- exporte cette requete en `.txt` pour pouvoir la coller plus tard dans une
  nouvelle conversation Claude.

Cette requete demande a Claude, via BlenderMCP, d'inspecter le mesh et les six
vues camera, puis de creer une armature adaptee au mesh. Le mesh doit etre lie
aux bones crees, avec des controles utilisables quand c'est pertinent.

## Cameras

L'add-on ne cree pas de nouvelles cameras si la scene contient deja des cameras.
Il les reutilise dans l'ordre suivant :

- une camera dont le nom contient `front`, `back`, `left`, `right`, `top` ou
  `bottom` est associee a cette vue;
- les autres cameras existantes sont assignees aux vues restantes dans l'ordre
  alphabetique;
- s'il n'existe aucune camera, l'add-on cree les six cameras `VMMCP_*`.

Quand une requete est envoyee a Claude, elle lui demande explicitement de
repositionner, orienter et regler la focale ou l'orthographic scale des cameras
listees afin que le mesh soit visible en entier pendant toute l'animation.

## Nombre de bones

L'utilisateur choisit :

- `Rig Target` : `Custom`, `Rigify` ou `Unreal`;
- `Bones` : nombre de bones cible pour l'armature.

Le nombre de bones est transmis a Claude comme une contrainte de production.
Claude peut l'ajuster legerement si la topologie du mesh l'exige, mais la
requete lui demande de respecter l'intention.

Guide de base :

| Usage | Recommandation |
|-------|----------------|
| Prop simple ou objet rigide | 8-25 bones |
| Humanoide Unreal / game engine | 50-70 deformation bones |
| Humanoide Rigify | 80-120 bones, controles inclus |
| Creature ou personnage complexe | 90-180 bones selon l'anatomie |
| Visage/mains tres detailles | 120+ bones ou combinaison bones + shape keys |

Pour Unreal, privilegier une hierarchie stable, un root clair, un pelvis separe,
et des noms compatibles export. Pour Rigify, privilegier des controles FK/IK
propres et une deformation anatomiquement coherente.

### 2. Animate

Le bouton `Animate` :

- reprend le meme mesh;
- utilise les videos ou la suite d'images fournies dans le panneau;
- cree ou met a jour le setup camera si l'option est active;
- genere une requete MCP dans `VMMCP_Animate_Request`;
- copie la requete dans le presse-papiers;
- exporte cette requete en `.txt`.

Cette requete demande a Claude d'analyser les references video/image et
d'animer le rig existant sur la plage de frames choisie. L'animation doit etre
bakee sur les bones du rig du mesh, pas sur une armature separee.

La requete d'animation impose aussi deux regles :

- les objets, personnages, accessoires, decors ou mouvements qui ne sont pas
  presents dans les references fournies doivent etre ignores;
- Claude doit comparer les poses animees avec chaque angle disponible
  (`front`, `back`, `left`, `right`, `top`, `bottom`) depuis les cameras
  `VMMCP_*`, puis corriger la pose si un angle contredit un autre.

## Sauvegarde de la requete

Les boutons `Rig Mesh` et `Animate` creent un prompt complet pret a coller dans
une nouvelle conversation Claude. Ce prompt rappelle a Claude qu'il doit etre
connecte a Blender via BlenderMCP avant d'executer le travail.

L'add-on garde trois copies de la derniere requete :

- dans le presse-papiers;
- dans un text block Blender (`VMMCP_Rig_Mesh_Request` ou
  `VMMCP_Animate_Request`);
- dans un fichier `.txt`.

Le bouton `Copy Request to txt` reexporte manuellement la derniere requete si
l'utilisateur a ferme ou oublie d'ouvrir une conversation Claude.

## Utilisation

1. Installer et activer BlenderMCP. C'est obligatoire.
2. Installer cet add-on dans Blender.
3. Ouvrir la scene contenant le mesh a rigger.
4. Dans `View3D > Sidebar > Mocap`, choisir le mesh.
5. Choisir `Rig Target` et le nombre de `Bones`.
6. Renseigner les sources disponibles :
   - `Front`
   - `Back`
   - `Left`
   - `Right`
   - `Top`
   - `Bottom`
   - ou `Image Sequence`
7. Cliquer sur `Rig Mesh`.
8. Envoyer la requete generee a Claude dans la conversation connectee a
   BlenderMCP.
9. Une fois le rig cree, cliquer sur `Animate`.
10. Envoyer la seconde requete a Claude.

## Operateurs exposes

| Idname | Action |
|--------|--------|
| `video_mocap.setup_cameras` | Cree ou met a jour les six cameras d'analyse autour du mesh |
| `video_mocap.rig_mesh` | Prepare la requete MCP pour que Claude cree le rig du mesh |
| `video_mocap.animate` | Prepare la requete MCP pour que Claude anime le rig depuis les videos/images |
| `video_mocap.copy_request_to_txt` | Exporte la derniere requete en fichier `.txt` |

## Propriete importante

Les proprietes sont disponibles dans `bpy.context.scene.vmmcp` :

- `mesh_object`
- `front_video`, `back_video`, `left_video`, `right_video`
- `top_video`, `bottom_video`
- `image_sequence_dir`
- `frame_start`, `frame_end`
- `rig_preset`, `requested_bone_count`
- `create_camera_setup`
- `camera_distance`
- `request_text_name`
- `request_txt_path`

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
