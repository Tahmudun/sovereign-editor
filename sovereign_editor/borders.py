"""Stock-compatible border pieces for authored paths and tall grass (PROD-VIS-001).

Pure geometry rules; no IO. Derived from, and checked against, the pinned stock
maps (evidence/production-authoring-v2/border-rules.json):

* Tall grass is an ``egrass`` plane two units above ground and shifted two units
  south, like every stock patch. Around the patch, each half-tile (8 units) of the
  ring gets one stock piece: ``egrass_u`` (grass above/below), ``egrass_v`` (left/
  right), ``egrass_ro`` (diagonal only, outer corner) or ``egrass_ri`` (grass on two
  orthogonal sides, inner corner). The texture quadrant follows the half-tile's
  parity (inverted for inner corners), exactly as in 88 stock maps.
* A path (``road01``) gets a full-tile ``road01_r`` rim ring: straight pieces from
  the left half of the rim atlas (u 0.01..0.49, v running along the edge), inner
  corners from its upper-right quadrant and outer corners from its lower-right
  quadrant, each rotated toward the path with a proper rotation.

Configurations the stock atlas cannot draw (a rim tile between two path segments,
or touching three sides) are refused before any write.
"""
import numpy as np

from .formats import require

GRASS_FAMILY = ('egrass', 'egrass_u', 'egrass_v', 'egrass_ro', 'egrass_ri')
PATH_FAMILY = ('road01', 'road01_r')
GRASS_RISE = 2      # model units above the ground plane
GRASS_SHIFT = 2     # model units south, as stock patches
SIDES = {'N': (0, -1), 'S': (0, 1), 'W': (-1, 0), 'E': (1, 0)}
DIAGONALS = {'NW': (-1, -1), 'NE': (1, -1), 'SW': (-1, 1), 'SE': (1, 1)}
# Rim frames: unit vectors (x, z) of increasing u (toward the path) and v.
FRAMES = {'straight': {'N': ((0, -1), (1, 0)), 'S': ((0, 1), (-1, 0)), 'E': ((1, 0), (0, 1)), 'W': ((-1, 0), (0, -1))},
          'corner': {'NE': ((0, 1), (-1, 0)), 'NW': ((1, 0), (0, 1)), 'SE': ((-1, 0), (0, -1)), 'SW': ((0, -1), (1, 0))}}


# ---- tall grass ------------------------------------------------------------------------

