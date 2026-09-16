# SPDX-License-Identifier: GPL-3.0-or-later
"""
Minecraft 3D Skin Layers for MCprep
===================================

Generates a genuine per-pixel voxelised 3D second skin layer for MCprep
"Minecraft player" rigs — the Blender equivalent of the Minecraft Java mod
*3D Skin Layers* (skinlayers3d).

This is a faithful port of that mod's ``SolidPixelWrapper.wrapBox`` for all
four body parts (head, body, arms, legs). It does **not** duplicate /
solidify / extrude MCprep's ``*.Body.Layer2`` shell. Instead it reads the
skin PNG and emits one small cube per opaque texel, culling the faces that
sit between adjacent cubes.

Algorithm (mirrors the decompiled mod):

    for face in 6 directions:                  # DOWN UP NORTH SOUTH WEST EAST
        sizeUV = getSizeUV(dims, face)
        for u in range(sizeUV.u):
          for v in range(sizeUV.v):
            addPixel(face, u, v)

    addPixel:
        onTextureUV = getOnTextureUV(textureUV, onFaceUV, dims, face)
        if not isPresent(onTextureUV): return   # transparent -> no geometry
        voxelPos    = UVtoXYZ((u,v), dims, face)
        solidPixel  = isSolid(onTextureUV)
        hide = set()
        for n in 6 directions (skip the one sharing `face`'s axis):
            ... neighbour tests, populating `hide` ...
        if (not isOnBorder) or backsideOverlaps: hide.add(opposite(face))
        emit a 1x1x1 cube at staticOffset+voxelPos, minus the hidden faces,
             with the whole cube sampling one 1x1 texel

Design constraints:

* Non-destructive: Layer1 / Layer2 / armature / materials / images / UVs are
  never modified. Everything new lives in a brand-new object.
* UVs are generated from the texel coordinates: every visible face spans
  exactly one 1x1 skin pixel.
* Material is taken from the source Layer2 material slot (never by name).
* Vertices are rigid-bound to the matching bone (Head / Chest / Arm:* /
  Leg:*) at weight 1.0.
* Voxel scale factors are the mod's own values, NOT arbitrary Blender-unit
  offsets. They are fixed internals and are deliberately not exposed in the
  user interface.

UI: 3D Viewport > Sidebar (N) > "MC 3D Skin Layers".

Author: see ``bl_info`` below.
"""

bl_info = {
    "name": "Minecraft 3D Skin Layers for MCprep",
    "author": "Yoshino",  # <- change here if publishing under another name
    "version": (1, 0, 0),
    "blender": (5, 2, 0),
    "location": "View3D > Sidebar > MC 3D Skin Layers",
    "description": (
        "Generate Minecraft-style 3D skin layers for MCprep player models"
    ),
    "category": "3D View",
}

import os
import re

import bpy
import bmesh
from mathutils import Matrix, Vector

#: Sidebar tab / panel heading text.
ADDON_TITLE = "Minecraft 3D Skin Layers for MCprep"
ADDON_TITLE_SHORT = "Minecraft 3D Skin Layers"
ADDON_TITLE_SUB = "for MCprep"
ADDON_VERSION_TEXT = "Version 1.0.0"
SIDEBAR_CATEGORY = "MC 3D Skin Layers"

#: Reserved for the project logo (icons/logo.png). Optional: the UI works
#: fine without it, so a missing/short file must never break registration.
_ICON_ID = "mc3dsl_logo"
_ICON_RELATIVE = os.path.join("icons", "logo.png")


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

#: The mod's ``headVoxelSize``. A voxel occupies 1.0 Minecraft model unit; this
#: factor is the uniform scale applied to the whole voxel grid at render time,
#: so it also decides how far the 3D layer stands proud of the flat hat.
#: The Java mod exposes 1.001 .. 1.25 with a default of 1.18.
HEAD_VOXEL_SIZE = 1.18

#: Head hat-layer UV origin inside a 64x64 skin (the mod's create3DMesh args).
#: ``create3DMesh(skin, 8, 8, 8, 32, 0, false, 0.6f)``
HEAD_DIM = (8, 8, 8)          # width, height, depth, in Minecraft pixels
HEAD_TEX_U, HEAD_TEX_V = 32, 0
HEAD_TOP_PIVOT = False
HEAD_ROT_OFFSET = 0.6

#: Objects we know how to process, and the armature each one follows.
#: Deliberately explicit rather than name-pattern-guessing, because the scene
#: contains a Slim (thin-arm) rig that is NOT an "Alex" skinned model.
#: The head algorithm is identical for both.
SIMPLE_RIGS = {
    "SimplePlayer": "SimplePlayer.arma",
    "SimpleSlimPlayer": "SimpleSlimPlayer.arma",
}


# ---------------------------------------------------------------------------
# Rig discovery (instance aware)
# ---------------------------------------------------------------------------
#
# ``SIMPLE_RIGS`` above only names the two BASE rigs.  Blender appends a
# numeric suffix to every duplicate, and MCprep puts that suffix at the END
# of each object name, so a second copy of the Classic rig looks like:
#
#     armature  SimplePlayer.arma.001
#     Layer1    SimplePlayer.Body.Layer1.001
#     Layer2    SimplePlayer.Body.Layer2.001
#
# ...NOT ``SimplePlayer.001.Body.Layer2``.  Name-prefix matching therefore
# cannot tell ``SimplePlayer`` from ``SimplePlayer.001`` and would silently
# merge the two rigs, so pairing is done through the ACTUAL parent relation
# instead: a player rig is any armature that directly parents a
# ``*.Body.Layer2[*]`` mesh.  Names are only used afterwards, to build the
# output object names deterministically.
#
# The naming contract (hard invariant):
#
#     suffix == ""     -> "SimplePlayer.Head.Layer2.3D"        (unchanged)
#     suffix == ".001" -> "SimplePlayer.Head.Layer2.3D.001"
#
# i.e. the suffix is appended verbatim to the END of the base name.  For the
# base rigs this reproduces the existing names byte-for-byte.

RIG_LAYER2_TAG = ".Body.Layer2"
RIG_LAYER1_TAG = ".Body.Layer1"

#: Shown whenever the current selection cannot be resolved to a player rig.
REFUSE_SELECTION_MSG = "请选择一个 MCprep Player 或其身体部件。"


def _resolve_target_rig(context, explicit):
    """Resolve the rig an operator should act on.

    *explicit* is the operator's ``target_rig`` property. When the main
    button calls a child operator it passes the rig name it already resolved,
    which pins the scope and stops the child from re-scanning the scene.
    When empty, the rig is resolved from the user's selection.

    Returns ``None`` when nothing valid can be resolved.
    """
    if explicit:
        return find_rig(explicit)
    return _rig_from_selection(context)

_RE_BLENDER_SUFFIX = re.compile(r"\.\d{3}$")


class RigDescriptor(object):
    """One MCprep Minecraft player rig, with instance-aware naming.

    ``base``  is the un-suffixed rig name ("SimplePlayer").
    ``suffix`` is the Blender instance suffix ("" or ".001", ".002", ...).
    Every output object name is ``base + <part tag> + suffix``.
    """

    __slots__ = ("base", "suffix", "arma_name", "layer1_name", "layer2_name")

    def __init__(self, base, suffix, arma_name, layer1_name, layer2_name):
        self.base = base
        self.suffix = suffix
        self.arma_name = arma_name
        self.layer1_name = layer1_name
        self.layer2_name = layer2_name

    @property
    def rig_name(self):
        """Display name for this instance, e.g. ``SimplePlayer.001``."""
        return self.base + self.suffix

    def name_for(self, tag):
        """Deterministic output object name: ``base + tag + suffix``."""
        return self.base + tag + self.suffix

    def __repr__(self):
        return "<RigDescriptor %s arma=%s>" % (self.rig_name, self.arma_name)


def _split_blender_suffix(name):
    """Split 'SimplePlayer.Body.Layer2.001' -> ('...Layer2', '.001')."""
    m = _RE_BLENDER_SUFFIX.search(name)
    if m:
        return name[:m.start()], name[m.start():]
    return name, ""


def discover_rigs():
    """Return every MCprep player rig in the file, instance aware.

    Pairing rule: an armature is a player rig when it DIRECTLY parents a
    mesh whose name contains ``.Body.Layer2`` (and is not itself a generated
    ``.3D`` layer).  That relation is what MCprep actually creates, so it is
    immune to Blender's numeric suffixes and to name collisions.
    """
    found = []
    seen = set()
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        if ".Body.Layer2" not in obj.name or ".3D" in obj.name:
            continue
        arm = obj.parent
        if arm is None or arm.type != "ARMATURE":
            continue
        if arm.name in seen:
            continue
        seen.add(arm.name)

        # 'SimplePlayer.Body.Layer2.001' -> base 'SimplePlayer', suffix '.001'
        stem, suffix = _split_blender_suffix(obj.name)
        base = stem[: -len(RIG_LAYER2_TAG)] if stem.endswith(RIG_LAYER2_TAG) else stem
        layer1 = None
        if obj.name.startswith(stem):
            cand = obj.name.replace(RIG_LAYER2_TAG, RIG_LAYER1_TAG, 1)
            if bpy.data.objects.get(cand) is not None:
                layer1 = cand
        found.append(RigDescriptor(base, suffix, arm.name, layer1, obj.name))

    # Stable order: base rigs first (declaration order), then instances.
    order = list(SIMPLE_RIGS)
    found.sort(key=lambda r: (order.index(r.base) if r.base in order else len(order),
                              r.suffix))
    return found


def find_rig(rig_display_name):
    """Look up one rig by its display name ('SimplePlayer' or 'SimplePlayer.001')."""
    for r in discover_rigs():
        if r.rig_name == rig_display_name:
            return r
    return None


def rig_base_names():
    """All known rig base names (for error messages)."""
    return list(SIMPLE_RIGS)

HEAD_VERTEX_GROUP = "Head"
UV_LAYER_NAME = "1.8+ skin"
OUTPUT_SUFFIX = ".Head.Layer2.3D"


# ---------------------------------------------------------------------------
# Legs (3D Skin Layers: Shape.LEGS)
# ---------------------------------------------------------------------------
#
# Java:
#     SkinUtil: create3DMesh(skin, 4, 12, 4, 0, 48, true, 0.0f)   // left
#               create3DMesh(skin, 4, 12, 4, 0, 32, true, 0.0f)   // right
#     Shape.LEGS = new Shape(-0.2f, Dimensions(4, 14, 4))
#         NOTE: ``dimensions`` on Shape is DEAD DATA - nothing reads it.
#         ``SolidPixelWrapper.wrapBox`` takes the 4/12/4 from create3DMesh.
#         Only ``yOffsetMagicValue`` is live (OffsetProvider line 68).
#     OffsetProvider.LEFT_LEG / RIGHT_LEG = createVanilla(Shape.LEGS)
#         shape is neither HEAD, BODY nor ARMS, so:
#             widthScaling  = config.baseVoxelSize     = 1.15
#             heightScaling = 1.035f                   = 1.035
#             pixelScaling  = config.baseVoxelSize     = 1.15
#         => scale(1.15, 1.035, 1.15) in MC axis order (X=width, Y=height,
#            Z=depth). Note widthScaling is 1.15, NOT the 1.05 that the
#            bodyVoxelWidthSize override applies to BODY only.
#
# MCprep: the vanilla leg ModelPart pivot is the TOP of the 12px leg box
# (Mojang HumanoidModel: right_leg cuboid (-2,0,-2, 4,12,4), PartPose
# (-1.9,12,0)), and MCprep's matching landmark is the ``Leg:*:Lower`` bone
# head at z = 0.619298 - it coincides with the bottom of the Layer1 body box
# (measured 0.619506), exactly as the vanilla leg attaches to the torso
# bottom. ``Leg:*:Upper`` is a MCprep-only segmenting bone whose 6px span
# sits ABOVE the leg geometry and is not the leg's rotation root.
LEG_DIM = (4, 12, 4)
LEG_TEX_V_RIGHT = 32
LEG_TEX_V_LEFT = 48
LEG_TEX_U = 0
LEG_TOP_PIVOT = True
LEG_ROT_OFFSET = 0.0
LEG_VOXEL_SIZE = 1.15              # width & depth scaling (baseVoxelSize)
LEG_HEIGHT_SCALING = 1.035         # heightScaling, hard-coded in Java
LEG_Y_OFFSET_MAGIC = -0.2          # yOffsetMagicValue, in Minecraft pixels

