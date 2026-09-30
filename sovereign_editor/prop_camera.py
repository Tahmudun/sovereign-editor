"""Game-camera views of a map context (software preview; NOT native acceptance).

Pinned field camera (ov01 camera table entry 0): elevation 48.66 deg, 1 screen pixel
per world unit at the target; the 16 deg perspective is approximated orthographically.
A player stand-in is a camera-facing quad whose depth is that of an overworld sprite:
props draw with the normal projection, then sprites draw as full billboards with the
projection depth shifted by 8 * cos(pitch) = 5.28 units toward the camera (pret
fieldmap.c ov01_021E6220), their quad centred about 16 units above the feet. In this
orthographic approximation that is the feet point moved 0.751 * 16 + 5.28 = 17.3 units
along the view axis (qualified against r82 native screenshots: a player one row north of
a ground-level card drew over it, two rows north stayed hidden). Lighting, fog, sprites
and animation are not emulated.
"""
import math
from dataclasses import replace

import numpy as np

from . import mapscene
from .nitro import Primitive
from .raster import render

PITCH = math.radians((0x10000 - 0xDD62) * 360 / 0x10000)
BIAS = 16 * math.sin(PITCH) + 8 * math.cos(PITCH)      # sprite centre height + depth shift
W, H = 256, 192


def camera():
    c, s = math.cos(PITCH), math.sin(PITCH)
    return np.array([[1, 0, 0], [0, -c, s], [0, s, c]], float)


def scene_prims(project, header, cell):
    context = project.context(header=header, cell=cell)
    description = mapscene.scene(project, header=header, cell=cell)
    refs, blobs = mapscene.tilesets(project, context)
    prims = mapscene.scene_primitives(project, context, description['placements'], blobs, refs['building_models']['archive'])
    return context, description, prims


def stand_in(tile, height, colour=(60, 90, 200)):
    """Camera-facing 16x24 px quad with a light outline, standing on ``tile`` (cell-local)."""
    c, s = math.cos(PITCH), math.sin(PITCH)
    up = np.array([0, math.sin(math.pi / 2 - PITCH), -math.cos(math.pi / 2 - PITCH)])
    towards = np.array([0, s, c])                      # unit vector toward the camera
    base = np.array([(tile[0] + 0.5) * 16, height * 16, (tile[1] + 0.5) * 16]) + towards * BIAS
    size = 24 / 1.0
    corners = [base + [-8, 0, 0], base + [8, 0, 0], base + [8, 0, 0] + up * size, base + [-8, 0, 0] + up * size]
    tex = np.zeros((8, 8, 4), np.uint8); tex[:, :, :3] = colour; tex[:, :, 3] = 255
    tex[0, :, :3] = tex[-1, :, :3] = tex[:, 0, :3] = tex[:, -1, :3] = 255
    return Primitive(vertices=np.array(corners, float), colors=np.ones((4, 3)), uvs=np.array([[0, 1], [1, 1], [1, 0], [0, 0]], float),
                     triangles=np.array([[0, 1, 2], [0, 2, 3]]), texture=tex,
                     material={'alpha': 1.0, 'repeat': [False, False], 'mirror': [False, False], 'name': 'stand-in'})


def frame(prims, focus, height):
    """256x192 view whose centre is cell-local tile ``focus`` at ground ``height``."""
    proj = camera()
    centre = proj @ np.array([(focus[0] + 0.5) * 16, height * 16, (focus[1] + 0.5) * 16])
    shift = np.linalg.solve(proj, np.array([W / 2, H / 2, 0]) - np.array([centre[0], centre[1], 0]))
    moved = [replace(p, vertices=p.vertices + shift) for p in prims]
    return render(moved, (W, H), proj, background=(0, 0, 0))