def grass_pieces(tiles):
    """(interior, ring) for a set of grass tiles in one coordinate frame.

    Interior pieces carry ``tile``; ring pieces carry ``half`` (half-tile lattice
    index) and a stock UV quadrant ``uv`` for corners (x0,z0),(x1,z0),(x1,z1),(x0,z1).
    """
    tiles = set(tiles)
    require(tiles, 'Choose at least one tall-grass tile', 'INVALID_INPUT')

    def inside(hx, hz):
        return (hx // 2, hz // 2) in tiles
    interior = [{'material': 'egrass', 'tile': t} for t in sorted(tiles)]
    ring = []
    candidates = sorted({(tx * 2 + dx, tz * 2 + dz) for tx, tz in tiles for dx in range(-1, 3) for dz in range(-1, 3)})
    for hx, hz in candidates:
        if inside(hx, hz):
            continue
        n, s, w, e = inside(hx, hz - 1), inside(hx, hz + 1), inside(hx - 1, hz), inside(hx + 1, hz)
        if (n or s) and (w or e):
            material = 'egrass_ri'
        elif n or s:
            material = 'egrass_u'
        elif w or e:
            material = 'egrass_v'
        elif any(inside(hx + dx, hz + dz) for dx, dz in DIAGONALS.values()):
            material = 'egrass_ro'
        else:
            continue
        ring.append({'material': material, 'half': (hx, hz), 'uv': half_uv(material, hx, hz)})
    return interior, ring


def half_uv(material, hx, hz):
    """Stock texture coordinates for one ring half-tile (corner order x0z0,x1z0,x1z1,x0z1)."""
    even_x, even_z = hx % 2 == 0, hz % 2 == 0
    if material == 'egrass_ri':
        even_x, even_z = not even_x, not even_z
    u0, v0 = (0.5 if even_x else 0.0), (0.5 if even_z else 0.0)
    if material == 'egrass_u':          # repeats along x: continuous u, quadrant v
        u0 = hx * 0.5
    if material == 'egrass_v':          # repeats along z: continuous v, quadrant u
        v0 = hz * 0.5
    return [(u0, v0), (u0 + 0.5, v0), (u0 + 0.5, v0 + 0.5), (u0, v0 + 0.5)]


# ---- path rims ---------------------------------------------------------------------------

def rim_pieces(tiles, allow_unsupported=False):
    """Rim ring of a path tile set: one piece per neighboring tile, or a refusal."""
    tiles = set(tiles)
    require(tiles, 'Choose at least one path tile', 'INVALID_INPUT')
    ring = []
    candidates = sorted({(x + dx, z + dz) for x, z in tiles for dx in (-1, 0, 1) for dz in (-1, 0, 1)} - tiles)
    for x, z in candidates:
        sides = [d for d, (dx, dz) in SIDES.items() if (x + dx, z + dz) in tiles]
        diagonals = [d for d, (dx, dz) in DIAGONALS.items() if (x + dx, z + dz) in tiles]
        kind = orientation = None
        if len(sides) == 1:
            side = sides[0]
            # Diagonals on the far side would meet this straight edge at a hard corner.
            if all(side in d for d in diagonals):
                kind, orientation = 'straight', side
        elif len(sides) == 2 and {sides[0], sides[1]} not in ({'N', 'S'}, {'W', 'E'}):
            corner = next(k for k in DIAGONALS if set(k) == set(''.join(sides)))
            # Both arms may continue past the corner; only the far diagonal is a conflict.
            opposite = next(k for k in DIAGONALS if not set(k) & set(corner))
            if opposite not in diagonals:
                kind, orientation = 'inner', corner
        elif not sides and len(diagonals) == 1:
            kind, orientation = 'outer', diagonals[0]
        if kind is None:
            if allow_unsupported:
                ring.append({'tile': (x, z), 'kind': 'unsupported', 'orientation': ','.join(sides + diagonals)})
                continue
            require(False, f'Tile {x},{z} lies between two path segments ({",".join(sides + diagonals)}); '
                    'widen, join or separate them by at least two tiles', 'UNSUPPORTED_BORDER')
        ring.append({'tile': (x, z), 'kind': kind, 'orientation': orientation, 'material': 'road01_r'})
    return ring


def rim_uv(kind, orientation, tx, tz):
    """Tile-local (xl, zl in 0..1) -> (u, v) for one rim piece at tile (tx, tz)."""
    if kind == 'straight':
        (ux, uz), (vx, vz) = FRAMES['straight'][orientation]
    else:
        (ux, uz), (vx, vz) = FRAMES['corner'][orientation]

    def uv(local):
        cx, cz = local[0] - 0.5, local[1] - 0.5
        s, t = cx * ux + cz * uz, cx * vx + cz * vz
        if kind == 'straight':
            along = (tx + local[0]) * vx + (tz + local[1]) * vz
            return 0.25 + 0.48 * s, 0.5 * along
        return 0.75 + 0.48 * s, (0.25 if kind == 'inner' else 0.75) + 0.48 * t
    return uv


def rim_affine(kind, orientation, local_tile, origin):
    """3x2 affine from model coordinates (x, z, 1) to UV for a rim tile in one cell.

    ``local_tile`` is the tile inside its cell; model x = local*16 - 256.
    """
    tx, tz = local_tile
    f = rim_uv(kind, orientation, tx, tz)
    points = [(0, 0), (1, 0), (0, 1)]
    a = np.array([[(tx + px) * 16 - 256, (tz + pz) * 16 - 256, 1.0] for px, pz in points])
    b = np.array([f(p) for p in points])
    return np.linalg.solve(a, b)


# ---- stock readers (evidence and tests) -----------------------------------------------------

def stock_grass(family):
    """Grass tiles and ring pieces {half: (material, uv0)} of one stock model's egrass family."""
    grass, pieces = set(), {}
    for name, prim in family.items():
        for poly in prim.polygons:
            v = prim.vertices[poly]
            if np.ptp(v[:, 1]) > 1e-4:
                continue
            xs, zs = v[:, 0], v[:, 2] - GRASS_SHIFT
            coef, *_ = np.linalg.lstsq(np.column_stack([xs, zs, np.ones(len(xs))]), prim.uvs[poly], rcond=None)
            for hx in np.arange(xs.min(), xs.max() - 1e-6, 8):
                for hz in np.arange(zs.min(), zs.max() - 1e-6, 8):
                    half = (int(round((hx + 256) / 8)), int(round((hz + 256) / 8)))
                    if name == 'egrass':
                        grass.add((half[0] // 2, half[1] // 2))
                    else:
                        u, w = np.array([hx, hz, 1.0]) @ coef
                        pieces[half] = (name, (round(u % 1.0, 2), round(w % 1.0, 2)))
    return grass, pieces


def stock_rims(road, rim):
    """Path tiles and rim tiles {tile: (kind, uv at centre)} from stock road/rim primitives."""
    def tiles_of(v):
        xs, zs = (v[:, 0] + 256) / 16, (v[:, 2] + 256) / 16
        return [(x, z) for x in range(int(round(xs.min())), int(round(xs.max())))
                for z in range(int(round(zs.min())), int(round(zs.max())))]
    region, pieces = set(), {}
    for poly in road.polygons:
        v = road.vertices[poly]
        if np.ptp(v[:, 1]) < 1e-4 and len(poly) == 4:
            region.update(tiles_of(v))
    for poly in rim.polygons:
        v = rim.vertices[poly]
        if np.ptp(v[:, 1]) > 1e-4 or len(poly) != 4:
            continue
        for tx, tz in tiles_of(v):
            if not (0 <= tx < 32 and 0 <= tz < 32):
                continue
            xs, zs = (v[:, 0] + 256) / 16 - tx, (v[:, 2] + 256) / 16 - tz
            coef, *_ = np.linalg.lstsq(np.column_stack([xs, zs, np.ones(len(xs))]), rim.uvs[poly], rcond=None)
            u, w = np.array([0.5, 0.5, 1.0]) @ coef
            kind = 'straight' if u % 1.0 < 0.5 else ('inner' if w % 1.0 < 0.5 else 'outer')
            pieces[(tx, tz)] = (kind, (float(u), float(w)))
    return region, pieces