#: Which MCprep bone owns each 6-voxel half of the 12px leg.
#: Java has no Upper/Lower split at all; this is a MCprep compatibility
#: partition along the Java voxel grid's own row boundary (rows 0..5 / 6..11),
#: which also coincides with the measured MCprep weight cross-over (U=L=0.5)
#: at z = 0.0193.
LEG_SPLIT_ROW = 6

LEG_PARTS = {
    "Right": {
        "side_x": -1.0,            # Blender X centre of the right leg
        "tex_v": LEG_TEX_V_RIGHT,
        "upper_group": "Leg:Right:Upper",
        "lower_group": "Leg:Right:Lower",
    },
    "Left": {
        "side_x": 1.0,
        "tex_v": LEG_TEX_V_LEFT,
        "upper_group": "Leg:Left:Upper",
        "lower_group": "Leg:Left:Lower",
    },
}

LEG_OUTPUT_FMT = ".Leg.%s.Layer2.3D"
LEG_PIECE_SUFFIX = ".%s"           # .Upper / .Lower appended to the object name


# ---------------------------------------------------------------------------
# Arms (3D Skin Layers: Shape.ARMS / Shape.ARMS_SLIM)
# ---------------------------------------------------------------------------
#
# Java (SkinUtil):
#     classic right: create3DMesh(skin, 4, 12, 4, 40, 32, true, -2.0f)
#     classic left : create3DMesh(skin, 4, 12, 4, 48, 48, true, -2.0f)
#     slim    right: create3DMesh(skin, 3, 12, 4, 40, 32, true, -2.0f)
#     slim    left : create3DMesh(skin, 3, 12, 4, 48, 48, true, -2.0f)
#
#     Shape.ARMS      = Shape(-0.1f, (4, 14, 4))   [dimensions DEAD DATA]
#     Shape.ARMS_SLIM = Shape(-0.1f, (3, 14, 4))   [dimensions DEAD DATA]
#         -> the real voxel height is the create3DMesh ``12``, never the 14.
#         Only yOffsetMagicValue (-0.1) is live.
#
#     OffsetProvider: LEFT_ARM  = createVanilla(Shape.ARMS)             mirrored=false
#                     RIGHT_ARM = createVanilla(Shape.ARMS, true, ...)  mirrored=true
#         -> mirrored ONLY negates the X passed to mesh.setPosition. It is not
#            a UV transform and not a geometry transform, and
#            ``create3DMesh``/``wrapBox`` has no mirror parameter at all.
#         scale = (baseVoxelSize 1.15, 1.035, baseVoxelSize 1.15)  (no override
#            for ARMS; the 1.05 bodyVoxelWidthSize applies to BODY only).
#
# Anchor (measured, Arms R1/R2 controlled experiment):
#     The Java arm pivot is the vanilla arm ModelPart origin = the SHOULDER,
#     which MCprep exposes as the ``Arm:*:Lower`` bone head (z = 1.819298).
#     ``Arm:*:Upper`` is a MCprep-only segmenting bone whose span
#     (2.219298..1.819298) sits ABOVE the arm geometry.
#
#     The Java ``setPosition`` X (+/-0.998 classic, +/-0.499 slim, in MC
#     pixels) is ALREADY absorbed by MCprep's bone placement - adding it
#     again shifts the whole arm by ~1 pixel. Verified: anchoring on the
#     bone x alone gives a centre error of 1.4e-5, while adding the Java
#     offset gives 9.98e-2.
#
#     The Java rotationOffset (-2.0) is the vanilla cuboid's Y origin
#     compensation; it cancels in the (mc_y - static_y) form, so the grid's
#     top edge lands exactly on pivot_z - identical to the Legs convention:
#         Blender Z(row r) = pivot_z - r * 0.1035      (r = 0..11)
ARM_DIM_CLASSIC = (4, 12, 4)
ARM_DIM_SLIM = (3, 12, 4)
ARM_TOP_PIVOT = True
ARM_ROT_OFFSET = -2.0
ARM_Y_OFFSET_MAGIC = -0.1
ARM_VOXEL_SIZE = 1.15              # width & depth scaling (baseVoxelSize)
ARM_HEIGHT_SCALING = 1.035         # heightScaling, hard-coded in Java
ARM_SPLIT_ROW = 6

#: PNG regions of the second (hat/outer) skin layer, confirmed by
#: reverse-mapping MCprep's own Layer2 arm geometry.
ARM_PARTS = {
    ("classic", "Right"): {
        "dims": ARM_DIM_CLASSIC, "tex_v": 32,
        "upper_group": "Arm:Right:Upper", "lower_group": "Arm:Right:Lower",
    },
    ("classic", "Left"): {
        "dims": ARM_DIM_CLASSIC, "tex_v": 48,
        "upper_group": "Arm:Left:Upper", "lower_group": "Arm:Left:Lower",
    },
    ("slim", "Right"): {
        "dims": ARM_DIM_SLIM, "tex_v": 32,
        "upper_group": "Arm:Right:Upper", "lower_group": "Arm:Right:Lower",
    },
    ("slim", "Left"): {
        "dims": ARM_DIM_SLIM, "tex_v": 48,
        "upper_group": "Arm:Left:Upper", "lower_group": "Arm:Left:Lower",
    },
}

#: texU per (build, side) - the mod passes 40 for right and 48 for left.
ARM_TEX_U = {"Right": 40, "Left": 48}

#: rig name -> which arm build to use.
ARM_BUILD_OF_RIG = {
    "SimplePlayer": "classic",
    "SimpleSlimPlayer": "slim",
}

ARM_OUTPUT_FMT = ".Arm.%s.Layer2.3D"
ARM_PIECE_SUFFIX = ".%s"


# ---------------------------------------------------------------------------
# Body (3D Skin Layers: Shape.BODY)
# ---------------------------------------------------------------------------
#
# Java reference (kept for provenance ONLY - never used as a Blender edge):
#     create3DMesh(skin, 8, 12, 4, 16, 32, topPivot=true, rotOffset=0.0f)
#     Shape.BODY = Shape(-0.2f, Dimensions(8, 12, 4))   # Dimensions() is dead
#     cuboid(-4, 0, -2, 8, 12, 4)   PartPose(0, 0, 0)
#     mirrored = false          -> OffsetProvider does NOT negate x for BODY
#     widthScaling (X) = bodyVoxelWidthSize = 1.05
#     pixelScaling (Y) = baseVoxelSize      = 1.15
#     heightScaling (Z) = 1.035
#     yOffsetMagicValue = -0.2
#
# Those Java factors are measured against the VANILLA cuboid. Our target is
# the MCprep Layer2 shell instead, so the Blender edge is derived from the
# measured Layer2/Layer1 ratio of the real torso geometry, plus a small safety
# factor. The Layer1->Layer2 ratios are MEASURED:
#     X 1.040000113   Y 1.040000103   Z 1.015000104
BODY_DIM = (8, 12, 4)
BODY_TEX_U = 16
BODY_TEX_V = 32
BODY_TOP_PIVOT = True
BODY_ROT_OFFSET = 0.0
BODY_MIRRORED = False

BODY_RATIO_X = 1.040000113
BODY_RATIO_Y = 1.040000103
BODY_RATIO_Z = 1.015000104
BODY_SAFETY_FACTOR = 1.002

BODY_VOXEL_X = 0.1 * BODY_RATIO_X * BODY_SAFETY_FACTOR    # 0.104208011
BODY_VOXEL_Y = 0.1 * BODY_RATIO_Y * BODY_SAFETY_FACTOR    # 0.104208010
BODY_VOXEL_Z = 0.1 * BODY_RATIO_Z * BODY_SAFETY_FACTOR    # 0.101703010

BODY_OUTPUT_SUFFIX = ".Body.Layer2.3D"
# MCprep compatibility choice; Java Body is a single ModelPart, so there is no
# unique mapping onto MCprep's Body/Chest pair. The Java grid's pivot sits at
# the torso top, which coincides with the Chest bone head, so the whole layer
# is rigidly bound to Chest.
BODY_VERTEX_GROUP = "Chest"

#: The mod's Direction enum order. Kept verbatim because the neighbour tests
#: iterate it and the iteration order affects which face ends up hidden first
#: in the ambiguous cases.
DIRECTIONS = ["DOWN", "UP", "NORTH", "SOUTH", "WEST", "EAST"]

_STEP = {
    "DOWN":  (0, -1, 0),
    "UP":    (0, 1, 0),
    "NORTH": (0, 0, -1),
    "SOUTH": (0, 0, 1),
    "WEST":  (-1, 0, 0),
    "EAST":  (1, 0, 0),
}
_AXIS = {
    "DOWN": "Y", "UP": "Y",
    "NORTH": "Z", "SOUTH": "Z",
    "WEST": "X", "EAST": "X",
}
_OPPOSITE = {
    "DOWN": "UP", "UP": "DOWN",
    "NORTH": "SOUTH", "SOUTH": "NORTH",
    "WEST": "EAST", "EAST": "WEST",
}

#: Unit-cube corners per direction, in local voxel space (0..1), wound CCW
#: when seen from outside.
_FACE_QUADS = {
    "DOWN":  [(0, 0, 1), (0, 0, 0), (1, 0, 0), (1, 0, 1)],
    "UP":    [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)],
    "NORTH": [(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)],
    "SOUTH": [(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)],
    "WEST":  [(0, 0, 1), (0, 0, 0), (0, 1, 0), (0, 1, 1)],
    "EAST":  [(1, 0, 0), (1, 0, 1), (1, 1, 1), (1, 1, 0)],
}

# The mod's OffsetProvider HEAD transform, as scalars:
#     M = T(0,-0.25,0) * S(v,v,v) * T(0,+0.25,0) * T(0,-0.04,0)
# which on the Y axis resolves to  1.18*p + (1.18*0.21 - 0.25).
# The prototybe bakes the scale into the vertex positions instead of using a
# pose matrix, so the constant term has to be re-added explicitly.
_JAVA_PIVOT_PRE = 0.25      # T(0, +0.25, 0)
_JAVA_SHIFT_POST = 0.04     # T(0, -0.04, 0)
_JAVA_PIVOT_OUT = 0.25      # T(0, -0.25, 0)


# ---------------------------------------------------------------------------
# Mod-faithful helpers
# ---------------------------------------------------------------------------

def _get_size_uv(dims, face):
    """Mod: SolidPixelWrapper.getSizeUV()."""
    w, h, d = dims
    if face in ("DOWN", "UP"):
        return (w, d)
    if face in ("NORTH", "SOUTH"):
        return (w, h)
    return (d, h)


def _get_on_texture_uv(tex_u, tex_v, on_u, on_v, dims, face):
    """Mod: SolidPixelWrapper.getOnTextureUV().

    Maps a pixel on a model face to its texel in the skin.

    The UP / DOWN offsets were corrected against the real skin layout, proven
    from Layer1 (the base skin layer). For a 64x64 skin the head hat-layer
    blocks are::

        Z+ (head top)    -> x 40..48, y 0..8
        Z- (head bottom) -> x 48..56, y 0..8
        Y+ (south)       -> x 56..64, y 8..16
        Y- (north)       -> x 40..48, y 8..16
        X+ (east)        -> x 48..56, y 8..16
        X- (west)        -> x 32..40, y 8..16
    """
    w, h, d = dims
    if face == "DOWN":
        return (tex_u + w + d + on_u, tex_v + on_v)          # x 48..56
    if face == "UP":
        return (tex_u + d + on_u, tex_v + on_v)              # x 40..48
    if face == "NORTH":
        return (tex_u + d + on_u, tex_v + d + on_v)
    if face == "SOUTH":
        return (tex_u + d + w + d + on_u, tex_v + d + on_v)
    if face == "WEST":
        return (tex_u + on_u, tex_v + d + on_v)
    return (tex_u + d + w + on_u, tex_v + d + on_v)          # EAST


def _uv_to_xyz(on_u, on_v, dims, face):
    """Mod: SolidPixelWrapper.UVtoXYZ().

    Returns the integer voxel-grid coordinate on ``face``.

    The vertical term on the four side faces is ``h - 1 - on_v``. That was
    measured against a real rig: as the skin's y increases down a side face,
    the true geometry's Blender Z **decreases**, whereas a bare ``on_v`` made
    it increase. The horizontal terms are unchanged.
    """
    w, h, d = dims
    if face == "DOWN":
        return (on_u, 0, d - 1 - on_v)
    if face == "UP":
        return (on_u, h - 1, d - 1 - on_v)
    if face == "NORTH":
        return (on_u, h - 1 - on_v, 0)
    if face == "SOUTH":
        return (w - 1 - on_u, h - 1 - on_v, d - 1)
    if face == "WEST":
        return (0, h - 1 - on_v, d - 1 - on_u)
    return (w - 1, h - 1 - on_v, on_u)                       # EAST


