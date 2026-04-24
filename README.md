# Video Mocap MCP - Branche `minor-tweaks`

Raffinements du workflow de l'add-on Blender pour le rigging/animation pilote par
Claude via BlenderMCP. Cette branche ne contient **pas** la technologie SMPL/TRAM/HMR2
(voir la branche `smpl-pipeline`).

BlenderMCP est indispensable. MCP_motion_bridge ne peut pas fonctionner seul :
il prepare le contexte et les requetes, mais Claude doit executer les actions
dans Blender via BlenderMCP.

## Objet de cette branche

Les modifications Hors Techno apportees ici sont :

- **Identification stable du mesh cible** via `vmmcp_target_id` + `target_contract`
- **Reutilisation intelligente des cameras** existantes dans la scene
- **Payload enrichi** : dimensions, parent, enfants, armature modifiers, vertex groups
- **Prompts MCP renforces** : contraintes target_contract, framing cameras, adaptation mesh

Aucune nouvelle dependance, aucun estimateur externe, aucun operateur supplementaire.

## Pipeline

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

## Regle de maintenance documentation

Toute modification de comportement de l'add-on doit etre accompagnee d'une mise
a jour de ce `README.md`. La requete MCP contient aussi une section
`documentation_policy` pour rappeler cette regle a Claude.

## Identification du mesh cible

La pipeline ne doit jamais laisser Claude deviner quel objet animer. Le mesh
cible est choisi dans `mesh_object`, puis l'add-on :

- verifie que l'objet existe et que son type est `MESH`;
- ajoute un custom property `vmmcp_target_id` (UUID stable) si le mesh n'en a
  pas encore;
- envoie dans le payload `target_contract.mesh_object_name`,
  `target_contract.mesh_data_name` et `target_contract.mesh_target_id`;
- ajoute dans `mesh` les dimensions, bounding box, parent, enfants, modifiers,
  armature modifiers, vertex groups, materials et shape keys.

Claude doit d'abord confirmer ce mesh par nom exact et `vmmcp_target_id`, puis
ignorer les autres meshes sauf s'ils servent explicitement de references. Le rig,
le binding et les keyframes doivent etre appliques a ce mesh cible uniquement.

## Cameras

L'add-on ne cree pas de nouvelles cameras si la scene contient deja des cameras.
Il les reutilise dans l'ordre suivant :

- une camera dont le nom contient `front`, `back`, `left`, `right`, `top` ou
  `bottom` est associee a cette vue;
- les autres cameras existantes sont assignees aux vues restantes dans l'ordre
  alphabetique;
- s'il n'existe aucune camera, l'add-on cree les six cameras `VMMCP_*`.

Les cameras sont placees en mode orthographique, `ortho_scale` adapte au mesh,
focale 50 mm. Quand une requete est envoyee a Claude, elle lui demande
explicitement de repositionner et regler les cameras listees afin que le mesh
soit visible en entier pendant toute l'animation.

## Pipeline detail

### 1. Rig Mesh

Le bouton `Rig Mesh` :

- prend le mesh choisi dans le panneau `Mocap`;
- marque ce mesh avec un identifiant stable `vmmcp_target_id` si necessaire;
- transmet a Claude un `target_contract` indiquant le nom exact du mesh,
  son data-block et son identifiant;
- reutilise les cameras deja presentes dans la scene et ne rajoute aucune
  camera si au moins une camera existe deja;
- cree les cameras `VMMCP_*` uniquement si la scene ne contient aucune camera;
- place ou ajuste les cameras pour cadrer le mesh entier;
- collecte les infos utiles du mesh : vertex count, polygons, dimensions,
  bounding box, transforms, parent, enfants, modifiers, armature modifiers,
  vertex groups, materials, shape keys;
- transmet a Claude le type de rig cible et le nombre de bones demande;
- genere une requete MCP dans un text block Blender nomme
  `VMMCP_Rig_Mesh_Request`;
- copie aussi cette requete dans le presse-papiers;
- exporte cette requete en `.txt` pour pouvoir la coller plus tard dans une
  nouvelle conversation Claude.

Cette requete demande a Claude, via BlenderMCP, d'inspecter le mesh et les vues
camera disponibles, puis de creer une armature adaptee au mesh. Le mesh doit
etre lie aux bones crees, avec des controles utilisables quand c'est pertinent.

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
  listees, puis corriger la pose si un angle contredit un autre.

## Adaptation de l'animation au mesh

La requete MCP demande a Claude d'adapter l'animation a la morphologie du mesh,
pas d'appliquer une animation generique brute :

- placer les bones dans le volume du mesh cible;
- adapter les longueurs d'os aux proportions du mesh;
- inspecter les armature modifiers et vertex groups existants avant de les
  remplacer;
- verifier la deformation sur les epaules, hanches, coudes, genoux, poignets,
  chevilles et root motion.

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

## Nombre de bones

L'utilisateur choisit :

- `Rig Target` : `Custom`, `Rigify` ou `Unreal`;
- `Bones` : nombre de bones cible pour l'armature.

| Usage | Recommandation |
|-------|----------------|
| Prop simple ou objet rigide | 8-25 bones |
| Humanoide Unreal / game engine | 50-70 deformation bones |
| Humanoide Rigify | 80-120 bones, controles inclus |
| Creature ou personnage complexe | 90-180 bones selon l'anatomie |
| Visage/mains tres detailles | 120+ bones ou combinaison bones + shape keys |

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
| `video_mocap.setup_cameras` | Cree ou met a jour les cameras d'analyse autour du mesh |
| `video_mocap.rig_mesh` | Prepare la requete MCP pour que Claude cree le rig du mesh |
| `video_mocap.animate` | Prepare la requete MCP pour que Claude anime le rig depuis les videos/images |
| `video_mocap.copy_request_to_txt` | Exporte la derniere requete en fichier `.txt` |

## Proprietes

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
- custom property mesh : `vmmcp_target_id`

## Limite MCP importante

Un add-on Blender ne peut pas forcer Claude a executer une action tout seul.
Dans l'architecture BlenderMCP, Claude est le client MCP et Blender expose des
outils. Cet add-on prepare donc la requete et le contexte de scene; Claude doit
ensuite utiliser BlenderMCP pour executer le rigging ou l'animation dans Blender.

## Fichiers

- `__init__.py` - add-on Blender, panneau, boutons, setup cameras, generation
  des requetes MCP.
- `extractor/extract_pose.py` - prototype MediaPipe multi-vues (non appele par
  l'add-on principal).
- `mediapipe_skeleton.py`, `retarget.py` - anciens prototypes MediaPipe.

## Autres branches

- `main` / `smpl-pipeline` : integration du pipeline Vesanerie (TRAM/HMR2)
  produisant un `.npz` SMPL comme source de mouvement principale.
- `minor-tweaks` (cette branche) : raffinements Hors Techno uniquement.
