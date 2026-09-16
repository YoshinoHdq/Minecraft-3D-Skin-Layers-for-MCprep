<p align="center">
  <img src="head_3d_skin_layer/icons/logo.png" alt="Minecraft 3D Skin Layers for MCprep" width="220">
</p>

<h1 align="center">Minecraft 3D Skin Layers for MCprep</h1>

<p align="center">
  <strong>A Blender add-on that generates Minecraft-style 3D skin layers for MCprep player models.</strong><br>
  Version 1.0.0 &nbsp;·&nbsp; Blender 5.2.0+
</p>

---

## Overview

**Minecraft 3D Skin Layers for MCprep** is a third-party Blender add-on that builds a genuine
per-pixel voxelised 3D second skin layer on top of an MCprep Minecraft player model — the Blender
equivalent of the Minecraft Java mod *3D Skin Layers*.

One small cube is emitted per opaque skin texel, and the faces hidden between neighbouring cubes are
culled. The result is a real voxel shell that follows the rig, not a duplicated or solidified copy of
MCprep's own Layer2 mesh.

> This is an **independent, third-party add-on**. It is not an official Minecraft, Mojang, or MCprep
> product, and it is not affiliated with or endorsed by them.

## Features

- Minecraft-style 3D skin layers generated from the skin texture
- MCprep player model support
- Support for the **Simple Player** and **Simple Player Slim** player models from MCprep
- Simple Player Slim, which has narrower arms, is detected automatically
- Transparent skin support — transparent texels produce no geometry
- Automatic player detection from the current selection
- Head / Body / Arms / Legs control — generate just the parts you need
- Rebuild generated layers
- Delete generated layers
- Selection-aware generation — only the selected player is affected
- Support for duplicated rigs (`SimplePlayer.001`, `SimplePlayer.002`, …)
- Non-destructive: MCprep's Layer1 / Layer2 / armature / materials / images are never modified
- Status feedback and user-friendly error messages

## Requirements

- **Blender 5.2.0** or newer
- **MCprep** add-on
- An MCprep-generated Minecraft player model

## Installation

1. Open Blender.
2. Go to **Edit → Preferences → Add-ons**.
3. Click **Install…** and select `minecraft_3d_skin_layers-v1.0.0.zip`.
4. Enable the add-on **Minecraft 3D Skin Layers for MCprep** in the list.

## Usage

1. Use **MCprep** to create a Minecraft player model in your scene.
2. Select the player model (or any of its parts).
3. Open the 3D Viewport sidebar with **N**.
4. Open the **MC 3D Skin Layers** tab.
5. Click **生成 3D 外层** (Generate 3D Skin Layers).

The add-on detects the selected player automatically and generates the 3D layers for it. If the same
model is on screen more than once, only the player you selected is affected.

## Body Parts

The **BODY PARTS** toggles let you choose which layers to build:

| Part | Default |
|------|---------|
| Head | enabled |
| Body | enabled |
| Arms | enabled |
| Legs | enabled |

All four are enabled by default. Turning one off simply skips that part — the others are untouched.

- **生成 3D 外层** — build the layers that are missing
- **重建 3D 外层** — remove this player's generated layers and build them again
- **删除 3D 外层** — remove this player's generated layers only

Deleting removes only the objects this add-on created. MCprep's Layer1 / Layer2 meshes, the armature,
the materials and the skin images are never touched.

## Compatibility

Designed for **MCprep-generated Minecraft player models** — the **Simple Player** and
**Simple Player Slim** models.

Only MCprep player models have been tested. Other Minecraft model importers or manually assembled
blocky models are not supported.

## Version

v1.0.0

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Credits

Developed by Yoshino. The voxel algorithm follows the Minecraft Java mod *3D Skin Layers*
(`skinlayers3d`).