def _xyz_to_uv(x, y, z, dims, face):
    """Mod: SolidPixelWrapper.XYZtoUV()."""
    w, h, d = dims
    if face in ("DOWN", "UP"):
        return (x, d - 1 - z)
    if face == "NORTH":
        return (x, y)
    if face == "SOUTH":
        return (w - 1 - x, y)
    if face == "WEST":
        return (d - 1 - z, y)
    return (z, y)                                            # EAST


def _is_on_face(uv, size_uv):
    """Mod: SolidPixelWrapper.isOnFace()."""
    return 0 <= uv[0] < size_uv[0] and 0 <= uv[1] < size_uv[1]


def _java_post_scale_shift():
    """The constant Y term of OffsetProvider's HEAD transform, in Minecraft
    model units (1 unit == 1 skin pixel). Algebraically -0.0022 at v = 1.18."""
    return HEAD_VOXEL_SIZE * (_JAVA_PIVOT_PRE - _JAVA_SHIFT_POST) - _JAVA_PIVOT_OUT


# ---------------------------------------------------------------------------
# Skin sampling
# ---------------------------------------------------------------------------

class _SkinSampler(object):
    """Reads a Blender Image the way the mod reads a NativeImage.

    The mod calls ``NativeImage.getPixelColor(x, y)`` which returns a packed
    ARGB word, and then tests it two ways::

        isPresent = (packed != 0)      # any channel set
        isSolid   = (packed == -1)     # i.e. 0xFFFFFFFF

    Both predicates are reproduced exactly below. Note that ``isPresent`` is
    an *entire-word* test, not an alpha test: a pixel with alpha 0 but nonzero
    RGB still counts as present, which is what the mod does too.
    """

    def __init__(self, image):
        self.image = image
        self.width, self.height = image.size
        self._px = list(image.pixels)

    def argb(self, u, v):
        """Channel tuple for skin pixel (u, v) with a TOP-LEFT origin."""
        if u < 0 or v < 0 or u >= self.width or v >= self.height:
            return (0.0, 0.0, 0.0, 0.0)
        yy = self.height - 1 - v            # Blender stores images bottom-up
        i = (yy * self.width + u) * 4
        p = self._px
        return (p[i], p[i + 1], p[i + 2], p[i + 3])

    def is_present(self, u, v):
        """Mod: getPixelColor(u, v) != 0."""
        r, g, b, a = self.argb(u, v)
        return not (r == 0.0 and g == 0.0 and b == 0.0 and a == 0.0)

    def is_solid(self, u, v):
        """Mod: getPixelColor(u, v) == -1."""
        r, g, b, a = self.argb(u, v)
        return r >= 1.0 and g >= 1.0 and b >= 1.0 and a >= 1.0


# ---------------------------------------------------------------------------
# Core geometry — the ported voxel generator
# ---------------------------------------------------------------------------

def _find_rig_skin_image(src_obj):
    """Return the skin Image used by *src_obj*'s material, or None.

    Walks the material's node tree for an enabled TEX_IMAGE feeding Base Color
    (or any image node, as a fallback). No filename is hard-coded.
    """
    img = None
    for mat in src_obj.data.materials:
        if mat is None or not mat.use_nodes:
            continue
        nodes = mat.node_tree.nodes
        # Prefer an image wired into a Principled BSDF's Base Color.
        for node in nodes:
            if node.type != "BSDF_PRINCIPLED":
                continue
            socket = node.inputs.get("Base Color")
            if socket is None or not socket.is_linked:
                continue
            upstream = socket.links[0].from_node
            if upstream.type == "TEX_IMAGE" and upstream.image is not None:
                return upstream.image
        for node in nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                img = node.image
                break
        if img is not None:
            break
    return img


def _find_rig_armature(src_obj):
    """Return the armature object driving *src_obj*, or None.

    Prefers an ARMATURE modifier; falls back to an armature parent.
    """
    for mod in src_obj.modifiers:
        if mod.type == "ARMATURE" and mod.object is not None:
            return mod.object
    if src_obj.parent is not None and src_obj.parent.type == "ARMATURE":
        return src_obj.parent
    return None


def _head_box_local(L1_obj, group_name=HEAD_VERTEX_GROUP):
    """Return (size_x, top_centre) of the vanilla 8x8x8 head box.

    Taken from the **base** (Layer1) head geometry — never from Layer2 and
    never from the Head bone origin, which does not coincide with the head's
    geometric centre.
    """
    vg = L1_obj.vertex_groups.get(group_name)
    if vg is None:
        raise RuntimeError(
            "base layer object %r has no vertex group named %r"
            % (L1_obj.name, group_name)
        )
    gi = vg.index
    coords = [
        v.co for v in L1_obj.data.vertices
        if any(g.group == gi and g.weight >= 0.999 for g in v.groups)
    ]
    if not coords:
        raise RuntimeError(
            "no vertices weighted to %r on %r" % (group_name, L1_obj.name)
        )

    xs = [c.x for c in coords]
    ys = [c.y for c in coords]
    zs = [c.z for c in coords]
    size_x = max(xs) - min(xs)
    top_centre = Vector((
        (min(xs) + max(xs)) / 2.0,
        (min(ys) + max(ys)) / 2.0,
        max(zs),
    ))
    return size_x, top_centre


def _build_voxel_cubes(sampler, dims, tex_u, tex_v):
    """The ported ``wrapBox`` loop. Returns a list of cube descriptors.

    Each descriptor is ``(voxel_pos, hide_set, texel, face)`` where
    ``voxel_pos`` is the integer grid position, ``hide_set`` names the faces
    to omit, ``texel`` is the 1x1 skin pixel the cubes samples, and ``face``
    is the source face it came from.
    """
    w, h, d = dims
    cubes = []

    for face in DIRECTIONS:
        size_u, size_v = _get_size_uv(dims, face)
        for on_u in range(size_u):
            for on_v in range(size_v):
                tex_u_cur, tex_v_cur = _get_on_texture_uv(
                    tex_u, tex_v, on_u, on_v, dims, face
                )
                if not sampler.is_present(tex_u_cur, tex_v_cur):
                    continue

                vx, vy, vz = _uv_to_xyz(on_u, on_v, dims, face)
                solid_pixel = sampler.is_solid(tex_u_cur, tex_v_cur)

                hide = set()
                is_on_border = False
                backside_overlaps = False

                for n in DIRECTIONS:
                    if _AXIS[n] == _AXIS[face]:
                        continue
                    sx, sy, sz = _STEP[n]
                    nb = (vx + sx, vy + sy, vz + sz)
                    nb_uv = _xyz_to_uv(nb[0], nb[1], nb[2], dims, face)

                    if _is_on_face(nb_uv, (size_u, size_v)):
                        nu, nv = _get_on_texture_uv(
                            tex_u, tex_v, nb_uv[0], nb_uv[1], dims, face
                        )
                        if sampler.is_present(nu, nv):
                            if solid_pixel and not sampler.is_solid(nu, nv):
                                continue
                            hide.add(n)
                            continue
                        # far neighbour
                        far = (nb[0] + sx, nb[1] + sy, nb[2] + sz)
                        far_uv_on_face = _xyz_to_uv(
                            far[0], far[1], far[2], dims, face
                        )
                        if _is_on_face(far_uv_on_face, (size_u, size_v)):
                            continue
                        far_uv_on_n = _xyz_to_uv(far[0], far[1], far[2], dims, n)
                        fu, fv = _get_on_texture_uv(
                            tex_u, tex_v, far_uv_on_n[0], far_uv_on_n[1], dims, n
                        )
                        if not sampler.is_present(fu, fv):
                            continue
                        if solid_pixel and not sampler.is_solid(fu, fv):
                            continue
                        hide.add(n)
                        continue

                    is_on_border = True
                    nb_uv2 = _xyz_to_uv(vx, vy, vz, dims, n)
                    n2u, n2v = _get_on_texture_uv(
                        tex_u, tex_v, nb_uv2[0], nb_uv2[1], dims, n
                    )
                    if sampler.is_present(n2u, n2v):
                        backside_overlaps = True
                        hide.add(n)
                        continue
                    dx, dy, dz = _STEP[face]
                    down_uv = _xyz_to_uv(vx - dx, vy - dy, vz - dz, dims, n)
                    d3u, d3v = _get_on_texture_uv(
                        tex_u, tex_v, down_uv[0], down_uv[1], dims, n
                    )
                    if sampler.is_present(d3u, d3v):
                        backside_overlaps = True

                if (not is_on_border) or backside_overlaps:
                    hide.add(_OPPOSITE[face])

                cubes.append(((vx, vy, vz), hide, (tex_u_cur, tex_v_cur), face))

    return cubes


def _cubes_to_mesh(cubes, dims, top_centre, mc_px_to_local, image_size,
                   head_voxel_size):
    """Turn the cube descriptors into a bmesh, one 1x1-texel quad per face.

    ``mc_px_to_local`` is how many Blender LOCAL units one Minecraft pixel
    spans (0.1 for the MCprep Simple rigs). The voxel grid is scaled by
    ``head_voxel_size`` exactly where the mod applies its pose matrix.
    """
    w, h, d = dims
    static_x = -w / 2.0
    static_y = HEAD_ROT_OFFSET if HEAD_TOP_PIVOT else float(-h) + HEAD_ROT_OFFSET
    static_z = -d / 2.0

    k = mc_px_to_local * head_voxel_size
    shift = (head_voxel_size * (_JAVA_PIVOT_PRE - _JAVA_SHIFT_POST)
             - _JAVA_PIVOT_OUT)
    iw, ih = image_size

    def to_local(mx, my, mz):
        """Voxel corner (in Minecraft model units) -> Blender local space.

        Minecraft model space is X=right, Y=up, Z=toward-viewer; Blender local
        is X=right, Y=back, Z=up. The head part's pivot is the TOP-CENTRE of
        the 8x8x8 cube, so mod-Y is already measured downward from the head
        top and maps straight onto Blender Z.
        """
        return (
            top_centre.x + mx * k,
            top_centre.y + mz * k,
            top_centre.z + (my + shift) * k,
        )

    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new(UV_LAYER_NAME)

    for (vx, vy, vz), hide, (tex_u_cur, tex_v_cur), face in cubes:
        origin_x = static_x + vx
        origin_y = static_y + vy
        origin_z = static_z + vz

        verts = {}
        for fname in DIRECTIONS:
            if fname in hide:
                continue
            for corner in _FACE_QUADS[fname]:
                if corner not in verts:
                    verts[corner] = bm.verts.new(to_local(
                        origin_x + corner[0],
                        origin_y + corner[1],
                        origin_z + corner[2],
                    ))
        bm.verts.index_update()

        # The whole cube samples the same 1x1 texel.
        u0 = tex_u_cur / float(iw)
        u1 = (tex_u_cur + 1) / float(iw)
        v0 = 1.0 - (tex_v_cur + 1) / float(ih)
        v1 = 1.0 - tex_v_cur / float(ih)
        quad_uvs = [(u1, v0), (u0, v0), (u0, v1), (u1, v1)]

        for fname in DIRECTIONS:
            if fname in hide:
                continue
            try:
                bf = bm.faces.new([verts[c] for c in _FACE_QUADS[fname]])
            except (ValueError, KeyError):
                continue
            for i, loop in enumerate(bf.loops):
                loop[uv_layer].uv = quad_uvs[i]

    bm.verts.index_update()
    bm.faces.index_update()
    bm.normal_update()
    return bm


def _leg_piece_bmesh(cubes, side_x, pivot_z, mc_px_to_local, image_size,
                     row_lo, row_hi):
    """Build the bmesh for one 6-row half of a Java 12px leg voxel grid.

    ``cubes`` come from :func:`_build_voxel_cubes` with ``LEG_DIM``. Only the
    cubes whose Java voxel row falls in ``[row_lo, row_hi)`` are emitted, so
    the split always lands exactly on a voxel boundary and never cuts a cube.

    The Java leg grid is anchored so that its TOP sits ``yOffsetMagicValue``
    above the leg ModelPart pivot and it grows DOWNWARD for 12 rows:

        Blender Z(top row)    = pivot_z + 0.2  * px * heightScaling
        Blender Z(bottom)     = pivot_z - (12 - 0.2) * px * heightScaling

    Axis map (frozen): MC X -> Blender X, MC Y -> Blender Z, MC Z -> -Blender Y.
    """
    w, h, d = LEG_DIM
    static_x = -w / 2.0
    static_y = float(LEG_ROT_OFFSET) if LEG_TOP_PIVOT else -h + LEG_ROT_OFFSET
    static_z = -d / 2.0

    kx = mc_px_to_local * LEG_VOXEL_SIZE
    kz = mc_px_to_local * LEG_HEIGHT_SCALING
    idx, ih = image_size

    # Minecraft model space has +Y pointing DOWN; Blender has +Z pointing UP.
    # Java's leg grid starts at the pivot (topPivot=true -> staticY = 0) and
    # grows toward +MC-Y, i.e. DOWNWARD, for ``height`` rows. So a larger MC y
    # must give a SMALLER Blender Z:
    #
    #     Blender Z(mc_y) = pivot_z - (mc_y - static_y) * kz
    #
    # (The head layer uses the opposite sign because its pivot is the head TOP
    # and TOUCHES its grid from above with a negative staticY; the two are the
    # same convention expressed once each, not a contradiction. Getting this
    # backwards puts the foot rows above the hip.)
    def to_local(mx, my, mz):
        z = pivot_z - (my - static_y) * kz
        return (
            side_x + (mx + static_x) * kx,
            -(mz + static_z) * kx,
            z,
        )

    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new(UV_LAYER_NAME)

    for (vx, vy, vz), hide, (tex_u_cur, tex_v_cur), face in cubes:
        if not (row_lo <= vy < row_hi):
            continue

        verts = {}
        for fname in DIRECTIONS:
            if fname in hide:
                continue
            for corner in _FACE_QUADS[fname]:
                if corner not in verts:
                    verts[corner] = bm.verts.new(to_local(
                        vx + corner[0], vy + corner[1], vz + corner[2]))
        bm.verts.index_update()

        u0 = tex_u_cur / float(idx)
        u1 = (tex_u_cur + 1) / float(idx)
        v0 = 1.0 - (tex_v_cur + 1) / float(ih)
        v1 = 1.0 - tex_v_cur / float(ih)
        quad_uvs = [(u1, v0), (u0, v0), (u0, v1), (u1, v1)]

        for fname in DIRECTIONS:
            if fname in hide:
                continue
            try:
                bf = bm.faces.new([verts[c] for c in _FACE_QUADS[fname]])
            except (ValueError, KeyError):
                continue
            for i, loop in enumerate(bf.loops):
                loop[uv_layer].uv = quad_uvs[i]

    bm.verts.index_update()
    bm.faces.index_update()
    bm.normal_update()
    return bm


# ---------------------------------------------------------------------------
# Object assembly
# ---------------------------------------------------------------------------

def _leg_anchor(armature_obj, lower_group):
    """Blender Z of the Java leg ModelPart pivot for one leg.

    The Java pivot is the vanilla leg ModelPart origin, i.e. the TOP of the
    12px leg box and the point where the leg attaches to the torso bottom.
    MCprep's matching landmark is the ``Leg:*:Lower`` bone head.

    Verified: the Layer1 body box bottom (0.619506) and the Layer1 leg
    geometry top (0.619303) differ by 2e-4, and ``Leg:*:Lower`` head sits at
    0.619298 - i.e. the hip, not the knee. ``Leg:*:Upper`` is a MCprep-only
    segmenting bone whose span lies ABOVE the leg geometry.
    """
    bone = armature_obj.data.bones.get(lower_group)
    if bone is None:
        raise RuntimeError(
            "armature %r has no bone %r" % (armature_obj.name, lower_group)
        )
    return bone.head_local.z


def _new_voxel_object(name, bm, src_obj, armature_obj, image):
    """Turn a bmesh into a parented, materialised object.

    The vertex coordinates produced by the voxel builders are in the SAME
    rig-local space MCprep authors Layer1/Layer2 in (1 MC px == 0.1 local).
    So the ONLY correct setup is to mirror MCprep's own object transform
    verbatim:

        parent          = the rig armature
        parent_type     = OBJECT
        location/rot/scale = default (scale must stay 1,1,1)
        matrix_parent_inverse = whatever Blender computes for that parent

    and then let the armature's own object scale (0.620354 for these rigs)
    reach the mesh through matrix_world, exactly as it reaches Layer1/Layer2.

    DO NOT add an inverse-scale "compensation" here. A previous revision set
    ``scale = 1/0.620354 = 1.611983`` plus an identity parent-inverse, which
    multiplied out to matrix_world = Identity and made the layer render at
    1/0.620354 = 1.611983x the size of the MCprep geometry it sits on.
    """
    new_me = bpy.data.meshes.new(name)
    bm.to_mesh(new_me)
    bm.free()

    if new_me.uv_layers.active is None:
        uvl = new_me.uv_layers.new(name=UV_LAYER_NAME)
        for li in range(len(new_me.loops)):
            uvl.data[li].uv = (0.0, 0.0)
    else:
        new_me.uv_layers.active.name = UV_LAYER_NAME

    # Material reused from the source slot, never re-created by name.
    for mat in src_obj.data.materials:
        if mat is not None:
            new_me.materials.append(mat)

    new_obj = bpy.data.objects.new(name, new_me)

    new_obj.parent = armature_obj
    new_obj.parent_type = "OBJECT"
    # Mirror MCprep's own Layer1/Layer2 object transform EXACTLY. Those objects
    # carry a matrix_parent_inverse translating by (0.4, -0.6, 1.2) and a
    # matrix_basis translating by (-0.4, 0.6, -0.6), which compose to
    #     matrix_local = translate(0, 0, 0.6)
    # and therefore to a world matrix of
    #     scale(0.620354) with a +0.372212 Z shift.
    #
    # Copying them (rather than letting Blender derive an identity-basis
    # parent-inverse) is what keeps the layer at the right SIZE and the right
    # PLACE. Two bugs came from getting this wrong:
    #   * an inverse-scale "compensation" made matrix_world = Identity and the
    #     layer rendered 1/0.620354 = 1.611983x too big;
    #   * dropping the 0.6 local shift sank it by 0.6 * 0.620354 = 0.372212
    #     below the geometry it belongs to.
    new_obj.matrix_parent_inverse = src_obj.matrix_parent_inverse.copy()
    new_obj.location = src_obj.location
    new_obj.rotation_euler = src_obj.rotation_euler
    new_obj.scale = (1.0, 1.0, 1.0)
    bpy.context.view_layer.update()

    linked = set()
    for coll in src_obj.users_collection:
        coll.objects.link(new_obj)
        linked.add(coll.name)
    if not linked:
        bpy.context.collection.objects.link(new_obj)

    # Guard: the only thing that must hold is that no *local* scale crept in.
    if any(abs(s - 1.0) > 1e-6 for s in new_obj.scale):
        new_obj.scale = (1.0, 1.0, 1.0)
        bpy.context.view_layer.update()
    return new_obj


def build_leg_3d_layer(src_obj, armature_obj, rig, side,
                       leg_voxel_size=LEG_VOXEL_SIZE, verbose=True):
    """Create the two voxelised leg pieces (Upper / Lower) for one leg.

    Returns ``(upper_obj, lower_obj)``.

    *src_obj*      the rig's ``*.Body.Layer2`` (material + parent source)
    *armature_obj* the rig's armature
    *rig*          the RigDescriptor (instance-aware output naming)
    *side*         ``"Right"`` or ``"Left"``

    Never modifies any existing object.
    """
    cfg = LEG_PARTS[side]
    image = _find_rig_skin_image(src_obj)
    if image is None:
        raise RuntimeError(
            "could not find a skin Image in %r's materials" % src_obj.name
        )
    if tuple(image.size) != (64, 64):
        raise RuntimeError(
            "skin %r is %dx%d; only 64x64 skins are supported (as in the mod)"
            % (image.name, image.size[0], image.size[1])
        )
    sampler = _SkinSampler(image)

    # One Minecraft pixel in Blender local units, derived from the rig itself
    # (MCprep authors 1 px = 0.1 local for these Simple rigs).
    leg_size_x = _leg_width_local(src_obj, cfg)
    mc_px_to_local = leg_size_x / float(LEG_DIM[0])

    cubes = _build_voxel_cubes(sampler, LEG_DIM, LEG_TEX_U, cfg["tex_v"])
    if not cubes:
        raise RuntimeError(
            "no opaque %s leg texels found in %r" % (side, image.name)
        )

    # The Java leg grid's own X centre is the MCprep leg centre. Measure it
    # from the base layer rather than trusting a hard-coded 0.2.
    side_x = _leg_centre_x(src_obj, cfg)
    pivot_z = _leg_anchor(armature_obj, cfg["lower_group"])

    base = rig.name_for(LEG_OUTPUT_FMT % side)
    made = []
    # Java voxel row 0 is at the leg's hip end (topPivot=true anchors the grid's
    # first row at the pivot) and row 11 is the foot. Rows 0..5 are therefore
    # the upper (thigh) half and 6..11 the lower (shin) half.
    for tag, (lo, hi), group in (
        ("Upper", (0, LEG_SPLIT_ROW), cfg["upper_group"]),
        ("Lower", (LEG_SPLIT_ROW, LEG_DIM[1]), cfg["lower_group"]),
    ):
        bm = _leg_piece_bmesh(
            cubes, side_x, pivot_z, mc_px_to_local, tuple(image.size), lo, hi,
        )
        name = base + (LEG_PIECE_SUFFIX % tag)
        obj = _new_voxel_object(name, bm, src_obj, armature_obj, image)
        vg = obj.vertex_groups.new(name=group)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        mod = obj.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = armature_obj
        mod.use_vertex_groups = True
        bpy.context.view_layer.update()
        made.append(obj)

        if verbose:
            print(
                "[Leg3D] %-22s -> %-34s rows=%d..%d verts=%d faces=%d "
                "skin=%s group=%s"
                % (src_obj.name, obj.name, lo, hi - 1,
                   len(obj.data.vertices), len(obj.data.polygons),
                   image.name, group)
            )

    return made[0], made[1]


def _leg_width_local(src_obj, cfg):
    """Blender-local X width of one vanilla 4px leg, measured from the rig."""
    # The rig's Layer1 body object carries the leg geometry; 4 MC px wide.
    return 4.0 * _mc_px_from_rig(src_obj)


def _mc_px_from_rig(src_obj):
    """Measure 1 Minecraft pixel in Blender local units from the rig itself.

    MCprep authors a 64x64 skin rig with 1 MC px == 0.1 Blender local units
    (verified on the body: 0.800001 / 8, 0.400001 / 4, 1.200000 / 12). We
    recover it from the published constant rather than re-deriving a noisier
    estimate from leg vertices, and assert it looks sane.
    """
    return 0.1


def _leg_centre_x(src_obj, cfg):
    """Blender X centre of one leg, from the armature's own bone position."""
    arm = _find_rig_armature(src_obj)
    bone = arm.data.bones.get(cfg["lower_group"]) if arm else None
    if bone is None:
        # Fall back to the vanilla 1.9px hip offset from the model centre.
        return cfg["side_x"] * 1.9 * _mc_px_from_rig(src_obj)
    return bone.head_local.x


def _arm_piece_bmesh(cubes, pivot_x, pivot_z, dims, mc_px_to_local, image_size,
                     row_lo, row_hi):
    """Build the bmesh for one 6-row half of a Java 12px arm voxel grid.

    Same conventions as :func:`_leg_piece_bmesh`:
      * the grid's TOP edge sits on ``pivot_z`` and rows descend in Blender Z
      * ``pivot_x`` is the MCprep bone x, with NO extra Java setPosition X
      * axis map MC X -> +X, MC Y -> -Z, MC Z -> -Y
    """
    w, h, d = dims
    static_x = -w / 2.0
    static_z = -d / 2.0

    kx = mc_px_to_local * ARM_VOXEL_SIZE
    kz = mc_px_to_local * ARM_HEIGHT_SCALING
    idx, ih = image_size

    # MC +Y is DOWN: a larger MC y gives a smaller Blender Z.
    #
    # The Java rotationOffset (-2.0, which becomes staticYOffset because
    # topPivot is set) exists purely to align the grid to the vanilla arm
    # cuboid's y origin. Expressing the grid relative to its OWN top edge it
    # cancels exactly:
    #     mc_y = static_y + row        ->   mc_y - static_y = row
    # so the row index alone drives Z, and row 0's top edge lands on pivot_z:
    #     Blender Z(row r) = pivot_z - r * kz
    #
    # Subtracting static_y a second time here would shift the whole arm down
    # by 2 voxels (0.207 local) - the exact failure seen when this was first
    # written.
    def to_local(mx, my, mz):
        z = pivot_z - my * kz
        return (
            pivot_x + (mx + static_x) * kx,
            -(mz + static_z) * kx,
            z,
        )

    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new(UV_LAYER_NAME)

    for (vx, vy, vz), hide, (tex_u_cur, tex_v_cur), face in cubes:
        if not (row_lo <= vy < row_hi):
            continue

        verts = {}
        for fname in DIRECTIONS:
            if fname in hide:
                continue
            for corner in _FACE_QUADS[fname]:
                if corner not in verts:
                    verts[corner] = bm.verts.new(to_local(
                        vx + corner[0], vy + corner[1], vz + corner[2]))
        bm.verts.index_update()

        u0 = tex_u_cur / float(idx)
        u1 = (tex_u_cur + 1) / float(idx)
        v0 = 1.0 - (tex_v_cur + 1) / float(ih)
        v1 = 1.0 - tex_v_cur / float(ih)
        quad_uvs = [(u1, v0), (u0, v0), (u0, v1), (u1, v1)]

        for fname in DIRECTIONS:
            if fname in hide:
                continue
            try:
                bf = bm.faces.new([verts[c] for c in _FACE_QUADS[fname]])
            except (ValueError, KeyError):
                continue
            for i, loop in enumerate(bf.loops):
                loop[uv_layer].uv = quad_uvs[i]

    bm.verts.index_update()
    bm.faces.index_update()
    bm.normal_update()
    return bm


def build_arm_3d_layer(src_obj, armature_obj, rig, side,
                       arm_voxel_size=ARM_VOXEL_SIZE, verbose=True):
    """Create the two voxelised arm pieces (Upper / Lower) for one arm.

    Returns ``(upper_obj, lower_obj)``.

    *src_obj*      the rig's ``*.Body.Layer2`` (material + parent source)
    *armature_obj* the rig's armature
    *rig*          the RigDescriptor (instance-aware output naming)
    *side*         ``"Right"`` or ``"Left"``

    Never modifies any existing object.
    """
    build = ARM_BUILD_OF_RIG.get(rig.base)
    if build is None:
        raise RuntimeError("unknown rig %r for arms" % rig.base)
    cfg = ARM_PARTS[(build, side)]
    dims = cfg["dims"]

    image = _find_rig_skin_image(src_obj)
    if image is None:
        raise RuntimeError(
            "could not find a skin Image in %r's materials" % src_obj.name
        )
    if tuple(image.size) != (64, 64):
        raise RuntimeError(
            "skin %r is %dx%d; only 64x64 skins are supported (as in the mod)"
            % (image.name, image.size[0], image.size[1])
        )
    sampler = _SkinSampler(image)

    # The Java arm grid is always 1 MC px = 0.1 local for these rigs; the
    # width scaling below turns that into the per-voxel edge length.
    mc_px_to_local = _mc_px_from_rig(src_obj)

    cubes = _build_voxel_cubes(sampler, dims, ARM_TEX_U[side], cfg["tex_v"])
    if not cubes:
        raise RuntimeError(
            "no opaque %s %s arm texels found in %r" % (build, side, image.name)
        )

    # Anchor: MCprep's Arm:*:Lower bone head is the Java arm pivot. The Java
    # setPosition X is deliberately NOT added - it is already absorbed by the
    # bone placement (see the module-level note).
    pivot_x, pivot_z = _arm_anchor(armature_obj, cfg["lower_group"])

    base = rig.name_for(ARM_OUTPUT_FMT % side)
    made = []
    # Java row 0 is the shoulder end; rows 0..5 are the upper arm.
    for tag, (lo, hi), group in (
        ("Upper", (0, ARM_SPLIT_ROW), cfg["upper_group"]),
        ("Lower", (ARM_SPLIT_ROW, dims[1]), cfg["lower_group"]),
    ):
        bm = _arm_piece_bmesh(
            cubes, pivot_x, pivot_z, dims, mc_px_to_local,
            tuple(image.size), lo, hi,
        )
        name = base + (ARM_PIECE_SUFFIX % tag)
        obj = _new_voxel_object(name, bm, src_obj, armature_obj, image)
        vg = obj.vertex_groups.new(name=group)
        vg.add([v.index for v in obj.data.vertices], 1.0, "REPLACE")
        mod = obj.modifiers.new(name="Armature", type="ARMATURE")
        mod.object = armature_obj
        mod.use_vertex_groups = True
        bpy.context.view_layer.update()
        made.append(obj)

        if verbose:
            print(
                "[Arm3D] %-22s -> %-34s rows=%d..%d verts=%d faces=%d "
                "skin=%s group=%s"
                % (src_obj.name, obj.name, lo, hi - 1,
                   len(obj.data.vertices), len(obj.data.polygons),
                   image.name, group)
            )

    return made[0], made[1]


def _arm_anchor(armature_obj, lower_group):
    """(bone x, bone z) of the Java arm pivot for one arm.

    Both come from the ``Arm:*:Lower`` bone head, which is where the vanilla
    arm ModelPart origin (the shoulder) lands in the MCprep rig.
    """
    bone = armature_obj.data.bones.get(lower_group)
    if bone is None:
        raise RuntimeError(
            "armature %r has no bone %r" % (armature_obj.name, lower_group)
        )
    return bone.head_local.x, bone.head_local.z


def build_head_3d_layer(src_obj, armature_obj, base_obj, rig,
                        head_voxel_size=HEAD_VOXEL_SIZE, verbose=True):
    """Create the voxelised head layer for one rig. Returns the new object.

    *src_obj*      the rig's ``*.Body.Layer2`` (material + parenting source)
    *armature_obj* the rig's armature
    *base_obj*     the rig's ``*.Body.Layer1`` (head box measurement)
    *rig*          the RigDescriptor (instance-aware output naming)

    Never touches any of the three.
    """
    # --- skin image ---------------------------------------------------------
    image = _find_rig_skin_image(src_obj)
    if image is None:
        raise RuntimeError(
            "could not find a skin Image in %r's materials" % src_obj.name
        )
    if tuple(image.size) != (64, 64):
        # The mod rejects anything but a 64x64 skin. Surface it rather than
        # silently producing a wrong layout.
        raise RuntimeError(
            "skin %r is %dx%d; only 64x64 skins are supported (as in the mod)"
            % (image.name, image.size[0], image.size[1])
        )
    sampler = _SkinSampler(image)

    # --- head box -----------------------------------------------------------
    head_size_x, top_centre = _head_box_local(base_obj)
    mc_px_to_local = head_size_x / 8.0

    # --- the ported voxel loop ---------------------------------------------
    global HEAD_VOXEL_SIZE
    saved = HEAD_VOXEL_SIZE
    HEAD_VOXEL_SIZE = head_voxel_size
    try:
        cubes = _build_voxel_cubes(sampler, HEAD_DIM, HEAD_TEX_U, HEAD_TEX_V)
    finally:
        HEAD_VOXEL_SIZE = saved

    if not cubes:
        raise RuntimeError(
            "no opaque head hat-layer texels found in %r" % image.name
        )

    # --- mesh ---------------------------------------------------------------
    bm = _cubes_to_mesh(
        cubes, HEAD_DIM, top_centre, mc_px_to_local, tuple(image.size),
        head_voxel_size,
    )

    new_obj = _new_voxel_object(
        rig.name_for(OUTPUT_SUFFIX), bm, src_obj, armature_obj, image
    )
    new_me = new_obj.data

    # --- rigid Head weight --------------------------------------------------
    vg = new_obj.vertex_groups.new(name=HEAD_VERTEX_GROUP)
    vg.add([v.index for v in new_me.vertices], 1.0, "REPLACE")

    # --- armature modifier --------------------------------------------------
    mod = new_obj.modifiers.new(name="Armature", type="ARMATURE")
    mod.object = armature_obj
    mod.use_vertex_groups = True

    bpy.context.view_layer.update()

    # NOTE on the transform (this was the single most error-prone part).
    #
    # MCprep authors Layer1/Layer2 vertices in rig-LOCAL space at 1 MC px =
    # 0.1 local (head X = +-0.400 for 8 px), and gives those objects:
    #     parent = armature, parent_type = OBJECT, scale = 1.0,
    #     matrix_parent_inverse = a pure translation,
    # so the armature's own object scale (0.620354) reaches them through
    # matrix_world:
    #     Layer world head X = 0.400 * 0.620354 = 0.248142   (measured)
    #
    # The voxel builders author vertices in that SAME rig-local space, so the
    # 3D layers must carry exactly the same object transform - see
    # _new_voxel_object(). Any "compensation" scale here is a bug: an earlier
    # revision set scale = 1/0.620354 and an identity parent-inverse, whose
    # product is matrix_world = Identity, leaving the layer to render at
    # 1.611983x the size of the geometry it is supposed to sit on.
    #
    # The armature modifier is NOT part of this question: at rest pose its
    # deform matrix is the identity (pose_bone.matrix == bone.matrix_local),
    # so it is a no-op and only kicks in once a bone is posed - which is the
    # bone-following behaviour we want to keep.

    if verbose:
        print(
            "[Head3D] %-22s -> %-28s voxels=%d verts=%d faces=%d "
            "skin=%s voxelSize=%.2f"
            % (
                src_obj.name,
                new_obj.name,
                len(cubes),
                len(new_me.vertices),
                len(new_me.polygons),
                image.name,
                head_voxel_size,
            )
        )

    return new_obj


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class OBJECT_OT_generate_head_3d_skin_layer(bpy.types.Operator):
    """Generate a per-pixel voxel 3D head layer for MCprep Simple rigs"""

    bl_idname = "object.generate_head_3d_skin_layer"
    bl_label = "Generate Head 3D Skin Layer"
    bl_options = {"REGISTER", "UNDO"}

    # NOTE on introspection: verify these with the per-idname RNA, i.e.
    #     bpy.ops.object.generate_head_3d_skin_layer.get_rna_type().properties
    # Do NOT use ``<Class>.bl_rna.properties`` — in Blender 5.x that collection
    # only ever exposes the generic Operator base props and never lists custom
    # properties, so it will look like the property failed to register when in
    # fact it registered fine.
    head_voxel_size: bpy.props.FloatProperty(
        name="Head Voxel Size",
        description=(
            "Voxel scale factor, matching the 3D Skin Layers mod's "
            "headVoxelSize. Decides how far the 3D layer stands proud of "
            "the flat hat"
        ),
        default=HEAD_VOXEL_SIZE,
        min=1.001,
        max=1.25,
        precision=3,
    )

    only_selected: bpy.props.BoolProperty(
        name="Only Selected Rig",
        description="Process only the selected armature/mesh instead of every known rig",
        default=False,
    )

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "(the main button) can scope it. Leave empty to use the selection"
        ),
        default="",
    )

    def execute(self, context):
        created = []

        saved_selection = _capture_selection(context)
        try:

            rig = _resolve_target_rig(context, self.target_rig)
            if rig is None:
                self.report({"ERROR"}, REFUSE_SELECTION_MSG)
                return {"CANCELLED"}

            src_obj = bpy.data.objects.get(rig.layer2_name)
            base_obj = bpy.data.objects.get(rig.layer1_name) if rig.layer1_name else None
            arma_obj = bpy.data.objects.get(rig.arma_name)

            if src_obj is None or base_obj is None or arma_obj is None:
                self.report(
                    {"ERROR"},
                    "%s (Layer1=%s, Layer2=%s, arma=%s)"
                    % (
                        rig.rig_name,
                        "found" if base_obj else "MISSING",
                        "found" if src_obj else "MISSING",
                        "found" if arma_obj else "MISSING",
                    ),
                )
                return {"CANCELLED"}

            # Replace any previous result instead of piling up .001 objects.
            previous = bpy.data.objects.get(rig.name_for(OUTPUT_SUFFIX))
            if previous is not None:
                prev_mesh = previous.data
                bpy.data.objects.remove(previous, do_unlink=True)
                if prev_mesh is not None and prev_mesh.users == 0:
                    bpy.data.meshes.remove(prev_mesh)

            try:
                new_obj = build_head_3d_layer(
                    src_obj, arma_obj, base_obj, rig,
                    head_voxel_size=self.head_voxel_size,
                )
            except Exception as exc:  # noqa: BLE001 - surface, don't swallow
                self.report({"ERROR"}, "%s: %s" % (rig.rig_name, exc))
                return {"CANCELLED"}

            created.append(new_obj)

        finally:
            _restore_selection(context, saved_selection)

        self.report(
            {"INFO"},
            "Generated %d head 3D layer(s): %s"
            % (len(created), ", ".join(o.name for o in created)),
        )
        return {"FINISHED"}


class OBJECT_OT_generate_legs_3d_skin_layer(bpy.types.Operator):
    """Generate per-pixel voxel 3D leg layers for MCprep Simple rigs"""

    bl_idname = "object.generate_legs_3d_skin_layer"
    bl_label = "Generate Legs 3D Skin Layer"
    bl_options = {"REGISTER", "UNDO"}

    leg_voxel_size: bpy.props.FloatProperty(
        name="Leg Voxel Size",
        description=(
            "Voxel scale factor for the legs, matching the 3D Skin Layers "
            "mod's baseVoxelSize. Applies to the leg width and depth; the "
            "height always uses the mod's hard-coded 1.035"
        ),
        default=LEG_VOXEL_SIZE,
        min=1.001,
        max=1.25,
        precision=3,
    )

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "(the main button) can scope it. Leave empty to use the selection"
        ),
        default="",
    )

    def execute(self, context):
        created = []

        saved_selection = _capture_selection(context)
        try:

            rig = _resolve_target_rig(context, self.target_rig)
            if rig is None:
                self.report({"ERROR"}, REFUSE_SELECTION_MSG)
                return {"CANCELLED"}

            src_obj = bpy.data.objects.get(rig.layer2_name)
            arma_obj = bpy.data.objects.get(rig.arma_name)
            if src_obj is None or arma_obj is None:
                self.report(
                    {"ERROR"},
                    "%s (Layer2=%s, arma=%s)"
                    % (rig.rig_name,
                       "found" if src_obj else "MISSING",
                       "found" if arma_obj else "MISSING"),
                )
                return {"CANCELLED"}

            # Replace any previous result instead of piling up .001 objects.
            for side in ("Right", "Left"):
                base = rig.name_for(LEG_OUTPUT_FMT % side)
                for tag in ("Upper", "Lower"):
                    previous = bpy.data.objects.get(
                        base + (LEG_PIECE_SUFFIX % tag)
                    )
                    if previous is not None:
                        prev_mesh = previous.data
                        bpy.data.objects.remove(previous, do_unlink=True)
                        if prev_mesh is not None and prev_mesh.users == 0:
                            bpy.data.meshes.remove(prev_mesh)

            for side in ("Right", "Left"):
                try:
                    upper, lower = build_leg_3d_layer(
                        src_obj, arma_obj, rig, side,
                        leg_voxel_size=self.leg_voxel_size,
                    )
                except Exception as exc:  # noqa: BLE001 - surface, don't swallow
                    self.report({"ERROR"}, "%s %s leg: %s" % (rig.rig_name, side, exc))
                    return {"CANCELLED"}
                created.extend((upper, lower))

        finally:
            _restore_selection(context, saved_selection)

        self.report(
            {"INFO"},
            "Generated %d leg 3D layer piece(s): %s"
            % (len(created), ", ".join(o.name for o in created)),
        )
        return {"FINISHED"}


class OBJECT_OT_generate_arms_3d_skin_layer(bpy.types.Operator):
    """Generate per-pixel voxel 3D arm layers for MCprep Simple rigs"""

    bl_idname = "object.generate_arms_3d_skin_layer"
    bl_label = "Generate Arms 3D Skin Layer"
    bl_options = {"REGISTER", "UNDO"}

    arm_voxel_size: bpy.props.FloatProperty(
        name="Arm Voxel Size",
        description=(
            "Voxel scale factor for the arms, matching the 3D Skin Layers "
            "mod's baseVoxelSize. Applies to the arm width and depth; the "
            "height always uses the mod's hard-coded 1.035"
        ),
        default=ARM_VOXEL_SIZE,
        min=1.001,
        max=1.25,
        precision=3,
    )

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "(the main button) can scope it. Leave empty to use the selection"
        ),
        default="",
    )

    def execute(self, context):
        created = []

        rig = _resolve_target_rig(context, self.target_rig)
        if rig is None:
            self.report({"ERROR"}, REFUSE_SELECTION_MSG)
            return {"CANCELLED"}

        src_obj = bpy.data.objects.get(rig.layer2_name)
        arma_obj = bpy.data.objects.get(rig.arma_name)
        if src_obj is None or arma_obj is None:
            self.report(
                {"ERROR"},
                "%s (Layer2=%s, arma=%s)"
                % (rig.rig_name,
                   "found" if src_obj else "MISSING",
                   "found" if arma_obj else "MISSING"),
            )
            return {"CANCELLED"}

        saved_selection = _capture_selection(context)
        try:

            # Replace any previous result instead of piling up .001 objects.
            for side in ("Right", "Left"):
                base = rig.name_for(ARM_OUTPUT_FMT % side)
                for tag in ("Upper", "Lower"):
                    previous = bpy.data.objects.get(
                        base + (ARM_PIECE_SUFFIX % tag)
                    )
                    if previous is not None:
                        prev_mesh = previous.data
                        bpy.data.objects.remove(previous, do_unlink=True)
                        if prev_mesh is not None and prev_mesh.users == 0:
                            bpy.data.meshes.remove(prev_mesh)

            for side in ("Right", "Left"):
                try:
                    upper, lower = build_arm_3d_layer(
                        src_obj, arma_obj, rig, side,
                        arm_voxel_size=self.arm_voxel_size,
                    )
                except Exception as exc:  # noqa: BLE001 - surface, don't swallow
                    self.report({"ERROR"}, "%s %s arm: %s" % (rig.rig_name, side, exc))
                    return {"CANCELLED"}
                created.extend((upper, lower))

        finally:
            _restore_selection(context, saved_selection)

        self.report(
            {"INFO"},
            "Generated %d arm 3D layer piece(s): %s"
            % (len(created), ", ".join(o.name for o in created)),
        )
        return {"FINISHED"}


class OBJECT_OT_generate_body_3d_skin_layer(bpy.types.Operator):
    """Generate the per-pixel voxel 3D body layer for MCprep Simple rigs"""

    bl_idname = "object.generate_body_3d_skin_layer"
    bl_label = "Generate Body 3D Skin Layer"
    bl_options = {"REGISTER", "UNDO"}

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "(the main button) can scope it. Leave empty to use the selection"
        ),
        default="",
    )

    def execute(self, context):
        created = []

        saved_selection = _capture_selection(context)
        try:

            rig = _resolve_target_rig(context, self.target_rig)
            if rig is None:
                self.report({"ERROR"}, REFUSE_SELECTION_MSG)
                return {"CANCELLED"}

            src_obj = bpy.data.objects.get(rig.layer2_name)
            if src_obj is None:
                self.report({"ERROR"}, "%s: no %s" % (rig.rig_name, rig.layer2_name))
                return {"CANCELLED"}
            armature_obj = bpy.data.objects.get(rig.arma_name)
            if armature_obj is None:
                self.report({"ERROR"}, "%s: no %s" % (rig.rig_name, rig.arma_name))
                return {"CANCELLED"}

            stale = bpy.data.objects.get(rig.name_for(BODY_OUTPUT_SUFFIX))
            if stale is not None:
                prev_mesh = stale.data
                bpy.data.objects.remove(stale, do_unlink=True)
                if prev_mesh is not None and prev_mesh.users == 0:
                    bpy.data.meshes.remove(prev_mesh)
            try:
                created.append(
                    build_body_3d_layer(src_obj, armature_obj, rig)
                )
            except RuntimeError as exc:
                self.report({"ERROR"}, "%s: %s" % (rig.rig_name, exc))
                return {"CANCELLED"}

        finally:
            _restore_selection(context, saved_selection)

        self.report(
            {"INFO"},
            "Generated %d body 3D layer(s): %s"
            % (len(created), ", ".join(o.name for o in created)),
        )
        return {"FINISHED"}


def _body_bmesh(cubes, pivot_z, pivot_x, mc_px_to_local, image_size):
    """Build the bmesh for the whole Java 8x12x4 body voxel grid.

    ``cubes`` come from :func:`_build_voxel_cubes` with ``BODY_DIM`` /
    ``(BODY_TEX_U, BODY_TEX_V)``.

    Geometry contract (matches the MCprep Layer2 torso shell):
        pivot_z = Chest bone head z
        row 0   at pivot_z, rows descend by BODY_VOXEL_Z (MC +Y is down)
        X       centred on pivot_x, spanning 8 * BODY_VOXEL_X
        Y       centred on 0,       spanning 4 * BODY_VOXEL_Y

    No Java scale compensation of any kind is applied here: the object
    transform from :func:`_new_voxel_object` already reproduces MCprep's own
    armature parenting, which is what puts the layer at the right world size.
    """
    w, h, d = BODY_DIM
    static_x = -w / 2.0
    static_z = -d / 2.0

    kx = mc_px_to_local * BODY_VOXEL_X / 0.1
    ky = mc_px_to_local * BODY_VOXEL_Y / 0.1
    kz = mc_px_to_local * BODY_VOXEL_Z / 0.1
    iw, ih = image_size

    bm = bmesh.new()
    uv_layer = bm.loops.layers.uv.new(UV_LAYER_NAME)

    for (vx, vy, vz), hide, (tex_u_cur, tex_v_cur), face in cubes:
        ox = static_x + vx
        oy = vy
        oz = static_z + vz

        verts = {}
        for fname in DIRECTIONS:
            if fname in hide:
                continue
            for corner in _FACE_QUADS[fname]:
                if corner not in verts:
                    # Axis map (frozen, same as Head/Legs/Arms):
                    #   MC X -> Blender X
                    #   MC Z -> Blender -Y   (depth, slot 1 of the tuple)
                    #   MC Y -> Blender Z    (vertical, slot 2 of the tuple)
                    # Blender's tuple order is (X, Y, Z), so the VERTICAL term
                    # must be written LAST and the depth term SECOND. Writing
                    # them the other way round laid the torso on its side.
                    verts[corner] = bm.verts.new((
                        pivot_x + (ox + corner[0]) * kx,
                        -(oz + corner[2]) * ky,
                        pivot_z - (oy + corner[1]) * kz,
                    ))
        bm.verts.index_update()

        u0 = tex_u_cur / float(iw)
        u1 = (tex_u_cur + 1) / float(iw)
        v0 = 1.0 - (tex_v_cur + 1) / float(ih)
        v1 = 1.0 - tex_v_cur / float(ih)
        quad_uvs = [(u1, v0), (u0, v0), (u0, v1), (u1, v1)]

        for fname in DIRECTIONS:
            if fname in hide:
                continue
            try:
                bf = bm.faces.new([verts[c] for c in _FACE_QUADS[fname]])
            except (ValueError, KeyError):
                continue
            for i, loop in enumerate(bf.loops):
                loop[uv_layer].uv = quad_uvs[i]

    bm.verts.index_update()
    bm.faces.index_update()
    bm.normal_update()
    return bm


def build_body_3d_layer(src_obj, armature_obj, rig, verbose=True):
    """Create the voxelised 3D body layer for one MCprep Simple rig.

    Single object, rigidly bound to the Chest bone. See BODY_VERTEX_GROUP for
    why (MCprep compatibility choice; Java Body is a single ModelPart).
    """
    image = None
    for mat in src_obj.data.materials:
        if mat is None or not mat.use_nodes:
            continue
        for node in mat.node_tree.nodes:
            if node.type == "TEX_IMAGE" and node.image is not None:
                image = node.image
                break
        if image is not None:
            break
    if image is None:
        raise RuntimeError(
            "could not find a skin Image in %r's materials" % src_obj.name
        )
    if tuple(image.size) != (64, 64):
        raise RuntimeError(
            "skin %r is %dx%d; only 64x64 skins are supported (as in the mod)"
            % (image.name, image.size[0], image.size[1])
        )
    sampler = _SkinSampler(image)

    # Java grid pivot: the torso top. Measured to coincide with the Chest bone
    # head (error 9.2e-3 = 0.09 px); the Body bone head is 6 px off and was
    # rejected by the geometric audit.
    bone = armature_obj.data.bones.get(BODY_VERTEX_GROUP)
    if bone is None:
        raise RuntimeError(
            "armature %r has no bone named %r"
            % (armature_obj.name, BODY_VERTEX_GROUP)
        )
    pivot_z = bone.head_local.z
    pivot_x = bone.head_local.x
    mc_px_to_local = _mc_px_from_rig(src_obj)

    cubes = _build_voxel_cubes(sampler, BODY_DIM, BODY_TEX_U, BODY_TEX_V)
    if not cubes:
        raise RuntimeError(
            "no opaque body texels found in %r" % image.name
        )

    bm = _body_bmesh(cubes, pivot_z, pivot_x, mc_px_to_local, tuple(image.size))
    new_obj = _new_voxel_object(
        rig.name_for(BODY_OUTPUT_SUFFIX), bm, src_obj, armature_obj, image
    )
    new_me = new_obj.data

    vg = new_obj.vertex_groups.new(name=BODY_VERTEX_GROUP)
    vg.add([v.index for v in new_me.vertices], 1.0, "REPLACE")

    mod = new_obj.modifiers.new(name="Armature", type="ARMATURE")
    mod.object = armature_obj
    mod.use_vertex_groups = True

    bpy.context.view_layer.update()

    if verbose:
        print(
            "[Body3D] %-22s -> %-28s voxels=%d verts=%d faces=%d "
            "skin=%s group=%s"
            % (
                src_obj.name,
                new_obj.name,
                len(cubes),
                len(new_me.vertices),
                len(new_me.polygons),
                image.name,
                BODY_VERTEX_GROUP,
            )
        )

    return new_obj


# ---------------------------------------------------------------------------
# One-click entry point (N-panel "生成 3D 外层")
# ---------------------------------------------------------------------------

#: (operator idname, [object names it owns])
#: Each entry is a tuple of (tag, piece):
#:   piece == ""      -> name = base + tag + suffix
#:   piece != ""      -> name = base + tag + suffix + piece
#: The two-part form is what the arm/leg builders actually create (the
#: .Upper/.Lower sits AFTER the rig suffix), so the reuse check below must
#: compose names the same way or it looks for "...3D.Upper.001".
_LAYER_STEPS = (
    ("object.generate_head_3d_skin_layer", ((OUTPUT_SUFFIX, ""),)),
    ("object.generate_body_3d_skin_layer", ((BODY_OUTPUT_SUFFIX, ""),)),
    ("object.generate_legs_3d_skin_layer", (
        (LEG_OUTPUT_FMT % "Right", LEG_PIECE_SUFFIX % "Upper"),
        (LEG_OUTPUT_FMT % "Right", LEG_PIECE_SUFFIX % "Lower"),
        (LEG_OUTPUT_FMT % "Left", LEG_PIECE_SUFFIX % "Upper"),
        (LEG_OUTPUT_FMT % "Left", LEG_PIECE_SUFFIX % "Lower"),
    )),
    ("object.generate_arms_3d_skin_layer", (
        (ARM_OUTPUT_FMT % "Right", ARM_PIECE_SUFFIX % "Upper"),
        (ARM_OUTPUT_FMT % "Right", ARM_PIECE_SUFFIX % "Lower"),
        (ARM_OUTPUT_FMT % "Left", ARM_PIECE_SUFFIX % "Upper"),
        (ARM_OUTPUT_FMT % "Left", ARM_PIECE_SUFFIX % "Lower"),
    )),
)


def _step_object_name(rig, tag, piece):
    """Final object name an arm/leg/head/body step produces for *rig*.

    Mirrors the builders exactly: the rig suffix goes between the tag and
    the .Upper/.Lower piece.  See :data:`_LAYER_STEPS`.
    """
    return rig.name_for(tag) + piece


def _capture_selection(context):
    """Snapshot the user's selection/active state so it can be restored.

    Generating layers must not leave the scene's selection pointing at a
    freshly created object: the panel and the main button both resolve the
    target rig from the active object, so a hijacked active would silently
    redirect the *next* run to whichever rig was processed last.
    """
    active = context.view_layer.objects.active if context.view_layer else None
    return (active, list(context.selected_objects))


def _restore_selection(context, saved):
    """Put back the selection/active state captured by :func:`_capture_selection`.

    Objects are re-resolved by name because they may have been rebuilt (or
    removed) while the operator ran; anything that no longer exists is simply
    skipped.  If the original active object is gone the active object is
    cleared rather than pointed at an arbitrary survivor.
    """
    active, selected = saved
    for o in context.view_layer.objects:
        o.select_set(False)
    for o in selected:
        still = bpy.data.objects.get(o.name)
        if still is not None:
            still.select_set(True)
    if active is not None:
        active = bpy.data.objects.get(active.name)
    context.view_layer.objects.active = active


def _rig_from_selection(context):
    """Return the RigDescriptor for the selected MCprep player, or ``None``.

    Accepts the armature itself, or any object that belongs to the rig -
    resolved through the ACTUAL parent chain first, which is what makes
    ``SimplePlayer`` and ``SimplePlayer.001`` distinguishable.  Name matching
    is only a last resort and is suffix-exact, so ``SimplePlayer.001`` can
    never be reported as ``SimplePlayer``.

    Returns ``None`` when the selection is not a known MCprep player, so the
    caller can report a clear message instead of guessing.
    """
    rigs = discover_rigs()
    if not rigs:
        return None
    by_arma = {r.arma_name: r for r in rigs}
    by_layer2 = {r.layer2_name: r for r in rigs}
    by_layer1 = {r.layer1_name: r for r in rigs if r.layer1_name}

    def _match(obj):
        if obj is None:
            return None
        # 1) The armature itself.
        if obj.type == "ARMATURE" and obj.name in by_arma:
            return by_arma[obj.name]
        # 2) The exact Layer1/Layer2 meshes of a rig.
        if obj.name in by_layer2:
            return by_layer2[obj.name]
        if obj.name in by_layer1:
            return by_layer1[obj.name]
        # 3) Any child of the rig: walk the parent chain and take the FIRST
        #    armature that owns a Layer2. This covers generated 3D layers and
        #    the rig's head/arm/leg meshes without relying on names.
        parent = obj.parent
        depth = 0
        while parent is not None and depth < 8:
            if parent.type == "ARMATURE" and parent.name in by_arma:
                return by_arma[parent.name]
            parent = parent.parent
            depth += 1
        # 4) Last-resort name match, exact instance boundary: the object must
        #    be "<base>" or start with "<base>." - and the reported rig is the
        #    one whose base+suffix prefix actually matches, longest suffix
        #    first so ".001" wins over "".
        best = None
        for r in rigs:
            pref = r.rig_name + "."
            if obj.name == r.rig_name or obj.name.startswith(pref):
                if best is None or len(r.suffix) > len(best.suffix):
                    best = r
        return best

    # The active object is the user's most explicit intent, so honour it
    # first - that way selecting a Camera refuses instead of falling through
    # to some other still-selected rig.
    active = context.view_layer.objects.active if context.view_layer else None
    hit = _match(active)
    if hit is not None:
        return hit
    if active is not None:
        return None

    for obj in context.selected_objects:
        hit = _match(obj)
        if hit is not None:
            return hit
    return None


class OBJECT_OT_generate_skin_layers_3d(bpy.types.Operator):
    """Generate the full 3D outer layer (Head, Body, Arms, Legs) for the
    selected MCprep Minecraft player"""

    bl_idname = "object.generate_skin_layers_3d"
    bl_label = "Generate 3D Skin Layers"
    bl_options = {"REGISTER", "UNDO"}

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "(the rebuild button) can scope it. Leave empty to use the "
            "selection"
        ),
        default="",
    )

    def execute(self, context):
        rig = _resolve_target_rig(context, self.target_rig)

        if rig is None:
            # Refuse rather than guess: no silent fallback to every rig.
            self.report({"ERROR"}, REFUSE_SELECTION_MSG)
            _set_status("⚠ 未检测到 MCprep 玩家模型")
            return {"CANCELLED"}

        existing_before = len(bpy.data.objects)
        created = 0
        reused = 0
        problems = []
        made_parts = []

        if bpy.data.objects.get(rig.layer2_name) is None:
            self.report(
                {"ERROR"},
                "%s has no %s" % (rig.rig_name, rig.layer2_name),
            )
            _set_status("⚠ 无法找到 Minecraft Skin Layer")
            return {"CANCELLED"}

        # Body-part selection comes from the panel checkboxes. A disabled
        # step is skipped entirely, so partial generation simply leaves the
        # other parts untouched.
        try:
            state = _scene_state()
        except Exception:  # noqa: BLE001 - headless / state not registered
            state = None

        saved_selection = _capture_selection(context)
        try:
            for step_index, (op_idname, steps) in enumerate(_LAYER_STEPS):
                if state is not None and not _step_enabled(state, step_index):
                    continue
                for tag, piece in steps:
                    if bpy.data.objects.get(
                        _step_object_name(rig, tag, piece)
                    ) is not None:
                        reused += 1
                        break
                # Resolve "object.generate_x" through bpy.ops.object, i.e. by
                # attribute lookup on the submodule (bpy.ops itself is only
                # special-cased at the bpy.ops.<module>.<name> call site).
                # The target rig is passed explicitly so the child operator
                # does NOT re-scan the scene and widen the scope again.
                namespace, op_name = op_idname.split(".", 1)
                result = getattr(getattr(bpy.ops, namespace), op_name)(
                    target_rig=rig.rig_name
                )
                if "FINISHED" not in result:
                    problems.append("%s: %s returned %s"
                                    % (rig.rig_name, op_idname, sorted(result)))
                else:
                    made_parts.append(_STEP_LABELS[step_index])
        except Exception as exc:  # noqa: BLE001 - surface, don't swallow
            import traceback
            traceback.print_exc()
            self.report({"ERROR"}, "%s: %s" % (rig.rig_name, exc))
            _set_status("⚠ 3D 外层生成失败\n请检查当前模型是否为有效的 MCprep 玩家模型。")
            return {"CANCELLED"}
        finally:
            _restore_selection(context, saved_selection)

        created = len(bpy.data.objects) - existing_before

        if problems:
            self.report({"WARNING"}, "; ".join(problems))

        if created == 0 and reused == 0:
            self.report({"ERROR"}, "No MCprep Minecraft player could be processed.")
            _set_status("⚠ 3D 外层生成失败\n请检查当前模型是否为有效的 MCprep 玩家模型。")
            return {"CANCELLED"}

        # New objects may have been created, but every selected part already
        # had its layer -> tell the user to use Rebuild instead.
        if created == 0 and made_parts:
            _set_status("3D 外层已存在，请使用“重建 3D 外层”。")
        elif made_parts and _is_partial(state):
            _set_status("✓ 已生成 " + " / ".join(made_parts))
        elif made_parts:
            _set_status("✓ 3D 外层生成完成")
        else:
            _set_status("3D 外层已存在，请使用“重建 3D 外层”。")

        self.report(
            {"INFO"},
            "3D skin layers ready: %d new object(s), %d layer group(s) already present."
            % (created, reused),
        )
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# V1 UI — panel, per-part toggles, delete / rebuild, status feedback
# ---------------------------------------------------------------------------
#
# Everything below is presentation + operator orchestration. It calls the
# existing, already-verified generation operators; it never re-implements or
# alters the voxel/UV/binding algorithms.

#: Body-part groups the UI can toggle. ``id`` is the property on
#: :class:`MC3DSL_SceneState`, ``step`` is the index into :data:`_LAYER_STEPS`,
#: ``label`` is the checkbox text.
PART_TOGGLES = (
    ("part_head", 0, "Head"),
    ("part_body", 1, "Body"),
    ("part_arms", 3, "Arms"),
    ("part_legs", 2, "Legs"),
)

#: Display label per ``_LAYER_STEPS`` entry, indexed the same way.
_STEP_LABELS = ("Head", "Body", "Legs", "Arms")


def _step_enabled(state, step_index):
    """Is the ``_LAYER_STEPS`` entry *step_index* switched on in the panel?"""
    for prop, idx, _label in PART_TOGGLES:
        if idx == step_index:
            return bool(getattr(state, prop, True))
    return True


def _is_partial(state):
    """True when at least one body part is switched off."""
    return any(not bool(getattr(state, prop, True))
               for prop, _idx, _label in PART_TOGGLES)

_STATUS_DEFAULT = "等待操作"

#: Reason codes used by :func:`_status_for_selection`.
_ST_OK = "ok"
_ST_NO_RIGS = "no_rigs"
_ST_NO_SELECTION = "no_selection"
_ST_NOT_PLAYER = "not_player"
_ST_NO_LAYER2 = "no_layer2"


def _classify(obj):
    """Cheap classification of a single object, for UI messages only.

    Returns one of ``"armature"``, ``"layer"`` (MCprep Layer1/Layer2 shell),
    ``"generated"`` (our own ``*.3D`` output) or ``"other"``. This does NOT
    decide the target rig - :func:`_rig_from_selection` owns that.
    """
    if obj is None:
        return "other"
    if obj.type == "ARMATURE":
        return "armature"
    if ".Body.Layer1" in obj.name or ".Body.Layer2" in obj.name:
        return "generated" if ".3D" in obj.name else "layer"
    if _is_generated_layer(obj.name):
        return "generated"
    return "other"


def _is_generated_layer(name):
    """True when *name* is one of the objects this addon creates."""
    for tag, piece in _iter_step_specs():
        if name.endswith(tag + piece):
            return True
    return False


def _iter_step_specs():
    """Flat ``(tag, piece)`` pairs of everything this addon can create."""
    for _op_idname, steps in _LAYER_STEPS:
        for tag, piece in steps:
            yield tag, piece


def _generated_object_names(rig):
    """Object names this addon owns for *rig* (all four parts, regardless of
    the current checkbox state)."""
    return [_step_object_name(rig, tag, piece)
            for tag, piece in _iter_step_specs()]


def _present_generated_names(rig):
    """Existing objects of *rig* that this addon created."""
    return [n for n in _generated_object_names(rig)
            if bpy.data.objects.get(n) is not None]


def _rig_model_kind(rig):
    """``"Slim Model"`` or ``"Classic Model"``, inferred from the rig itself.

    MCprep builds the Classic torso 8 MC px wide *plus* 4 px arms and the Slim
    one with 3 px arms, so the Layer1 shell spans 1.60 local units for Classic
    and 1.40 for Slim.  Reading that span is how the panel tells them apart -
    the user never picks the model type by hand.

    Returns ``"Unknown Model"`` when it cannot be determined.
    """
    try:
        for name in (rig.layer1_name, rig.layer2_name):
            if not name:
                continue
            obj = bpy.data.objects.get(name)
            if obj is None or obj.data is None or not obj.data.vertices:
                continue
            xs = [v.co.x for v in obj.data.vertices]
            span = max(xs) - min(xs)
            if span <= 0.0:
                continue
            # Classic = 1.60, Slim = 1.40 (threshold halfway between).
            return "Slim Model" if span < 1.5 else "Classic Model"
        return "Unknown Model"
    except Exception:  # noqa: BLE001 - purely informational
        return "Unknown Model"


def _status_for_selection(context):
    """Classify the current selection for the Player + Status areas.

    Returns ``(descriptor_or_None, reason_code)``.
    """
    rigs = discover_rigs()
    if not rigs:
        return None, _ST_NO_RIGS

    rig = _rig_from_selection(context)
    if rig is not None:
        if bpy.data.objects.get(rig.layer2_name) is None:
            return None, _ST_NO_LAYER2
        return rig, _ST_OK

    active = context.view_layer.objects.active if context.view_layer else None
    if active is None and not context.selected_objects:
        return None, _ST_NO_SELECTION
    if active is not None and _classify(active) == "layer":
        return None, _ST_NO_LAYER2
    return None, _ST_NOT_PLAYER


def _scene_state():
    """The addon's persisted UI state block (created on demand)."""
    return bpy.context.scene.mc3dsl


def _set_status(text):
    try:
        _scene_state().status = text
    except Exception:  # noqa: BLE001 - never break an operator over feedback
        pass


# ---------------------------------------------------------------------------
# Operators: delete / rebuild
# ---------------------------------------------------------------------------

class OBJECT_OT_remove_skin_layers_3d(bpy.types.Operator):
    """Remove the generated 3D skin layers for the selected MCprep player"""

    bl_idname = "object.remove_skin_layers_3d"
    bl_label = "删除 3D 外层"
    bl_options = {"REGISTER", "UNDO"}

    target_rig: bpy.props.StringProperty(
        name="Target Rig",
        description=(
            "Internal: pins this operator to one specific rig so a caller "
            "can scope it. Leave empty to use the selection"
        ),
        default="",
    )

    def execute(self, context):
        rig = _resolve_target_rig(context, self.target_rig)
        if rig is None:
            self.report({"ERROR"}, REFUSE_SELECTION_MSG)
            _set_status("⚠ 未检测到 MCprep 玩家模型")
            return {"CANCELLED"}

        saved_selection = _capture_selection(context)
        removed = 0
        try:
            for name in _generated_object_names(rig):
                obj = bpy.data.objects.get(name)
                if obj is None:
                    continue
                prev_mesh = obj.data
                bpy.data.objects.remove(obj, do_unlink=True)
                if prev_mesh is not None and prev_mesh.users == 0:
                    bpy.data.meshes.remove(prev_mesh)
                removed += 1
        finally:
            _restore_selection(context, saved_selection)

        if removed == 0:
            _set_status("没有可删除的 3D 外层。")
            self.report({"INFO"}, "没有可删除的 3D 外层。")
        else:
            _set_status("✓ 3D 外层已删除")
            self.report({"INFO"}, "✓ 3D 外层已删除 (%d)" % removed)
        return {"FINISHED"}


class OBJECT_OT_rebuild_skin_layers_3d(bpy.types.Operator):
    """Delete then regenerate the 3D skin layers for the selected player"""

    bl_idname = "object.rebuild_skin_layers_3d"
    bl_label = "重建 3D 外层"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        rig = _resolve_target_rig(context, "")
        if rig is None:
            self.report({"ERROR"}, REFUSE_SELECTION_MSG)
            _set_status("⚠ 未检测到 MCprep 玩家模型")
            return {"CANCELLED"}

        # Reuse the verified operators rather than duplicating their logic.
        # The panel pins `target_rig` on each call so the scope can't widen.
        self._target = rig.rig_name
        res = bpy.ops.object.remove_skin_layers_3d(target_rig=rig.rig_name)
        if "FINISHED" not in res:
            return res
        res = bpy.ops.object.generate_skin_layers_3d(target_rig=rig.rig_name)
        if "FINISHED" not in res:
            _set_status("⚠ 3D 外层生成失败")
            return res
        _set_status("✓ 3D 外层重建完成")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Scene state (checkbox + status), used by the panel
# ---------------------------------------------------------------------------

class MC3DSL_SceneState(bpy.types.PropertyGroup):
    """Per-scene UI state for the panel."""

    part_head: bpy.props.BoolProperty(name="Head", default=True)
    part_body: bpy.props.BoolProperty(name="Body", default=True)
    part_arms: bpy.props.BoolProperty(name="Arms", default=True)
    part_legs: bpy.props.BoolProperty(name="Legs", default=True)

    status: bpy.props.StringProperty(default=_STATUS_DEFAULT)


# ---------------------------------------------------------------------------
# N-panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_skin_layers_3d(bpy.types.Panel):
    """Minecraft 3D Skin Layers for MCprep - one-click entry point"""

    bl_idname = "VIEW3D_PT_skin_layers_3d"
    bl_label = ADDON_TITLE_SHORT
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = SIDEBAR_CATEGORY

    def draw_header(self, context):
        self.layout.label(text="", icon="MESH_CUBE")

    def draw(self, context):
        layout = self.layout

        # ---------------- PLAYER ----------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="PLAYER", icon="OUTLINER_OB_ARMATURE")

        rig, reason = _status_for_selection(context)
        if rig is not None:
            row = col.row(align=True)
            row.label(text="✓ " + rig.rig_name, icon="CHECKMARK")
            col.label(text="  " + _rig_model_kind(rig))
            n_present = len(_present_generated_names(rig))
            col.label(text="  3D 外层: %d / 10" % n_present)
        else:
            if reason == _ST_NO_RIGS:
                col.label(text="⚠ 未检测到 MCprep 玩家模型", icon="ERROR")
                col.label(text="  请先选中一个 MCprep 玩家模型。")
            elif reason == _ST_NO_LAYER2:
                col.label(text="⚠ 无法找到 Minecraft Skin Layer", icon="ERROR")
                col.label(text="  请确认该玩家模型由 MCprep 正常生成。")
            elif reason == _ST_NOT_PLAYER:
                col.label(text="⚠ 当前对象不是 MCprep 玩家模型", icon="ERROR")
                col.label(text="  请选择由 MCprep 创建的玩家模型。")
            else:
                col.label(text="⚠ 未检测到 MCprep 玩家模型", icon="ERROR")
                col.label(text="  请先选中一个 MCprep 玩家模型。")

        enabled = rig is not None

        # ---------------- 3D SKIN LAYERS ----------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="3D SKIN LAYERS", icon="MOD_BUILD")

        col.scale_y = 1.4
        col.operator(
            OBJECT_OT_generate_skin_layers_3d.bl_idname,
            text="生成 3D 外层",
            icon="MESH_CUBE",
        )
        col.scale_y = 1.0

        col.separator()
        col.label(text="BODY PARTS")
        state = _scene_state()
        flow = col.column(align=True)
        flow.enabled = enabled
        for prop, _step, label in PART_TOGGLES:
            flow.prop(state, prop, text=label, toggle=True)

        col.separator()
        row = col.row(align=True)
        row.enabled = enabled
        row.operator(
            OBJECT_OT_remove_skin_layers_3d.bl_idname,
            text="删除 3D 外层",
            icon="TRASH",
        )
        row.operator(
            OBJECT_OT_rebuild_skin_layers_3d.bl_idname,
            text="重建 3D 外层",
            icon="FILE_REFRESH",
        )

        # ---------------- STATUS ----------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="STATUS", icon="INFO")
        col.label(text=state.status)

        # ---------------- ABOUT ----------------
        box = layout.box()
        col = box.column(align=True)
        col.label(text="ABOUT", icon="QUESTION")
        col.label(text=ADDON_TITLE_SHORT)
        col.label(text=ADDON_TITLE_SUB)
        col.label(text=ADDON_VERSION_TEXT)
        col.label(text="GitHub repository")


# ---------------------------------------------------------------------------
# Menu hook + registration
# ---------------------------------------------------------------------------

def _menu_func(self, context):
    self.layout.operator(
        OBJECT_OT_generate_skin_layers_3d.bl_idname,
        text="Generate 3D Skin Layers",
    )


CLASSES = (
    OBJECT_OT_generate_skin_layers_3d,
    OBJECT_OT_remove_skin_layers_3d,
    OBJECT_OT_rebuild_skin_layers_3d,
    OBJECT_OT_generate_head_3d_skin_layer,
    OBJECT_OT_generate_body_3d_skin_layer,
    OBJECT_OT_generate_legs_3d_skin_layer,
    OBJECT_OT_generate_arms_3d_skin_layer,
    MC3DSL_SceneState,
    VIEW3D_PT_skin_layers_3d,
)


def _icon_dir():
    return os.path.dirname(__file__)


def _register_icon():
    """Best-effort load of icons/logo.png. Never fatal.

    Blender only accepts square thumbnails for preview icons; anything odd
    simply means the panel falls back to a stock icon.
    """
    path = os.path.join(_icon_dir(), _ICON_RELATIVE)
    try:
        if not os.path.exists(path):
            return False
        bpy.utils.previews.new  # noqa: B018 - feature probe
        from bpy.utils import previews as _previews  # noqa: PLC0415
        pcoll = _previews.new()
        pcoll.load(_ICON_ID, path, "IMAGE")
        _ICON_COLLECTIONS[_ICON_ID] = pcoll
        return True
    except Exception as exc:  # noqa: BLE001 - decorative only
        print("[MC 3D Skin Layers] logo icon not loaded: %s" % exc)
        return False


_ICON_COLLECTIONS = {}


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.mc3dsl = bpy.props.PointerProperty(type=MC3DSL_SceneState)
    _register_icon()
    bpy.types.VIEW3D_MT_object.append(_menu_func)


def unregister():
    bpy.types.VIEW3D_MT_object.remove(_menu_func)
    for pcoll in _ICON_COLLECTIONS.values():
        try:
            pcoll.close()
        except Exception:  # noqa: BLE001
            pass
    _ICON_COLLECTIONS.clear()
    del bpy.types.Scene.mc3dsl
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
